"""Offline, audited FileGDB -> ESRI Shapefile conversion.

Reads only the explicitly selected local File Geodatabase via OpenFileGDB.
All output is in a *new* local directory; the input GDB remains unchanged.
The SHP field-name/format limitations are never silently called lossless.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .datasets import DatasetError, GdalRuntime
from .plans import digest_file, save_json

_SUPPORTED_GEOMETRIES = ("point", "line", "polygon")
_ALLOWED_FIELDS = {"String", "Integer", "Integer64", "Real", "Date"}
_LONG_FIELD_BYTES = 10


def _local_gdb(source: Path) -> Path:
    path = Path(source).expanduser().absolute()
    if any(x.is_symlink() for x in (path, *path.parents)):
        raise DatasetError("GDB_SYMLINK_BLOCKED", "不能通过符号链接访问 GDB。")
    if path.suffix.lower() != ".gdb" or not path.is_dir():
        raise DatasetError("GDB_NOT_FOUND", "请指定本机实际存在的 .gdb 文件夹。")
    if os.name == "nt" and str(path).startswith("\\\\"):
        raise DatasetError("GDB_REMOTE_DISALLOWED", "首版只处理本机 GDB，不访问共享盘。")
    return path.resolve()


def gdb_fingerprint(directory: Path) -> str:
    """Read-only SHA256 covering names and contents, not just timestamps."""
    digest = hashlib.sha256()
    source = _local_gdb(directory)
    for file in sorted(source.rglob("*"), key=lambda item: str(item).casefold()):
        if file.is_symlink():
            raise DatasetError("GDB_SYMLINK_BLOCKED", "GDB 中含符号链接，拒绝跟随。")
        if file.is_file():
            digest.update(file.relative_to(source).as_posix().encode("utf-8"))
            digest.update(bytes.fromhex(digest_file(file)))
    return digest.hexdigest()


def _filename(name: str, used: set[str]) -> str:
    # Preserve leading underscores in ArcGIS feature class names, so QGIS
    # Style Assistant can emit a matching <layer>.sld without user renaming.
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name)
    safe = re.sub(r"_+", "_", safe).strip(" .")
    if not safe.strip("_") or safe[0].isdigit():
        safe = "layer_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    safe = safe[:70]
    candidate = safe
    number = 2
    while candidate.casefold() in used:
        suffix = f"_{number}"
        candidate = safe[: 70 - len(suffix)] + suffix
        number += 1
    used.add(candidate.casefold())
    return candidate


def _field_aliases(fields: list[dict]) -> list[dict[str, Any]]:
    """DBF-safe ASCII identifiers, deterministic per original field list."""
    used: set[str] = set()
    results = []
    for index, field in enumerate(fields):
        original = field["name"]
        if not isinstance(original, str) or not original or "\0" in original:
            raise DatasetError("GDB_FIELD_INVALID", "GDB 字段名称无效。")
        simple = re.fullmatch("[A-Za-z_][A-Za-z0-9_]{0,9}", original)
        if simple and original.casefold() not in used:
            alias = original
        else:
            prefix = re.sub(r"[^A-Za-z0-9_]", "", original.upper())[:6]
            if not prefix or prefix[0].isdigit():
                prefix = "F"
            digest = hashlib.sha256(original.encode("utf-8")).hexdigest().upper()
            alias = (prefix[:6] + "_" + digest[:3])[:10]
            salt = 0
            while alias.casefold() in used:
                salt += 1
                alias = "F" + hashlib.sha256(
                    (original + "|" + str(salt)).encode("utf-8")
                ).hexdigest().upper()[:9]
        used.add(alias.casefold())
        results.append(
            {
                "original": original,
                "shp_field": alias,
                "source_type": field.get("type", ""),
                "source_width": field.get("width"),
                "renamed": alias != original,
                "omitted": field.get("type") not in _ALLOWED_FIELDS,
                "index": index,
            }
        )
    return results


def _quoted_identifier(name: str) -> str:
    if not name or "\0" in name:
        raise DatasetError("GDB_IDENTIFIER_INVALID", "GDB 含非法字段/图层名称。")
    return '"' + name.replace('"', '""') + '"'


def _sql_query(layer: str, mappings: list[dict]) -> str:
    permitted = [m for m in mappings if not m["omitted"]]
    fields = ", ".join(
        f'{_quoted_identifier(m["original"])} AS {_quoted_identifier(m["shp_field"])}'
        for m in permitted
    )
    if not fields:
        raise DatasetError("GDB_NO_SUPPORTED_FIELDS", "图层属性都不支持 SHP，拒绝静默导出。")
    return f"SELECT {fields} FROM {_quoted_identifier(layer)}"


def inspect_gdb(runtime: GdalRuntime, source: Path | str) -> dict:
    directory = _local_gdb(Path(source))
    info = runtime._json("ogrinfo", ["-ro", "-json", "-so", "-al", str(directory)])
    if info.get("driverShortName") != "OpenFileGDB":
        raise DatasetError("GDB_DRIVER_UNEXPECTED", "当前 GDB 未使用受控 OpenFileGDB 驱动读取。")
    raw_layers = info.get("layers", [])
    if not isinstance(raw_layers, list) or not raw_layers:
        raise DatasetError("GDB_NO_LAYERS", "未找到可读取的 GDB 要素类。")
    names: set[str] = set()
    layers = []
    files_used: set[str] = set()
    for layer in raw_layers:
        name = layer.get("name")
        if not isinstance(name, str) or name.casefold() in names:
            raise DatasetError("GDB_LAYER_NAME_COLLISION", "GDB 中图层名称重复或无效。")
        names.add(name.casefold())
        geom = (layer.get("geometryFields") or [{}])[0]
        geometry_type = geom.get("type") or ""
        crs_info = geom.get("coordinateSystem") or {}
        epsg = (crs_info.get("projjson") or {}).get("id") or {}
        crs = (
            f"EPSG:{epsg['code']}"
            if epsg.get("authority") == "EPSG" and str(epsg.get("code", "")).isdigit()
            else None
        )
        mappings = _field_aliases(layer.get("fields", []))
        annotation = name.upper().endswith("_A") and any(
            f["original"].lower() == "element" and f["source_type"] == "Binary"
            for f in mappings
        )
        warnings = []
        if annotation:
            warnings.append("注记要素层：SHP 仅能保存几何边界与可支持的属性，不能恢复文字排版。")
        if any(f["renamed"] for f in mappings):
            warnings.append("存在长字段或中文字段名，已生成短字段名映射；SLD 字段引用需同步核对。")
        if any(f["omitted"] for f in mappings):
            warnings.append("部分字段类型不被 SHP 支持，必须查看省略字段清单。")
        if any(f["source_width"] and f["source_width"] > 254 for f in mappings if f["source_type"] == "String"):
            warnings.append("部分源文本宽度超过 SHP DBF 的 254 字节限制，存在属性截断风险。")
        if not crs:
            warnings.append("无明确 EPSG 坐标系，未自动推断/重投影。")
        if not any(g in geometry_type.lower() for g in _SUPPORTED_GEOMETRIES):
            warnings.append("不支持的几何类型，不能转换成 SHP。")
        if "Curve" in geometry_type or "Circular" in geometry_type:
            warnings.append("曲线几何导出 SHP 可能被线性化，需人工验收。")
        filename = _filename(name, files_used)
        layers.append(
            {
                "name": name,
                "output_basename": filename,
                "geometry": geometry_type,
                "crs": crs,
                "feature_count": layer.get("featureCount"),
                "field_map": mappings,
                "annotation": annotation,
                "warnings": warnings,
            }
        )
    return {
        "source": str(directory),
        "driver": "OpenFileGDB",
        "layer_count": len(layers),
        "feature_count": sum(int(x["feature_count"] or 0) for x in layers),
        "layers": layers,
        "gdal_version": runtime.probe()["version"],
    }


def _export_one(runtime: GdalRuntime, gdb: Path, layer: dict, dest: Path) -> None:
    executable = runtime.bin_dir / ("ogr2ogr.exe" if os.name == "nt" else "ogr2ogr")
    if not executable.is_file():
        raise DatasetError("OGR2OGR_MISSING", "受控 GDAL 目录中找不到 ogr2ogr。")
    args = [
        str(executable),
        "-f", "ESRI Shapefile",
        str(dest),
        str(gdb),
        "-dialect", "OGRSQL",
        "-sql", _sql_query(layer["name"], layer["field_map"]),
        "-lco", "ENCODING=UTF-8",
        "-lco", "2GB_LIMIT=YES",
        "-lco", "RESIZE=YES",
    ]
    try:
        result = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=240, shell=False, env=runtime._environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DatasetError("OGR2OGR_EXECUTION_FAILED", "本地要素类导出中断；请核对隔离产物。") from None
    if result.returncode:
        # Native stderr can contain sensitive feature values and filesystem paths.
        # Recognize only an exact diagnostic category; never expose raw output.
        detail = result.stderr.lower()
        if "index is out of sync" in detail or "invalid row length" in detail:
            raise DatasetError(
                "GDB_INDEX_READ_ANOMALY",
                "OpenFileGDB 检测到记录索引与长度异常，可能是源数据问题或驱动兼容性；请用 ArcGIS 在副本中核对。不会跳过错误要素。",
            )
        raise DatasetError("OGR2OGR_CONVERSION_FAILED", "GDAL 导出该图层失败，未修改原 GDB。")


def convert_gdb(
    runtime: GdalRuntime, source: Path | str, output: Path | str,
    *, include_annotations: bool = False, only_layers: list[str] | None = None,
) -> dict:
    """Convert local GDB to independent SHPs and record every lossy constraint."""
    gdb = _local_gdb(Path(source))
    destination = Path(output).expanduser().absolute()
    if destination.exists() or destination.is_relative_to(gdb):
        raise DatasetError("GDB_OUTPUT_EXISTS", "转换目录必须是新位置，不能覆盖或位于源 GDB 内。")
    if any(part.is_symlink() for part in (destination, *destination.parents)):
        raise DatasetError("SYMLINK_NOT_ALLOWED", "转换输出路径不能经过符号链接。")
    metadata = inspect_gdb(runtime, gdb)
    allowed = set(only_layers or [entry["name"] for entry in metadata["layers"]])
    known = {entry["name"] for entry in metadata["layers"]}
    if not allowed or not allowed.issubset(known):
        raise DatasetError("GDB_LAYER_SELECTION_INVALID", "选择的 GDB 图层不在实际源库中。")
    before = gdb_fingerprint(gdb)
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    metadata_results = {item["name"]: item for item in metadata["layers"]}
    for name in [x["name"] for x in metadata["layers"] if x["name"] in allowed]:
        entry = metadata_results[name]
        result = {
            "source_layer": name,
            "output_basename": entry["output_basename"],
            "geometry": entry["geometry"],
            "crs": entry["crs"],
            "source_feature_count": entry["feature_count"],
            "field_map": entry["field_map"],
            "warnings": list(entry["warnings"]),
        }
        if entry["annotation"] and not include_annotations:
            result["status"] = "SKIPPED_ANNOTATION"
        elif not entry["feature_count"]:
            result["status"] = "SKIPPED_EMPTY"
        elif not entry["crs"] or not any(g in entry["geometry"].lower() for g in _SUPPORTED_GEOMETRIES):
            result["status"] = "SKIPPED_UNSUPPORTED"
        else:
            # Never leave a failed partial SHP in the importable top level.
            # The publisher discovery function explicitly ignores artifacts/.
            stage = destination / "artifacts" / entry["output_basename"]
            stage.mkdir(parents=True, exist_ok=False)
            shp = stage / (entry["output_basename"] + ".shp")
            try:
                _export_one(runtime, gdb, entry, shp)
                if not all(shp.with_suffix(s).is_file() for s in (".shp", ".shx", ".dbf", ".prj", ".cpg")):
                    raise DatasetError("SHP_COMPONENT_INCOMPLETE", "导出的 SHP 文件组件不完整。")
                converted = runtime._json("ogrinfo", ["-ro", "-json", "-so", "-al", str(shp)])
                dest_layers = converted.get("layers", [])
                if len(dest_layers) != 1:
                    raise DatasetError("SHP_LAYER_UNEXPECTED", "转换后的 SHP 图层数量异常。")
                actual = dest_layers[0]
                original_count = int(entry["feature_count"])
                new_count = actual.get("featureCount")
                if new_count != original_count:
                    raise DatasetError("SHP_FEATURE_COUNT_MISMATCH", "导出前后要素数量不一致。")
                expected_fields = [m["shp_field"] for m in entry["field_map"] if not m["omitted"]]
                actual_fields = [f.get("name") for f in actual.get("fields", [])]
                if [x.casefold() for x in actual_fields] != [x.casefold() for x in expected_fields]:
                    raise DatasetError("SHP_FIELD_MAP_MISMATCH", "实际 SHP 字段与记录的映射不一致。")
                actual_crs = (
                    ((actual.get("geometryFields") or [{}])[0].get("coordinateSystem") or {})
                    .get("projjson", {}).get("id", {})
                )
                if f"EPSG:{actual_crs.get('code')}" != entry["crs"]:
                    result["warnings"].append("导出后无法确证坐标系与源 EPSG 一致，请复核 PRJ。")
                output_files = []
                for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                    source_component = shp.with_suffix(extension)
                    target_component = destination / source_component.name
                    source_component.replace(target_component)
                    output_files.append(
                        {"name": target_component.name, "sha256": digest_file(target_component)}
                    )
                result["status"] = "EXPORTED_REVIEW_REQUIRED" if result["warnings"] else "EXPORTED"
                result["output_files"] = output_files
                result["output_feature_count"] = new_count
            except DatasetError as exc:
                result["status"] = "FAILED"
                result["errors"] = [{"code": exc.code, "message": str(exc)}]
                result["incomplete_artifact_quarantined"] = True
        records.append(result)
    after = gdb_fingerprint(gdb)
    if before != after:
        raise DatasetError("GDB_SOURCE_CHANGED", "源 GDB 在转换期间发生变化，停止使用输出成果。")
    report = {
        "schema_version": 1,
        "operation": "GDB_TO_SHP_LOCAL_ONLY",
        "gdal_version": metadata["gdal_version"],
        "source_display_name": gdb.name,
        "source_sha256_before_after": before,
        "network_requests": 0,
        "source_unchanged": True,
        "destination": str(destination),
        "all_fields_lossless": False,
        "style_conversion_performed": False,
        "layers": records,
        "summary": {
            key: count for key, count in sorted(Counter(item["status"] for item in records).items())
        },
        "message": (
            "SHP 字段缩短、类型/宽度限制与注记信息不能保证无损；"
            "导出的 SHP 仅可在人工核对属性和样式后用于业务发布。"
        ),
    }
    save_json(destination / "conversion-report.json", report)
    # Excel-friendly, local-only field alias review (no source feature values).
    with (destination / "字段对照.csv").open(
        "x", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["要素类", "原始字段名", "SHP字段名", "原始类型", "原始宽度", "是否改名", "是否省略", "处理结果"]
        )
        for item in records:
            for field in item["field_map"]:
                writer.writerow([
                    item["source_layer"], field["original"], field["shp_field"],
                    field["source_type"], field["source_width"] or "",
                    "是" if field["renamed"] else "否",
                    "是" if field["omitted"] else "否", item["status"],
                ])
    explanation = (
        "本地 GDB → SHP 转换结果\\n"
        "转换成功不代表字段与样式无损。请查看字段对照.csv 和 conversion-report.json。\\n"
        "失败图层的部分导出文件放在 artifacts/，不会自动加入发布任务。\\n"
        "注记层默认跳过；原数据源和原坐标系均未修改。\\n"
        "正式发布前请核对属性、字段映射、SLD 字段引用和地图外观。\\n"
        f"图层状态统计：{report['summary']}\\n"
    )
    (destination / "转换说明.txt").write_text(explanation, encoding="utf-8-sig")
    return report
