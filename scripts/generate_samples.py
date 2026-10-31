#!/usr/bin/env python3
"""Generate small, explicitly synthetic GIS fixtures using a selected GDAL.

Usage: python scripts/generate_samples.py --gdal-bin /path/to/bin --output data/synthetic
No downloaded or user spatial data is used.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from geopublisher.datasets import GdalRuntime
from geopublisher.styles import write_default_sld


def generate(bin_dir: Path, output: Path) -> dict:
    runtime = GdalRuntime(bin_dir)
    capability = runtime.probe()
    output = output.absolute()
    if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise ValueError("Output must not use symlinks.")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Synthetic output directory must be empty; existing files will not be overwritten.")

    def run(name: str, arguments: list[str]) -> None:
        executable = bin_dir / (name + (".exe" if os.name == "nt" else ""))
        if not executable.is_file():
            raise ValueError(f"Selected GDAL runtime is missing {name}.")
        result = subprocess.run(
            [str(executable), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=runtime._environment(),
            shell=False,
        )
        if result.returncode:
            raise RuntimeError(f"Synthetic fixture generation failed at {name}.")

    shapes = {
        "points_utf8": (
            "Point",
            "UTF-8",
            [
                {"type": "Point", "coordinates": [10.2, 20.2]},
                {"type": "Point", "coordinates": [10.8, 20.7]},
            ],
        ),
        "lines_utf8": (
            "LineString",
            "UTF-8",
            [
                {"type": "LineString", "coordinates": [[10.1, 20.1], [10.6, 20.8], [10.9, 20.3]]},
            ],
        ),
        "polygons_utf8": (
            "Polygon",
            "UTF-8",
            [
                {
                    "type": "Polygon",
                    "coordinates": [[[10.1, 20.1], [10.9, 20.1], [10.9, 20.9], [10.1, 20.9], [10.1, 20.1]]],
                },
            ],
        ),
        "polygons_gbk": (
            "Polygon",
            "GBK",
            [
                {
                    "type": "Polygon",
                    "coordinates": [[[10.2, 20.2], [10.7, 20.2], [10.7, 20.7], [10.2, 20.7], [10.2, 20.2]]],
                },
            ],
        ),
    }
    files = []
    with tempfile.TemporaryDirectory(prefix=".synthetic-input-", dir=output) as temporary:
        staging = Path(temporary)
        for name, (geometry, encoding, geometries) in shapes.items():
            collection = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": item,
                        "properties": {"name": f"合成示例{index + 1}", "class_id": index + 1},
                    }
                    for index, item in enumerate(geometries)
                ],
            }
            geojson = staging / (name + ".geojson")
            geojson.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
            target = output / (name + ".shp")
            run(
                "ogr2ogr",
                [
                    "-f",
                    "ESRI Shapefile",
                    "-a_srs",
                    "EPSG:4326",
                    "-lco",
                    f"ENCODING={encoding}",
                    str(target),
                    str(geojson),
                ],
            )
            style = output / (name + ".sld")
            write_default_sld(style, name=name, geometry_type=geometry)
            files.append(
                {"source": target.name, "style": style.name, "encoding": encoding, "kind": "shapefile"}
            )
        image = staging / "gradient.ppm"
        width = height = 256
        pixels = bytes(
            channel for y in range(height) for x in range(width) for channel in (x, y, (x + y) // 2)
        )
        image.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)
        target = output / "rgb_gradient.tif"
        run(
            "gdal_translate",
            [
                "-of",
                "GTiff",
                "-a_srs",
                "EPSG:4326",
                "-a_ullr",
                "10",
                "21",
                "11",
                "20",
                "-co",
                "TILED=YES",
                "-co",
                "COMPRESS=DEFLATE",
                str(image),
                str(target),
            ],
        )
        run("gdaladdo", ["-r", "average", str(target), "2", "4"])
        style = output / "rgb_gradient.sld"
        write_default_sld(style, name="rgb_gradient", band_count=3)
        files.append({"source": target.name, "style": style.name, "kind": "geotiff"})
    manifest = {
        "synthetic": True,
        "source": "Generated mathematically; no real project data.",
        "gdal_version": capability["version"],
        "crs": "EPSG:4326",
        "resources": files,
    }
    (output / "synthetic-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gdal-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.gdal_bin.resolve(), args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
