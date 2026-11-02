"""Offline QGIS batch conversion of paired ArcMap LYR and SHP to QML/SLD.

Run with the *installed QGIS Python launcher*, not regular system Python:
    python-qgis.bat qgis_lyr_to_sld.py SOURCE_DIRECTORY NEW_OUTPUT_DIRECTORY

Requires the separately installed QGIS Style Assistant and SLYR Community.
This script does not contact GeoServer or write into the source folder.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_MAX_STYLE_BYTES = 5 * 1024 * 1024
_REQUIRED_COMPONENTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")


class ConversionError(ValueError):
    pass


def _safe_source(path: Path) -> Path:
    if str(path).startswith("\\\\") or not path.is_dir() or path.is_symlink():
        raise ConversionError("请选择已存在的本地普通文件夹，不接受 UNC 或符号链接。")
    return path.resolve()


def _source_file(path: Path, base: Path) -> Path:
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(base):
        raise ConversionError("不允许外部路径、符号链接或缺失文件：" + path.name)
    return path


def discover_pairs(directory: Path | str) -> list[tuple[Path, Path]]:
    """Pair by exact case-insensitive basename; refuse ambiguous/partial input."""
    root = _safe_source(Path(directory))
    names: dict[tuple[str, str], Path] = {}
    for entry in root.iterdir():
        ext = entry.suffix.casefold()
        if ext not in {".lyr", ".shp"}:
            continue
        _source_file(entry, root)
        key = (entry.stem.casefold(), ext)
        if key in names:
            raise ConversionError("发现大小写冲突的同名数据：" + entry.name)
        names[key] = entry
    lyr_files = sorted(
        (path for (_, ext), path in names.items() if ext == ".lyr"),
        key=lambda path: (path.stem.casefold(), path.name),
    )
    if not lyr_files:
        raise ConversionError("没有找到 .lyr 文件；需要与同名 SHP 放在一个本地目录。")
    pairs = []
    for lyr in lyr_files:
        shp = names.get((lyr.stem.casefold(), ".shp"))
        if shp is None:
            raise ConversionError("LYR 没有同名 SHP：" + lyr.name)
        for ext in _REQUIRED_COMPONENTS:
            _source_file(shp.with_suffix(ext), root)
        if lyr.stat().st_size == 0 or shp.stat().st_size == 0:
            raise ConversionError("空的 LYR 或 SHP 文件：" + lyr.name)
        pairs.append((shp, lyr))
    return pairs


def _new_output(output: Path | str, source: Path) -> Path:
    path = Path(output).expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise ConversionError("必须选择尚不存在的全新输出目录，拒绝覆盖。")
    resolved = path.resolve()
    if resolved.is_relative_to(source) or source.is_relative_to(resolved):
        raise ConversionError("输出目录不得包含来源目录，也不能位于来源目录内部。")
    if any(parent.is_symlink() for parent in path.parents):
        raise ConversionError("输出目录不能经过符号链接。")
    return path


def _style_save_ok(response: object) -> bool:
    """PyQGIS API versions return tuples in either (message,bool) order."""
    if isinstance(response, tuple):
        flags = [item for item in response if isinstance(item, bool)]
        return len(flags) == 1 and flags[0]
    return response is True


def validate_local_sld(path: Path, geometry: int) -> None:
    if not path.is_file() or not 0 < path.stat().st_size <= _MAX_STYLE_BYTES:
        raise ConversionError("SLD 文件为空、缺失或超过 5 MiB。")
    try:
        root = ET.parse(str(path)).getroot()
    except ET.ParseError as exc:
        raise ConversionError("SLD XML 解析失败：" + str(exc)) from None
    if root.tag.rsplit("}", 1)[-1] != "StyledLayerDescriptor":
        raise ConversionError("无效 SLD 根节点。")
    wanted = ("PointSymbolizer", "LineSymbolizer", "PolygonSymbolizer")
    if geometry not in (0, 1, 2):
        raise ConversionError("仅支持点、线、面矢量样式。")
    found = {node.tag.rsplit("}", 1)[-1] for node in root.iter()}
    if wanted[geometry] not in found:
        raise ConversionError("SLD 符号类型与 SHP 几何类型不匹配。")
    if found.intersection({"ExternalGraphic", "OnlineResource", "RemoteOWS"}):
        raise ConversionError("SLD 包含外部图形或远程资源；不允许自动发布。")
    for node in root.iter():
        for key, value in node.attrib.items():
            if key.rsplit("}", 1)[-1].lower() == "href" and (
                value.lower().startswith(("http:", "https:", "file:", "ftp:"))
            ):
                raise ConversionError("SLD 包含外部资源引用。")


def _write_report(output: Path, rows: list[dict]) -> None:
    report = {
        "schema_version": 1,
        "converter": "QGIS-local-SLYR",
        "status": "PASSED" if all(row["status"] == "EXPORTED" for row in rows) else "INCOMPLETE",
        "items": rows,
        "uploaded": False,
        "note": "样式已保存，但不保证 ArcMap 与 GeoServer 渲染完全一致；须人工核对真实 WMS 图像。",
    }
    with (output / "lyr-conversion-report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)


def convert(source: Path | str, output: Path | str, *, plugin_parent: Path | str | None = None,
            qgis_prefix: str | None = None, cancel_file: str | None = None) -> list[dict]:
    pairs = discover_pairs(source)
    root = _safe_source(Path(source))
    dest = _new_output(output, root)
    # These flags are local-only. They do not make potentially remote plugin
    # functionality magically safe; all source/style references are reviewed.
    os.environ["PROJ_NETWORK"] = "OFF"
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from qgis.core import Qgis, QgsApplication, QgsVectorLayer
    except ImportError:
        raise ConversionError("请使用 QGIS 自带的 python-qgis.bat 启动此脚本。") from None
    if qgis_prefix:
        QgsApplication.setPrefixPath(qgis_prefix, True)
    app = QgsApplication.instance() or QgsApplication([], False)
    app.initQgis()
    if plugin_parent is None:
        major = Qgis.QGIS_VERSION.split(".", 1)[0]
        plugin_parent = (
            Path.home() / "AppData" / "Roaming" / "QGIS" /
            ("QGIS" + major) / "profiles" / "default" / "python" / "plugins"
        )
    plugin_dir = _safe_source(Path(plugin_parent))
    sys.path.insert(0, str(plugin_dir))
    try:
        from qgis_style_assistant.lyr_importer import apply_lyr_style
    except ImportError:
        raise ConversionError("未找到 QGIS Style Assistant / SLYR；请先在本地 QGIS 安装插件。") from None

    dest.mkdir(parents=True, exist_ok=False)
    artifacts = dest / "artifacts"
    artifacts.mkdir()
    rows: list[dict] = []
    for shp, lyr in pairs:
        if cancel_file and Path(cancel_file).is_file():
            rows.append({"shp": shp.name, "lyr": lyr.name, "status": "CANCELLED"})
            print("STYLE_CANCELLED=" + shp.stem, flush=True)
            continue
        row = {"shp": shp.name, "lyr": lyr.name, "status": "FAILED"}
        staged = artifacts / shp.stem
        staged.mkdir()
        layer = None
        try:
            layer = QgsVectorLayer(str(shp), shp.stem, "ogr")
            if not layer.isValid():
                raise ConversionError("SHP 无法读取。")
            row["feature_count"] = int(layer.featureCount())
            row["crs"] = layer.crs().authid()
            if not row["crs"]:
                raise ConversionError("无法确认 SHP 的坐标系。")
            converted = apply_lyr_style(lyr, layer)
            row["renderer"] = layer.renderer().type() if layer.renderer() else ""
            row["warnings"] = converted.get("warnings", [])
            qml = staged / (shp.stem + ".qml")
            sld = staged / (shp.stem + ".sld")
            if not _style_save_ok(layer.saveNamedStyle(str(qml))):
                raise ConversionError("QGIS 无法保存 QML。")
            if not _style_save_ok(layer.saveSldStyle(str(sld))):
                raise ConversionError("QGIS 无法保存 SLD。")
            if not qml.is_file() or not 0 < qml.stat().st_size <= _MAX_STYLE_BYTES:
                raise ConversionError("QML 文件为空、缺失或超过 5 MiB。")
            validate_local_sld(sld, int(layer.geometryType()))
            qml.rename(dest / qml.name)
            sld.rename(dest / sld.name)
            row["status"] = "EXPORTED"
        except Exception as exc:
            row["reason"] = str(exc)[:600]
        finally:
            if layer is not None:
                del layer
        rows.append(row)
        print("STYLE_" + row["status"] + "=" + shp.stem, flush=True)
    _write_report(dest, rows)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量离线转换同名 ArcMap LYR/SHP 为 QGIS QML 和 GeoServer SLD")
    parser.add_argument("source_dir", help="包含同名 SHP 和 LYR 的本地文件夹")
    parser.add_argument("new_output_dir", help="全新样式输出目录，不允许覆盖")
    parser.add_argument("--plugin-parent", help="QGIS 用户插件的父目录；默认当前 QGIS 主版本的 default profile")
    parser.add_argument("--qgis-prefix", help="需要时明确指定 QGIS 应用安装目录")
    parser.add_argument("--cancel-file", help="本地取消检查点文件（不会强行结束进程）")
    args = parser.parse_args(argv)
    try:
        rows = convert(args.source_dir, args.new_output_dir,
                       plugin_parent=args.plugin_parent, qgis_prefix=args.qgis_prefix,
                       cancel_file=args.cancel_file)
    except ConversionError as exc:
        print("STYLE_CONVERSION_BLOCKED: " + str(exc), file=sys.stderr)
        return 2
    return 0 if all(row["status"] == "EXPORTED" for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
