"""Explicit, offline SHP attribute splitting; never infer a geological map sheet.

A reviewed ID field partitions records, not geological boundaries. Only a
completed, verified local manifest makes the entire output batch importable.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path

from .datasets import DatasetError, GdalRuntime
from .plans import digest_file, fingerprint, save_json

_COMPONENTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
_ALLOWED_TYPES = {"String", "Integer", "Integer64"}
_MAX_SHEETS = 100
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _fail(code: str, message: str) -> None:
    raise DatasetError(code, message)


def _safe_path(source: Path | str, *, file: bool) -> Path:
    path = Path(source).expanduser().absolute()
    if (str(path).startswith(("\\\\", "//"))
            or path.is_symlink() or any(p.is_symlink() for p in path.parents)):
        _fail("SPLIT_PATH_UNSAFE", "仅允许本机真实路径，不能通过共享盘或符号链接操作图幅。")
    if file and (path.suffix.casefold() != ".shp" or not path.is_file()):
        _fail("SPLIT_SOURCE_INVALID", "请选择本机已有的 SHP 文件，保留配套的 SHX/DBF/PRJ/CPG。")
    return path


def _hashes(
    source: Path, *, allow_missing_cpg: bool = False,
) -> list[dict[str, str]]:
    values = []
    for suffix in _COMPONENTS:
        part = source.with_suffix(suffix)
        if suffix == ".cpg" and allow_missing_cpg and not part.exists():
            continue  # Explicitly chosen encoding; never write CPG into the original.
        if not part.is_file() or part.is_symlink() or part.stat().st_size == 0:
            _fail("SPLIT_COMPONENT_MISSING", f"图层缺少或为空的必要组件：{suffix}")
        values.append({"path": str(part), "sha256": digest_file(part)})
    return values


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _predicate(field: str, value: str, kind: str) -> str:
    # The only values admitted are conservative ASCII map-sheet codes.
    if not _CODE.fullmatch(value):
        _fail("SPLIT_VALUE_INVALID", "图幅编号有空值或非法字符，请人工核对原始属性；不能静默跳过。")
    if kind == "String":
        return f"{_identifier(field)} = '{value}'"
    if not re.fullmatch(r"[+-]?\d+", value):
        _fail("SPLIT_VALUE_INVALID", "整数图幅编号含非整数值。")
    return f"{_identifier(field)} = {int(value)}"


def _source_stats(runtime: GdalRuntime, source: Path, encoding: str | None) -> dict:
    inspection = runtime.inspect(source, encoding=encoding)
    if inspection.get("blocking_errors"):
        issues = "; ".join(item["message"] for item in inspection["blocking_errors"][:4])
        _fail("SPLIT_SOURCE_BLOCKED", "SHP 本地预检未通过：" + issues)
    if (type(inspection.get("feature_count")) is not int
            or inspection["feature_count"] < 1
            or not isinstance(inspection.get("native_bbox"), list)
            or not isinstance(inspection.get("crs"), str)):
        _fail("SPLIT_SOURCE_INCOMPLETE", "源图层缺少明确的总要素数、范围或坐标系。")
    return inspection


def _distinct_values(
    runtime: GdalRuntime, source: Path, field: str, kind: str,
) -> list[str]:
    layer = _identifier(source.stem)
    statement = f"SELECT DISTINCT {_identifier(field)} FROM {layer}"
    document = runtime._json("ogrinfo", [
        "-ro", "-json", "-features", "-geom", "NO",
        "-limit", str(_MAX_SHEETS + 1),
        "-dialect", "OGRSQL", "-sql", statement, str(source),
    ])
    layers = document.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        _fail("SPLIT_DISTINCT_FAILED", "受控 GDAL 无法只读枚举图幅编号。")
    data = layers[0]
    if (type(data.get("featureCount")) is not int
            or data["featureCount"] > _MAX_SHEETS):
        _fail("SPLIT_TOO_MANY_SHEETS", "图幅数量超过 100，需先分批处理。")
    entries = data.get("features")
    if (not isinstance(entries, list) or len(entries) != data["featureCount"]
            or not entries):
        _fail("SPLIT_DISTINCT_FAILED", "图幅编号枚举不完整，请核对字段类型。")
    values = []
    for entry in entries:
        raw = entry.get("properties", {}).get(field)
        if (kind == "String" and not isinstance(raw, str)) or (
            kind != "String" and (type(raw) is not int)
        ):
            _fail("SPLIT_VALUE_INVALID", "存在缺失、空白或不支持的图幅编号。")
        value = str(raw)
        _predicate(field, value, kind)
        values.append(value)
    if len({x.casefold() for x in values}) != len(values):
        _fail("SPLIT_VALUE_COLLISION", "图幅编号存在大小写冲突；Windows 文件名会冲突。")
    return sorted(values)


def _count_for(runtime: GdalRuntime, source: Path, predicate: str) -> int:
    result = runtime._json("ogrinfo", [
        "-ro", "-json", "-so", "-al", "-where", predicate, str(source),
    ])
    layers = result.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        _fail("SPLIT_COUNT_INVALID", "无法检查图幅筛选后的要素数量。")
    value = layers[0].get("featureCount")
    if type(value) is not int or value < 1:
        _fail("SPLIT_COUNT_INVALID", "存在空图幅或无法验证的要素数量。")
    return value


def _basename(source: Path, code: str) -> str:
    # Keep the complete code visible when possible and retain uniqueness via hash.
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", source.stem).strip("_")[:24] or "layer"
    return f"{stem}__{code[:30]}_{hashlib.sha256(code.encode()).hexdigest()[:8]}"


def inspect_sheet_split(
    runtime: GdalRuntime, source: Path | str, field: str,
    *, encoding: str | None = None,
) -> dict:
    """Read-only frozen preview. Candidate fields are never self-approved."""
    path = _safe_path(source, file=True)
    hashes = _hashes(path, allow_missing_cpg=bool(encoding))
    source_info = _source_stats(runtime, path, encoding)
    found = [entry for entry in source_info["fields"]
             if entry.get("name") == field and entry.get("type") in _ALLOWED_TYPES]
    if len(found) != 1:
        names = ", ".join(entry["name"] for entry in source_info["fields"]
                          if entry.get("type") in _ALLOWED_TYPES)
        _fail("SPLIT_FIELD_INVALID", "请选择已核实的字符串/整数字段。可选字段：" + names[:250])
    kind = found[0]["type"]
    values = _distinct_values(runtime, path, field, kind)
    if len(values) < 2:
        _fail("SPLIT_SINGLE_VALUE", "只找到一个图幅编号，无需按字段拆分。")
    parts, total = [], 0
    for value in values:
        count = _count_for(runtime, path, _predicate(field, value, kind))
        parts.append({
            "sheet_code": value, "feature_count": count,
            "output_basename": _basename(path, value),
        })
        total += count
    if total != source_info["feature_count"]:
        _fail(
            "SPLIT_COUNT_NOT_CONSERVED",
            "存在空值、未归组或重复计数的要素，拒绝跳过任何原始记录。",
        )
    if len({x["output_basename"].casefold() for x in parts}) != len(parts):
        _fail("SPLIT_NAME_COLLISION", "导出文件名冲突，无法安全保存独立图幅。")
    summary = {
        "source": str(path), "field": field, "field_type": kind,
        "encoding": source_info["encoding"], "source_hashes": hashes,
        "source_count": total, "source_crs": source_info["crs"],
        "source_bbox": source_info["native_bbox"],
        "source_geometry_type": source_info["geometry_type"],
        "source_fields": [x["name"] for x in source_info["fields"]],
        "source_field_types": [x["type"] for x in source_info["fields"]],
        "groups": parts,
    }
    summary["review_sha256"] = fingerprint(summary)
    return summary


def _bbox_within(inner: list, outer: list) -> bool:
    if not isinstance(inner, list) or len(inner) != 4:
        return False
    if not all(type(v) in (int, float) and math.isfinite(v) for v in inner):
        return False
    return (
        outer[0] - 1e-7 <= inner[0] <= inner[2] <= outer[2] + 1e-7
        and outer[1] - 1e-7 <= inner[1] <= inner[3] <= outer[3] + 1e-7
    )


def split_sheet(
    runtime: GdalRuntime, source: Path | str, field: str, output: Path | str,
    *, review_sha256: str, encoding: str | None = None,
    cancelled: object = None,
) -> dict:
    """Copy exactly one output layer per audited sheet ID, never alter source."""
    preview = inspect_sheet_split(runtime, source, field, encoding=encoding)
    if preview["review_sha256"] != review_sha256:
        _fail("SPLIT_REVIEW_CHANGED", "来源或图幅统计已变化；请重新只读预览并确认，不得按旧计划拆分。")
    source_path = Path(preview["source"])
    target = _safe_path(output, file=False)
    if (target.exists() or target.is_relative_to(source_path.parent)
            or not target.parent.is_dir() or target == source_path):
        _fail("SPLIT_OUTPUT_UNSAFE", "请选择与源图层分离的本地全新输出目录，不允许覆盖。")
    executable = runtime.bin_dir / ("ogr2ogr.exe" if os.name == "nt" else "ogr2ogr")
    if not executable.is_file():
        _fail("OGR2OGR_MISSING", "受控 GDAL 中没有 ogr2ogr，不能执行分幅。")
    # Verify provenance before creating an output directory; fail closed.
    from .batch_lineage import conversion_batch

    batch_source = conversion_batch(
        source_path, source_sha256=preview["source_hashes"][0]["sha256"]
    )
    # Keep all incomplete output under _partial, never in an importable root.
    target.mkdir(parents=False, exist_ok=False)
    partial = target / "_partial"
    partial.mkdir()
    output_groups = []
    source_bbox = preview["source_bbox"]
    union_bbox = [float("inf"), float("inf"), float("-inf"), float("-inf")]
    try:
        for group in preview["groups"]:
            if cancelled is not None and cancelled():
                _fail("SPLIT_CANCELLED", "分幅已在安全检查点取消；部分产物留在 _partial。")
            code = group["sheet_code"]
            basename = group["output_basename"]
            shp = partial / (basename + ".shp")
            predicate = _predicate(field, code, preview["field_type"])
            try:
                result = subprocess.run([
                    str(executable), "-f", "ESRI Shapefile",
                    "-where", predicate, "-lco", "ENCODING=UTF-8",
                    "-lco", "2GB_LIMIT=YES", "-lco", "RESIZE=YES",
                    str(shp), str(source_path),
                ], capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=240, shell=False,
                    env=runtime._environment())
            except (subprocess.TimeoutExpired, OSError):
                _fail("SPLIT_OGR_FAILED", "本地图幅导出中断；原始 SHP 未修改。")
            if result.returncode != 0:
                _fail("SPLIT_OGR_FAILED", "GDAL 未能完整导出图幅；请检查本地隔离产物。")
            # This reads actual *output* bbox; filtered source extent is unreliable.
            extracted = _source_stats(runtime, shp, "UTF-8")
            if extracted["feature_count"] != group["feature_count"]:
                _fail("SPLIT_COUNT_MISMATCH", "分幅前后的记录数不一致；保留隔离副本，拒绝使用。")
            if extracted["crs"] != preview["source_crs"]:
                _fail("SPLIT_CRS_MISMATCH", "输出图幅的 EPSG 与源图层不一致。")
            if (
                [x["name"] for x in extracted["fields"]] != preview["source_fields"]
                or [x["type"] for x in extracted["fields"]] != preview["source_field_types"]
            ):
                _fail("SPLIT_FIELDS_MISMATCH", "输出图幅属性字段名称或类型发生变化。")
            if extracted["geometry_type"] != preview["source_geometry_type"]:
                _fail("SPLIT_GEOMETRY_MISMATCH", "输出图幅几何类型与源图层不一致。")
            bbox = extracted["native_bbox"]
            if not _bbox_within(bbox, source_bbox):
                _fail("SPLIT_EXTENT_INVALID", "导出图幅的空间范围超出源图层。")
            actual_values = _distinct_values(runtime, shp, field, preview["field_type"])
            if actual_values != [code]:
                _fail("SPLIT_VALUE_MISMATCH", "导出的图幅包含其他编号，分幅过滤不可信。")
            for j in range(4):
                union_bbox[j] = (
                    min(union_bbox[j], bbox[j]) if j < 2
                    else max(union_bbox[j], bbox[j])
                )
            hashes = _hashes(shp)
            output_groups.append({
                "status": "EXPORTED_VERIFIED",
                "sheet_code": code,
                "source_layer": source_path.stem,
                "output_basename": basename,
                "shp": shp.name,
                "feature_count": extracted["feature_count"],
                "bbox": bbox,
                "crs": extracted["crs"],
                "output_files": [
                    {"name": Path(item["path"]).name, "sha256": item["sha256"]}
                    for item in hashes
                ],
            })
        if (sum(group["feature_count"] for group in output_groups) != preview["source_count"]
                or any(abs(a - b) > 1e-7 for a, b in zip(union_bbox, source_bbox, strict=True))):
            _fail("SPLIT_COVERAGE_MISMATCH", "拆分的要素总数或空间范围并集不等于原始图层。")
        if _hashes(source_path, allow_missing_cpg=True) != preview["source_hashes"]:
            _fail("SPLIT_SOURCE_CHANGED", "执行期间原始 SHP 或配套组件发生变化，拒绝声称转换成功。")
        # The report is written only after ALL required components are promoted.
        for row in output_groups:
            for component in row["output_files"]:
                from_file = partial / component["name"]
                from_file.replace(target / from_file.name)
        if _hashes(source_path, allow_missing_cpg=True) != preview["source_hashes"]:
            _fail("SPLIT_SOURCE_CHANGED", "完成前原始 SHP 已被修改，输出不得作为已验证图幅。")
        summary_csv = target / "sheet-split-summary.csv"
        with summary_csv.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([
                "图幅编号", "要素数量", "EPSG", "最小X", "最小Y",
                "最大X", "最大Y", "SHP文件", "人工图幅边界已核验",
            ])
            for row in output_groups:
                writer.writerow([
                    row["sheet_code"], row["feature_count"], row["crs"],
                    *row["bbox"], row["shp"], "否",
                ])
        manifest = {
            "schema_version": 1,
            "operation": "SHP_SHEET_SPLIT_LOCAL_ONLY",
            "status": "COMPLETE_VERIFIED",
            "source": str(source_path),
            "source_sha256": preview["source_hashes"][0]["sha256"],
            "source_hashes": preview["source_hashes"],
            "source_batch": batch_source,
            "destination": str(target),
            "field": field,
            "field_type": preview["field_type"],
            "review_sha256": review_sha256,
            "source_count": preview["source_count"],
            "source_bbox": preview["source_bbox"],
            "source_crs": preview["source_crs"],
            "source_geometry_type": preview["source_geometry_type"],
            "groups": output_groups,
            "source_unchanged": True,
            "summary_csv": summary_csv.name,
            "summary_csv_sha256": digest_file(summary_csv),
            "full_sheet_boundary_verified": False,
            "geoserver_published": False,
            "database_registered": False,
        }
        save_json(target / "sheet-split-manifest.json", manifest)
        # Remove the empty staging folder but keep it if partial sidecars exist.
        if not any(partial.iterdir()):
            partial.rmdir()
        return manifest
    except Exception:
        # Retain all partial files and no completed manifest, never auto-retry.
        raise


def verified_split_sources(directory: Path | str) -> list[Path]:
    """Recheck a complete local split batch before enqueuing its SHPs."""
    root = _safe_path(directory, file=False)
    marker = root / "sheet-split-manifest.json"
    if (not root.is_dir() or marker.is_symlink()
            or not marker.is_file() or marker.stat().st_size > 2 * 1024 * 1024):
        _fail("SPLIT_MANIFEST_INVALID", "分幅输出缺少完成后的本地验证清单。")
    try:
        data = json.loads(marker.read_text(encoding="utf-8-sig"))
        groups = data["groups"]
        summary = root / "sheet-split-summary.csv"
        if (
            data["summary_csv"] != summary.name or not summary.is_file()
            or summary.is_symlink()
            or digest_file(summary) != data["summary_csv_sha256"]
        ):
            raise ValueError()
        source = _safe_path(data["source"], file=True)
        if (
            data["operation"] != "SHP_SHEET_SPLIT_LOCAL_ONLY"
            or data["status"] != "COMPLETE_VERIFIED"
            or data["destination"] != str(root)
            or not isinstance(groups, list) or not 2 <= len(groups) <= _MAX_SHEETS
            or _hashes(source, allow_missing_cpg=True) != data["source_hashes"]
            or data["source_sha256"] != data["source_hashes"][0]["sha256"]
        ):
            raise ValueError()
        from .batch_lineage import conversion_batch

        origin = conversion_batch(source, source_sha256=data["source_sha256"])
        if origin != data.get("source_batch"):
            raise ValueError()
        found, seen = [], set()
        for row in groups:
            name = row["shp"]
            if (not isinstance(name, str) or name != row["output_basename"] + ".shp"
                    or "/" in name or "\\" in name or name.casefold() in seen
                    or row["status"] != "EXPORTED_VERIFIED"
                    or not _CODE.fullmatch(row["sheet_code"])
                    or type(row["feature_count"]) is not int or row["feature_count"] < 1):
                raise ValueError()
            shp = _safe_path(root / name, file=True)
            actual_hashes = [
                {"name": Path(item["path"]).name, "sha256": item["sha256"]}
                for item in _hashes(shp)
            ]
            if actual_hashes != row["output_files"]:
                raise ValueError()
            seen.add(name.casefold())
            found.append(shp)
        if sum(row["feature_count"] for row in groups) != data["source_count"]:
            raise ValueError()
        return found
    except (ValueError, KeyError, TypeError, UnicodeError, OSError) as exc:
        raise DatasetError(
            "SPLIT_MANIFEST_INVALID",
            "分幅报告、原文件或任何输出组件与记录不一致；请重新检查，不加入发布。",
        ) from exc
