"""Math-only GDB -> SHP -> SLD remap -> real WMS classification acceptance.

PREPARE: no network; generates two synthetic colored polygon features and a
reproducible GeoPublisher plan. RUN: uses the same plan against one disposable,
loopback GeoServer clone with a verified local Java process. Never use this
script with real user GIS data or production server credentials.

Only output under the explicitly marked D: validation laboratory is accepted.
Windows-specific runtime guard deliberately fails closed on other platforms.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from geopublisher.config import Environment
from geopublisher.datasets import GdalRuntime
from geopublisher.gdb_export import convert_gdb, gdb_fingerprint
from geopublisher.plans import build_plan, digest_file, load_plan, save_json
from geopublisher.verification import verify_image

_LAYER = "SYNTHETIC_CLASS"
_GDB_FIELD = "LongFormationCategory"
_LEFT = "#D43B36"
_RIGHT = "#2762CE"
_PORT = 18884
_WORKSPACE = "gp_sld_class_01"
_BBOX = [109.8, 28.8, 111.2, 30.2]
_EXPECTED_RGB = [(212, 59, 54), (39, 98, 206)]
_POINTS = [(163, 256), (349, 256)]


def _allowed_lab(lab: Path) -> Path:
    path = lab.absolute()
    if (
        os.name != "nt"
        or path.name != "synthetic-sld-lab-20261102"
        or path.parent.name != "GeoPublisher-Workbench-validation"
        or path.drive.upper() != "D:"
        or path.is_symlink()
        or any(parent.is_symlink() for parent in path.parents)
    ):
        raise ValueError("This acceptance only permits the new D: synthetic GeoPublisher lab.")
    return path


def sample_geojson() -> dict:
    """Two non-overlapping, deterministic polygons with different class values."""
    def polygon(x0: float, x1: float) -> dict:
        return {
            "type": "Polygon",
            "coordinates": [[[x0, 29.2], [x1, 29.2], [x1, 29.8],
                             [x0, 29.8], [x0, 29.2]]],
        }
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {_GDB_FIELD: "LEFT", "中文标签": "合成甲"},
             "geometry": polygon(110.1, 110.42)},
            {"type": "Feature", "properties": {_GDB_FIELD: "RIGHT", "中文标签": "合成乙"},
             "geometry": polygon(110.58, 110.9)},
        ],
    }


def original_class_sld() -> str:
    """Original field reference intentionally uses long GDB field name."""
    rules = []
    for name, rgb in (("LEFT", _LEFT), ("RIGHT", _RIGHT)):
        rules.append(
            "<sld:Rule><sld:Title>synthetic-" + name + "</sld:Title>"
            "<ogc:Filter><ogc:PropertyIsEqualTo>"
            "<ogc:PropertyName>" + _GDB_FIELD + "</ogc:PropertyName>"
            "<ogc:Literal>" + name + "</ogc:Literal>"
            "</ogc:PropertyIsEqualTo></ogc:Filter>"
            '<sld:PolygonSymbolizer><sld:Fill><sld:CssParameter name="fill">'
            + rgb +
            '</sld:CssParameter></sld:Fill><sld:Stroke>'
            '<sld:CssParameter name="stroke">' + rgb +
            '</sld:CssParameter><sld:CssParameter name="stroke-width">1</sld:CssParameter>'
            "</sld:Stroke></sld:PolygonSymbolizer></sld:Rule>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sld:StyledLayerDescriptor version="1.0.0"'
        ' xmlns:sld="http://www.opengis.net/sld"'
        ' xmlns:ogc="http://www.opengis.net/ogc">'
        "<sld:NamedLayer><sld:Name>" + _LAYER + "</sld:Name>"
        "<sld:UserStyle><sld:Title>synthetic-classification</sld:Title>"
        "<sld:FeatureTypeStyle>" + "".join(rules) + "</sld:FeatureTypeStyle>"
        "</sld:UserStyle></sld:NamedLayer></sld:StyledLayerDescriptor>"
    )


def _gdal_translate_to_gdb(runtime: GdalRuntime, geojson: Path, out: Path) -> None:
    binary = runtime.bin_dir / "ogr2ogr.exe"
    args = [str(binary), "-f", "OpenFileGDB", str(out), str(geojson), "-nln", _LAYER]
    result = subprocess.run(
        args, capture_output=True, timeout=100, check=False,
        env=runtime._environment(), shell=False,
    )
    if result.returncode:
        raise RuntimeError("Pinned GDAL could not generate the mathematical FileGDB fixture.")


def prepare(lab: Path, gdal_bin: Path) -> dict:
    lab = _allowed_lab(lab)
    if lab.exists():
        raise ValueError("The synthetic acceptance directory must be completely new.")
    runtime = GdalRuntime(gdal_bin, expected_version="3.13.3")
    capabilities = runtime.probe()
    lab.mkdir(parents=True, exist_ok=False)
    (lab / "SYNTHETIC-ONLY.txt").write_text(
        "Mathematically generated polygons only. Never use real GIS data.\n", encoding="utf-8"
    )
    input_geojson = lab / "two-synthetic-polygons.geojson"
    input_geojson.write_text(json.dumps(sample_geojson(), ensure_ascii=False), encoding="utf-8")
    source = lab / "synthetic-original.gdb"
    _gdal_translate_to_gdb(runtime, input_geojson, source)
    source_sha_before = gdb_fingerprint(source)
    converted = lab / "converted"
    report = convert_gdb(runtime, source, converted, only_layers=[_LAYER])
    successes = [row for row in report["layers"] if row["status"].startswith("EXPORTED")]
    if len(successes) != 1 or successes[0].get("output_feature_count") != 2:
        raise RuntimeError("GDB to SHP conversion of two mathematical polygons did not succeed.")
    field_mapping = next(
        row for row in successes[0]["field_map"] if row["original"] == _GDB_FIELD
    )
    short_field = field_mapping["shp_field"]
    if short_field == _GDB_FIELD or not field_mapping["renamed"]:
        raise RuntimeError("Expected long-field truncation did not occur.")
    sld_dir = lab / "sld"
    sld_dir.mkdir()
    sld = sld_dir / (_LAYER + ".sld")
    sld.write_text(original_class_sld(), encoding="utf-8")
    sld_source_sha = digest_file(sld)
    shp = converted / (_LAYER + ".shp")
    shp_sha = digest_file(shp)
    env = Environment(
        environment_key="disposable-classification",
        base_url=f"http://127.0.0.1:{_PORT}/geoserver",
        workspace=_WORKSPACE,
        gdal_bin=str(Path(gdal_bin).absolute()),
        gdal_version="3.13.3",
        timeout=60,
    )
    plan = build_plan(
        env, [shp], lab / "publish", sld_directory=sld_dir,
        remap_sld_fields=True, strategy="new-version",
    )
    if not plan["ready"] or len(plan["resources"]) != 1:
        raise RuntimeError("Frozen classification plan has blocking errors.")
    item = plan["resources"][0]
    audit = item["sld_field_rewrite"]
    if audit["changes"] != [{"original": _GDB_FIELD, "shp_field": short_field}]:
        raise RuntimeError("The SLD field reference was not rewritten exactly once.")
    if digest_file(shp) != shp_sha or digest_file(sld) != sld_source_sha:
        raise RuntimeError("The source SHP or SLD was modified by plan creation.")
    if gdb_fingerprint(source) != source_sha_before:
        raise RuntimeError("The mathematical GDB was modified.")
    summary = {
        "stage": "PREPARED_OFFLINE",
        "data_origin": "mathematical-polygons-only",
        "gdal_version": capabilities["version"],
        "feature_count": 2,
        "original_field": _GDB_FIELD,
        "converted_field": short_field,
        "source_gdb_sha256": source_sha_before,
        "source_shp_sha256": shp_sha,
        "source_sld_sha256": sld_source_sha,
        "plan_hash": plan["plan_hash"],
        "target_layer": item["target_name"],
        "frozen_style_sha256": audit["mapped_sld_sha256"],
        "requests_to_server": 0,
        "source_unchanged": True,
    }
    save_json(lab / "prepare-result.json", summary)
    return summary



def _safe_server(lab: Path, server: Path) -> None:
    """Confirm the *actual Java listener* owns this disposable WMS endpoint."""
    lab = _allowed_lab(lab)
    server = server.absolute()
    if (
        not (lab / "SYNTHETIC-ONLY.txt").is_file()
        or server.name != "geoserver-disposable-classification-20261102"
        or server.parent != lab
        or not (server / "DISPOSABLE-ONLY.txt").is_file()
        or not (server / "server-wms.pid").is_file()
    ):
        raise ValueError("Disposable WMS catalog and synthetic marker required.")
    pid = int((server / "server-wms.pid").read_text(encoding="ascii").strip())
    if not 0 < pid < 2**31:
        raise ValueError("Invalid disposable server PID.")
    command = (
        "$ErrorActionPreference='Stop';"
        f"$l=@(Get-NetTCPConnection -LocalPort {_PORT} -State Listen -ErrorAction SilentlyContinue);"
        f"if($l.Count -ne 1 -or $l[0].LocalAddress -ne '127.0.0.1' -or $l[0].OwningProcess -ne {pid})"
        "{exit 3};"
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}';"
        "if(-not $p -or $p.Name -notmatch '^java(w)?[.]exe$' -or "
        "$p.CommandLine -notlike '*geoserver-disposable-classification-20261102*'){exit 4}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, timeout=15, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Could not confirm loopback Java process identity.") from None
    if result.returncode:
        raise ValueError("Refusing WMS read from unverified process.")


def pixel_evidence(content: bytes) -> dict:
    """Pixel-semantic evidence; no original WMS imagery is written to Git."""
    verified = verify_image(content, "image/png", expected_size=(512, 512))
    with Image.open(BytesIO(content)) as img:
        rgba = img.convert("RGBA")
        samples = [rgba.getpixel((x, y)) for x, y in _POINTS]
        outside = rgba.getpixel((256, 55))

    def exact_color(actual: tuple[int, ...], expected: tuple[int, ...]) -> bool:
        return actual[3] == 255 and all(abs(a - b) <= 6 for a, b in zip(actual[:3], expected))

    return {
        "wms_status": verified["status"],
        "png_sha256": verified["sha256"],
        "left_rgba": list(samples[0]),
        "right_rgba": list(samples[1]),
        "background_rgba": list(outside),
        "both_class_colors_match": all(
            exact_color(color, expected)
            for color, expected in zip(samples, _EXPECTED_RGB)
        ),
        "outside_is_transparent": outside[3] == 0,
        "visible_pixels": verified["visible_pixels"],
    }


def verify_wms_readonly(lab: Path) -> dict:
    """No auth, no REST writes. This is not an end-to-end publication test."""
    lab = _allowed_lab(lab)
    server = lab / "geoserver-disposable-classification-20261102"
    _safe_server(lab, server)
    prepared = json.loads((lab / "prepare-result.json").read_text(encoding="utf-8"))
    if prepared.get("data_origin") != "mathematical-polygons-only":
        raise ValueError("Unknown test data origin.")
    if (
        gdb_fingerprint(lab / "synthetic-original.gdb") != prepared["source_gdb_sha256"]
        or digest_file(lab / "converted" / (_LAYER + ".shp")) != prepared["source_shp_sha256"]
        or digest_file(lab / "sld" / (_LAYER + ".sld")) != prepared["source_sld_sha256"]
    ):
        raise ValueError("Math-only input changed after preparation.")
    plan = load_plan(lab / "publish" / "plan.json")
    if plan["plan_hash"] != prepared["plan_hash"] or not plan["ready"]:
        raise ValueError("Frozen publication plan has changed.")
    if plan["resources"][0]["sld_field_rewrite"]["changes"] != [
        {"original": _GDB_FIELD, "shp_field": prepared["converted_field"]}
    ]:
        raise ValueError("Unexpected source field remapping.")
    url = f"http://127.0.0.1:{_PORT}/geoserver/topp/wms"
    params = {
        "service": "WMS",
        "request": "GetMap",
        "version": "1.1.1",
        "layers": "topp:SYNTHETIC_CLASS",
        "styles": "",
        "format": "image/png",
        "transparent": "true",
        "srs": "EPSG:4326",
        "bbox": ",".join(str(x) for x in _BBOX),
        "width": "512",
        "height": "512",
    }
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=35) as http:
        caps = http.get(
            url, params={"service": "WMS", "request": "GetCapabilities", "version": "1.1.1"}
        )
        if caps.status_code != 200 or b"SYNTHETIC_CLASS" not in caps.content:
            raise ValueError("Disposable example layer is missing from WMS Capabilities.")
        response = http.get(url, params=params, headers={"Accept": "image/png"})
    if response.status_code != 200:
        raise ValueError("Synthetic WMS returned a non-success response.")
    evidence = pixel_evidence(response.content)
    passed = (
        evidence["wms_status"] == "PASSED"
        and evidence["both_class_colors_match"]
        and evidence["outside_is_transparent"]
    )
    report = {
        "status": "PASSED" if passed else "REVIEW_REQUIRED",
        "test_scope": "stopped-catalog-preparation-then-readonly-WMS",
        "data_origin": "math-generated-two-polygons",
        "server_writes_during_check": 0,
        "authentication_provided": False,
        "field_before": _GDB_FIELD,
        "field_after": prepared["converted_field"],
        "render": evidence,
        "notice": "This validates true WMS rendering, not REST publication or actual LYR conversion.",
    }
    result_path = lab / "read-only-wms-final.json"
    if result_path.exists():
        raise ValueError("Do not overwrite existing synthetic WMS verification.")
    save_json(result_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "verify-readonly"))
    parser.add_argument("--lab", type=Path, required=True)
    parser.add_argument("--gdal-bin", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        if args.gdal_bin is None:
            parser.error("--gdal-bin required for offline math fixture preparation")
        result = prepare(args.lab, args.gdal_bin)
    else:
        result = verify_wms_readonly(args.lab)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.mode == "verify-readonly" and result["status"] != "PASSED":
        raise SystemExit("The synthetic read-only WMS classification requires manual review.")


if __name__ == "__main__":
    main()
