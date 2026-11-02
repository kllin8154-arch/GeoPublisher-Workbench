# -*- coding: utf-8 -*-
"""Generate a genuine tiny MXD using installed ArcMap 10.x / ArcPy only.

Requires a valid blank MXD template supplied by the ArcMap installation/user.
No attempt is made to fake or reverse-engineer the binary MXD format.
Test geometries are deterministic and contain no actual geological data.
Compatible with ArcMap's Python 2.7 syntax.
"""
from __future__ import print_function

import argparse
import os
import sys


def build_synthetic_mxd(template, output, arc=None):
    template = os.path.abspath(template)
    output = os.path.abspath(output)
    if not template.lower().endswith(".mxd") or not os.path.isfile(template):
        raise ValueError("请提供 ArcMap 能实际打开的空白 .mxd 模板。")
    if os.path.exists(output):
        raise ValueError("请选择尚不存在的全新输出目录。")
    if arc is None:
        import arcpy
        arc = arcpy
    arc.env.overwriteOutput = False
    arc.env.outputCoordinateSystem = arc.SpatialReference(4326)
    project = arc.mapping.MapDocument(template)
    frames = arc.mapping.ListDataFrames(project)
    if not frames:
        raise ValueError("该 MXD 模板没有可用于添加图层的数据框。")
    os.makedirs(output)
    names = (
        ("TEST_POINT", "POINT"),
        ("TEST_LINE", "POLYLINE"),
        ("TEST_AREA", "POLYGON"),
    )
    created = []
    try:
        for name, geometry_type in names:
            arc.CreateFeatureclass_management(
                output, name + ".shp", geometry_type,
                spatial_reference=arc.SpatialReference(4326),
            )
            path = os.path.join(output, name + ".shp")
            arc.AddField_management(path, "LABEL", "TEXT", field_length=32)
            points = [
                arc.Point(110.1, 28.9),
                arc.Point(110.3, 29.1),
                arc.Point(110.5, 28.9),
                arc.Point(110.1, 28.9),
            ]
            if geometry_type == "POINT":
                geometry = arc.PointGeometry(points[0], arc.SpatialReference(4326))
            elif geometry_type == "POLYLINE":
                geometry = arc.Polyline(arc.Array(points[:3]), arc.SpatialReference(4326))
            else:
                geometry = arc.Polygon(arc.Array(points), arc.SpatialReference(4326))
            with arc.da.InsertCursor(path, ["SHAPE@", "LABEL"]) as cursor:
                cursor.insertRow((geometry, name))
            arc.mapping.AddLayer(frames[0], arc.mapping.Layer(path), "TOP")
            created.append(name)
        destination = os.path.join(output, "synthetic_map.mxd")
        project.saveACopy(destination)
        if not os.path.isfile(destination) or os.path.getsize(destination) == 0:
            raise RuntimeError("ArcMap 未生成有效的 MXD 文件。")
        return {"mxd": destination, "layers": created, "synthetic": True}
    finally:
        del project


def main(argv=None):
    parser = argparse.ArgumentParser(description="用 ArcMap Python 2.7 真正创建数学测试 MXD")
    parser.add_argument("template_mxd", help="已有的 ArcMap 空白 MXD 模板")
    parser.add_argument("new_output_dir", help="新输出目录，不可已存在")
    args = parser.parse_args(argv)
    try:
        result = build_synthetic_mxd(args.template_mxd, args.new_output_dir)
    except ValueError as error:
        print(error)
        return 2
    print("Created synthetic map and {0} layer(s).".format(len(result["layers"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
