"""Generate/inspect synthetic NoData and Alpha GeoTIFFs with pinned GDAL.

Offline-only. This does not connect to GeoServer or prove WMS rendering.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from PIL import Image

from geopublisher.datasets import GdalRuntime
from geopublisher.plans import digest_file, save_json
from geopublisher.verification import verify_image

_SIZE = 128
_VALID = (32, 32, 96, 96)
_EXTENT = (110.0, 29.0, 111.0, 30.0)


def make_synthetic_inputs(output: Path) -> tuple[Path, Path]:
    """Two deterministic generated images, no downloaded or user files."""
    output.mkdir(parents=True, exist_ok=True)
    pgm = output / "nodata-gray.pgm"
    rgba = output / "alpha-source.png"
    if pgm.exists() or rgba.exists():
        raise ValueError("Refusing to overwrite existing synthetic inputs.")

    samples = bytearray()
    image = Image.new("RGBA", (_SIZE, _SIZE), (230, 45, 90, 0))
    x0, y0, x1, y1 = _VALID
    for y in range(_SIZE):
        for x in range(_SIZE):
            valid = x0 <= x < x1 and y0 <= y < y1
            samples.append(15 + ((x + y) % 240) if valid else 0)
            if valid:
                alpha = 96 if x < 36 or y < 36 or x >= 92 or y >= 92 else 255
                image.putpixel((x, y), ((2 * x) % 255, (2 * y) % 255, 50, alpha))
    pgm.write_bytes(f"P5\n{_SIZE} {_SIZE}\n255\n".encode("ascii") + samples)
    image.save(rgba, format="PNG")
    return pgm, rgba


def generate_gdal_fixtures(bin_dir: Path, output: Path) -> dict:
    bin_dir = bin_dir.resolve()
    output = output.absolute()
    if output.exists():
        raise ValueError("Choose a new output folder; nothing will be overwritten.")
    if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise ValueError("Symlinked output paths are not supported.")
    runtime = GdalRuntime(bin_dir, expected_version="3.13.3")
    capability = runtime.probe()
    output.mkdir(parents=True, exist_ok=False)
    pgm, rgba_png = make_synthetic_inputs(output)
    src_hash = {str(path): digest_file(path) for path in (pgm, rgba_png)}

    def convert(source: Path, target: Path, *, nodata: bool) -> None:
        binary = bin_dir / ("gdal_translate.exe" if os.name == "nt" else "gdal_translate")
        args = [
            "-of", "GTiff",
            "-a_srs", "EPSG:4326",
            "-a_ullr", "110", "30", "111", "29",
            "-co", "TILED=YES",
            "-co", "COMPRESS=DEFLATE",
        ]
        if nodata:
            args.extend(("-a_nodata", "0"))
        args.extend((str(source), str(target)))
        result = subprocess.run(
            [str(binary), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=runtime._environment(),
            shell=False,
        )
        if result.returncode:
            raise RuntimeError("Pinned GDAL failed to create a synthetic raster.")

    gray = output / "nodata-gray.tif"
    alpha = output / "alpha-rgba.tif"
    convert(pgm, gray, nodata=True)
    convert(rgba_png, alpha, nodata=False)
    gray_info = runtime.inspect(gray)
    alpha_info = runtime.inspect(alpha)
    if gray_info["blocking_errors"] or alpha_info["blocking_errors"]:
        raise AssertionError("Synthetic input should not require external auxiliary resources.")
    gray_bands = gray_info["raster"]["bands"]
    alpha_bands = alpha_info["raster"]["bands"]
    if len(gray_bands) != 1 or gray_bands[0].get("noDataValue") != 0:
        raise AssertionError("NoData=0 was not preserved by native GDAL.")
    if len(alpha_bands) != 4 or alpha_bands[3].get("colorInterpretation") != "Alpha":
        raise AssertionError("The explicit RGBA alpha channel was not preserved.")
    for info in (gray_info, alpha_info):
        if info["crs"] != "EPSG:4326":
            raise AssertionError("The synthetic GeoTIFF CRS is unexpected.")
        actual = info["wgs84_bbox"]
        if actual is None or any(abs(a - b) > 1e-8 for a, b in zip(actual, _EXTENT)):
            raise AssertionError("The synthetic GeoTIFF geographic bounds are unexpected.")
    if any(digest_file(path) != original for path, original in ((pgm, src_hash[str(pgm)]), (rgba_png, src_hash[str(rgba_png)]))):
        raise AssertionError("Raster inspection modified a synthetic source image.")

    # This PNG is derived from the mathematical source, NOT a GeoServer map.
    valid_mask = Image.open(rgba_png).resize((512, 512), resample=Image.Resampling.NEAREST)
    png_path = output / "local-alpha-preview.png"
    valid_mask.save(png_path, format="PNG")
    evidence = verify_image(png_path.read_bytes(), "image/png", expected_size=(512, 512))
    if evidence["status"] != "PASSED" or evidence["transparent_fraction"] < 0.4:
        raise AssertionError("Local synthetic alpha visualization was misclassified.")
    return {
        "status": "LOCAL_GDAL_VALIDATION_ONLY",
        "data_origin": "mathematically-generated",
        "server_requests": 0,
        "gdal": capability["version"],
        "full_geographic_bbox": _EXTENT,
        "synthetic_valid_data_window_hint": [110.25, 29.25, 110.75, 29.75],
        "nodata": {"band_count": 1, "noDataValue": gray_bands[0]["noDataValue"], "sha256": digest_file(gray)},
        "rgba": {
            "band_count": 4,
            "alpha_band": alpha_bands[3]["band"],
            "color_interpretation": alpha_bands[3]["colorInterpretation"],
            "sha256": digest_file(alpha),
        },
        "local_png_only": {
            "sha256": evidence["sha256"],
            "visible_pixels": evidence["visible_pixels"],
            "transparent_fraction": evidence["transparent_fraction"],
        },
        "geoserver_nodata_alpha_tested": False,
        "warning": "NoData/Alpha source metadata is verified; true GeoServer WMS rendering remains untested.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gdal-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = generate_gdal_fixtures(args.gdal_bin, args.output)
    save_json(args.output / "result.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
