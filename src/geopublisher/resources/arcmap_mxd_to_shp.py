# -*- coding: utf-8 -*-
"""ArcMap 10.x Python/ArcPy offline MXD -> referenced SHP and matching LYR.

The MXD does NOT contain the feature geometries. Only readable local feature
sources are eligible. This helper must run in installed ArcMap's Python 2.7.
Never connects to GeoServer or uploads files.
"""
from __future__ import print_function

import argparse
import io
import json
import locale
import os
import re
import sys

try:
    text_type = unicode
except NameError:
    text_type = str


def as_text(value):
    if isinstance(value, text_type):
        return value
    try:
        return value.decode("utf-8", "replace")
    except AttributeError:
        return text_type(value)


def print_status(value):
    """Keep ArcMap Python 2.7 alive when stdout is redirected without an encoding."""
    message = as_text(value)
    if sys.version_info[0] < 3:
        encoding = getattr(sys.stdout, "encoding", None) or locale.getpreferredencoding(False) or "utf-8"
        sys.stdout.write(message.encode(encoding, "replace") + b"\n")
    else:
        print(message)


def safe_filename(value):
    name = re.sub(u'[<>:"/\\\\|?*\x00-\x1f]+', u"_", as_text(value))
    name = re.sub(u"_+", u"_", re.sub(u"\\s+", u"_", name)).strip(u" ._")[:70] or u"layer"
    if name.split(u".", 1)[0].upper() in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT1"):
        name = u"gis_" + name
    return name[:70]


def unique_name(value, used):
    base = safe_filename(value)
    name = base
    n = 2
    while name.lower() in used:
        suffix = u"_{0}".format(n)
        name = base[:70 - len(suffix)] + suffix
        n += 1
    used.add(name.lower())
    return name


def eligible(layer, arc):
    if not getattr(layer, "isFeatureLayer", False) or getattr(layer, "isGroupLayer", False):
        return False, u"非矢量要素图层"
    if getattr(layer, "isBroken", False):
        return False, u"原数据源断开，MXD 无法恢复要素"
    if not layer.supports("DATASOURCE"):
        return False, u"无法获取数据源"
    source = as_text(layer.dataSource)
    if (
        not source
        or source.startswith(u"\\\\")
        or "://" in source
        or ".sde" in source.lower()
        or not os.path.isabs(source)
        or not arc.Exists(source)
    ):
        return False, u"仅支持可访问的本地 SHP / GDB；拒绝服务、SDE、UNC"
    return True, u"本地矢量数据"


def export_mxd(mxd_file, output_dir, allow_filtered=False, arc=None, cancel_file=None):
    if arc is None:
        import arcpy
        arc = arcpy
    mxd_file = os.path.abspath(mxd_file)
    output_dir = os.path.abspath(output_dir)
    if os.path.splitext(mxd_file)[1].lower() != ".mxd" or not os.path.isfile(mxd_file):
        raise ValueError(u"必须选择已有的 MXD")
    if os.path.exists(output_dir):
        raise ValueError(u"输出目录必须全新，拒绝覆盖")
    arc.env.overwriteOutput = False
    document = arc.mapping.MapDocument(mxd_file)
    results, used = [], set()
    try:
        os.makedirs(output_dir)
        for frame in arc.mapping.ListDataFrames(document):
            for layer in arc.mapping.ListLayers(document, "", frame):
                row = {"source_layer": as_text(getattr(layer, "name", u"未命名")),
                       "data_frame": as_text(frame.name)}
                if cancel_file and os.path.isfile(cancel_file):
                    row.update(status="CANCELLED", reason=u"用户请求安全取消，未开始此图层")
                    results.append(row)
                    continue
                try:
                    ok, reason = eligible(layer, arc)
                    if not ok:
                        row.update(status="SKIPPED", reason=reason)
                        results.append(row)
                        continue
                    if getattr(layer, "definitionQuery", "") and not allow_filtered:
                        row.update(status="SKIPPED", reason=u"存在 Definition Query；需明确 --allow-filtered 才能导出子集")
                        results.append(row)
                        continue
                    name = unique_name(layer.name, used)
                    shp = os.path.join(output_dir, name + u".shp")
                    lyr = os.path.join(output_dir, name + u".lyr")
                    arc.CopyFeatures_management(layer, shp)
                    if not arc.Exists(shp):
                        raise RuntimeError(u"ArcPy 未生成 SHP")
                    row.update(status="SHP_EXPORTED", shp=name + u".shp")
                    try:
                        arc.SaveToLayerFile_management(layer, lyr, "ABSOLUTE")
                        if not os.path.isfile(lyr):
                            raise RuntimeError(u"ArcPy 未生成 LYR")
                        row.update(status="EXPORTED", lyr=name + u".lyr")
                    except Exception:
                        row.update(status="SHP_EXPORTED_LYR_FAILED", reason=u"SLD 样式准备未完成；SHP 保留")
                    if not os.path.isfile(os.path.splitext(shp)[0] + u".prj"):
                        row["crs_warning"] = u"缺少 PRJ，不能猜测坐标系"
                    if not os.path.isfile(os.path.splitext(shp)[0] + u".cpg"):
                        row["encoding_warning"] = u"缺少 CPG，需检查中文 DBF 编码"
                except Exception:
                    row.update(status="FAILED", reason=u"图层导出失败；局部产物已保留，需人工核对")
                results.append(row)
    finally:
        del document
    report = {
        "schema_version": 1, "converter": "ArcMap-10x-local",
        "source_format": "MXD", "source_filename": os.path.basename(mxd_file),
        "items": results, "files_relative_to_output": True, "uploaded": False,
        "note": u"LYR 可能仍引用原数据源。请在 QGIS 插件中把 LYR 样式应用到同名 SHP 后导出 SLD。",
    }
    with io.open(os.path.join(output_dir, "mxd-export-manifest.json"), "w", encoding="utf-8") as out:
        out.write(as_text(json.dumps(report, ensure_ascii=False, indent=2)))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="使用 ArcMap Python 离线导出 MXD 引用的 SHP 和 LYR")
    parser.add_argument("mxd")
    parser.add_argument("output")
    parser.add_argument("--allow-filtered", action="store_true")
    parser.add_argument("--cancel-file", help="本地取消检查点文件")
    args = parser.parse_args(argv)
    try:
        report = export_mxd(args.mxd, args.output, allow_filtered=args.allow_filtered,
                            cancel_file=args.cancel_file)
    except ValueError as error:
        print_status(error)
        return 2
    exported = sum(row["status"] in ("EXPORTED", "SHP_EXPORTED_LYR_FAILED") for row in report["items"])
    print_status(u"完成：SHP {0} 个，清单在新输出目录。".format(exported))
    return 0 if exported and not any(row["status"] == "CANCELLED" for row in report["items"]) else 1


if __name__ == "__main__":
    sys.exit(main())
