#!/usr/bin/env python3
"""Create 100% synthetic, reproducible offline GIS input for GeoPublisher.

Never downloads geographic information or reads existing proprietary files.
Outputs 3 SHP layers, a multi-feature-class OpenFileGDB, GeoTIFF and SLDs.
Refuses to touch an existing output directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def feature(geometry, sheet_id, label, idx):
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "SHEET_ID": sheet_id,
            "GEO_TYPE": "MOCK",
            "LABEL": label,
            "ITEM_NO": idx,
        },
    }


def datasets():
    # Fictional features only. Coordinates are arbitrary WGS84 test positions;
    # never derived from a customer's map/GDB/imagery.
    points = []
    lines = []
    areas = []
    for si, sheet in enumerate(("TEST_A01", "TEST_A02")):
        x0 = 110.1 + 0.22 * si
        y0 = 27.0
        for p in range(4):
            x = round(x0 + 0.035 + (p % 2) * 0.085, 6)
            y = round(y0 + 0.038 + (p // 2) * 0.083, 6)
            points.append(feature(
                {"type": "Point", "coordinates": [x, y]}, sheet,
                f"Synthetic sample {sheet}-{p+1}", si * 4 + p + 1,
            ))
        for j in range(2):
            coords = [
                [round(x0 + 0.016, 6), round(y0 + 0.028 + j * 0.072, 6)],
                [round(x0 + 0.069, 6), round(y0 + 0.08 + j * 0.063, 6)],
                [round(x0 + 0.147, 6), round(y0 + 0.063 + j * 0.073, 6)],
                [round(x0 + 0.184, 6), round(y0 + 0.119 + j * 0.057, 6)],
            ]
            lines.append(feature(
                {"type": "LineString", "coordinates": coords}, sheet,
                f"Synthetic route {sheet}-{j+1}", si * 2 + j + 1,
            ))
        for j in range(2):
            x = x0 + 0.015 + j * 0.09
            y = y0 + 0.012 + j * 0.054
            ring = [[x, y], [x + .082, y], [x + .082, y + .067],
                    [x, y + .067], [x, y]]
            areas.append(feature(
                {"type": "Polygon",
                 "coordinates": [[[round(a, 6), round(b, 6)] for a, b in ring]]},
                sheet, f"Synthetic area {sheet}-{j+1}", si * 2 + j + 1,
            ))
    return {
        "gp_demo_points": points,
        "gp_demo_lines": lines,
        "gp_demo_areas": areas,
    }


def _run(cmd, env):
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=150, env=env, check=False,
    )
    if result.returncode:
        # Do not dump arbitrary CLI output; it may include internal file paths.
        raise RuntimeError(
            f"GDAL_COMMAND_FAILED: {Path(cmd[0]).name} EXIT={result.returncode} "
            f"OUTPUT_TAIL={result.stderr[-350:]}"
        )
    return result.stdout


def _write_sld(path, layer, geom):
    sym = {
        "gp_demo_points": (
            '<sld:PointSymbolizer><sld:Graphic><sld:Mark>'
            '<sld:WellKnownName>circle</sld:WellKnownName>'
            '<sld:Fill><sld:CssParameter name="fill">#D46E42</sld:CssParameter></sld:Fill>'
            '<sld:Stroke><sld:CssParameter name="stroke">#332F2B</sld:CssParameter>'
            '<sld:CssParameter name="stroke-width">1</sld:CssParameter></sld:Stroke>'
            '</sld:Mark><sld:Size>10</sld:Size></sld:Graphic></sld:PointSymbolizer>'
        ),
        "gp_demo_lines": (
            '<sld:LineSymbolizer><sld:Stroke>'
            '<sld:CssParameter name="stroke">#326A92</sld:CssParameter>'
            '<sld:CssParameter name="stroke-width">3</sld:CssParameter>'
            '</sld:Stroke></sld:LineSymbolizer>'
        ),
        "gp_demo_areas": (
            '<sld:PolygonSymbolizer><sld:Fill>'
            '<sld:CssParameter name="fill">#9BC7B4</sld:CssParameter>'
            '<sld:CssParameter name="fill-opacity">0.65</sld:CssParameter>'
            '</sld:Fill><sld:Stroke>'
            '<sld:CssParameter name="stroke">#2E625B</sld:CssParameter>'
            '<sld:CssParameter name="stroke-width">1.5</sld:CssParameter>'
            '</sld:Stroke></sld:PolygonSymbolizer>'
        ),
    }[layer]
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sld:StyledLayerDescriptor version="1.0.0" '
        'xmlns:sld="http://www.opengis.net/sld" '
        'xmlns:ogc="http://www.opengis.net/ogc">\n'
        f'  <sld:NamedLayer><sld:Name>{layer}</sld:Name><sld:UserStyle>'
        f'<sld:Title>Original synthetic {geom} demo</sld:Title>'
        '<sld:FeatureTypeStyle><sld:Rule>\n'
        f'    {sym}\n'
        '</sld:Rule></sld:FeatureTypeStyle></sld:UserStyle></sld:NamedLayer>\n'
        '</sld:StyledLayerDescriptor>\n'
    )
    ET.fromstring(body.encode("utf-8"))
    path.write_text(body, encoding="utf-8")


def _raster_ppm(path):
    # A purely computed 256x256 color grid, no network tile source.
    size = 256
    with path.open("wb") as f:
        f.write(f"P6\n{size} {size}\n255\n".encode("ascii"))
        for y in range(size):
            for x in range(size):
                valley = 25 if abs(x-y) < 12 else 0
                f.write(bytes((56+x//3+valley, 82+y//3, 150-x//4)))


def generate(destination, gdal_bin):
    ogr2ogr = gdal_bin / ("ogr2ogr.exe" if os.name == "nt" else "ogr2ogr")
    ogrinfo = gdal_bin / ("ogrinfo.exe" if os.name == "nt" else "ogrinfo")
    translate = gdal_bin / ("gdal_translate.exe" if os.name == "nt" else "gdal_translate")
    for tool in (ogr2ogr, ogrinfo, translate):
        if not tool.is_file():
            raise FileNotFoundError(f"Missing controlled GDAL command: {tool.name}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Test output already exists; refusing to overwrite")
    env = dict(os.environ)
    env["PATH"] = str(gdal_bin) + os.pathsep + env.get("PATH", "")
    share = gdal_bin.parent / "share"
    if (share / "gdal").is_dir():
        env["GDAL_DATA"] = str(share / "gdal")
    if (share / "proj").is_dir():
        env["PROJ_DATA"] = str(share / "proj")
    env["PROJ_NETWORK"] = "OFF"
    destination.mkdir(parents=True)
    for name in ("source", "shp", "gdb", "raster", "styles"):
        (destination / name).mkdir()
    generated = datasets()
    for layer, features in generated.items():
        geojson = destination / "source" / f"{layer}.geojson"
        geojson.write_text(json.dumps({
            "type": "FeatureCollection", "features": features,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        shp = destination / "shp" / f"{layer}.shp"
        _run([
            str(ogr2ogr), "-f", "ESRI Shapefile", str(shp), str(geojson),
            "-nln", layer, "-a_srs", "EPSG:4326",
            "-lco", "ENCODING=UTF-8", "-lco", "RESIZE=YES",
        ], env)
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            if not shp.with_suffix(ext).exists():
                raise RuntimeError(f"Missing complete shapefile part: {layer}{ext}")
        info = json.loads(_run([str(ogrinfo), "-ro", "-json", str(shp)], env))
        layers = info["layers"]
        if len(layers) != 1:
            raise RuntimeError("Shapefile inspection returned unexpected layer count")
        fc = layers[0].get("featureCount")
        if fc != len(features):
            raise RuntimeError(f"Feature count mismatch: {layer}={fc}")
        _write_sld(destination / "styles" / f"{layer}.sld", layer, {
            "gp_demo_points": "points", "gp_demo_lines": "lines",
            "gp_demo_areas": "polygons",
        }[layer])
    gdb = destination / "gdb" / "GeoPublisher_Synthetic_MultiLayer.gdb"
    for i, layer in enumerate(generated):
        command = [str(ogr2ogr)]
        if i:
            command.append("-update")
        command.extend([
            "-f", "OpenFileGDB", str(gdb),
            str(destination / "shp" / f"{layer}.shp"),
            "-nln", layer,
        ])
        _run(command, env)
    gdb_report = json.loads(_run([str(ogrinfo), "-ro", "-json", str(gdb)], env))
    all_gdb_layers = {x["name"] for x in gdb_report["layers"]}
    if all_gdb_layers != set(generated):
        raise RuntimeError(f"GDB layer mismatch: {sorted(all_gdb_layers)}")
    ppm = destination / "source" / "fictional_grid.ppm"
    _raster_ppm(ppm)
    _run([
        str(translate), "-of", "GTiff", "-a_srs", "EPSG:4326",
        "-a_ullr", "110.0", "27.25", "110.6", "26.94",
        "-co", "COMPRESS=LZW", "-co", "TILED=YES",
        str(ppm), str(destination / "raster" / "gp_demo_fictional_rgb.tif"),
    ], env)
    manifest = {
        "synthetic_data": True,
        "source": "Procedurally invented geometry, no customer GIS data",
        "coordinate_system": "EPSG:4326",
        "map_sheets": ["TEST_A01", "TEST_A02"],
        "feature_counts": {k: len(v) for k, v in generated.items()},
        "source_geodatabase": "gdb/GeoPublisher_Synthetic_MultiLayer.gdb",
        "shapefile_input_dir": "shp",
        "raster_input": "raster/gp_demo_fictional_rgb.tif",
        "styles": sorted(p.name for p in (destination/"styles").glob("*.sld")),
        "sha256": {
            str(f.relative_to(destination)).replace("\\", "/"): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(destination.rglob("*")) if f.is_file()
        },
    }
    (destination / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (destination / "START-HERE.txt").write_text(
        "GeoPublisher 本地合成测试数据（不含真实地质数据）\n\n"
        "推荐：先测试最简单的完整自动发布，再测试整个 GDB 批量转换。\n\n"
        "方式 A：工作台点击“添加 SHP / TIFF”，依次添加 shp 目录里的\n"
        "gp_demo_points.shp、gp_demo_lines.shp、gp_demo_areas.shp。\n"
        "这 3 个图层分别有 8 个点、4 条线、4 个面；\n"
        "都有 SHEET_ID=TEST_A01/TEST_A02，可验证分组。全部采用 EPSG:4326。\n"
        "点击 下一步：检查图幅 → 确认并自动发布。\n\n"
        "方式 B：点击“选择整个 GDB”，选择 gdb/GeoPublisher_Synthetic_MultiLayer.gdb，\n"
        "检查 3 个 Feature Class，离线转换为 SHP 后走同一个 GeoServer 自动发布流程。\n"
        "方式 C：添加 raster/gp_demo_fictional_rgb.tif 检查独立 GeoTIFF 发布。\n\n"
        "styles 目录有 3 种测试 SLD；如需保留此手绘符号，发布计划中选择样式目录。\n"
        "source 目录是本机生成数据的 GeoJSON/PPM，不要与对应 SHP 重复导入。\n"
        "不要将 SHP 与同源 GDB 一起发布到同一工作空间，否则可能图层同名。\n"
        "建议新建独立测试 Workspace，如 geopub_auto_smoke_20261117；\n"
        "不得重复发布旧的 geopub_acceptance_20261117 里已有的 24 个图层。\n"
        "GeoServer 上传需要管理网页账号，而不是 Linux root/SSH 或数据库账号。\n"
        "内网 HTTP 无 TLS，须明确允许、已获授权；推荐 HTTPS。\n"
        "本机测试图幅目录/文件不可用于真实业务生产。\n",
        encoding="utf-8",
    )
    bundle = destination.parent / (destination.name + ".zip")
    if bundle.exists():
        raise FileExistsError("Bundle already exists")
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for file in sorted(destination.rglob("*")):
            if file.is_file():
                z.write(file, arcname=destination.name + "/" + file.relative_to(destination).as_posix())
    with zipfile.ZipFile(bundle) as z:
        if z.testzip():
            raise RuntimeError("Synthetic ZIP CRC verification failed")
    print("SYNTHETIC_FIXTURES_CREATED=" + str(destination), flush=True)
    print("SHP_LAYER_COUNT=3", flush=True)
    print("GDB_FEATURE_CLASS_COUNT=3", flush=True)
    print("SYNTHETIC_FEATURE_COUNT=16", flush=True)
    print("BUNDLE_FILE=" + str(bundle), flush=True)
    print("BUNDLE_SHA256=" + hashlib.sha256(bundle.read_bytes()).hexdigest(), flush=True)
    print("REMOTE_GEOSERVER_UPLOAD=NO", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gdal-bin", type=Path, required=True)
    args = parser.parse_args()
    generate(args.output, args.gdal_bin)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"SYNTHETIC_TEST_DATA_ERROR={type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
