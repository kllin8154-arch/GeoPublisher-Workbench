"""Prepare a disposable GeoServer raster catalog with ONLY mathematical TIFF data.

No HTTP, no credentials, no writes to an existing GeoServer. Copies an
existing *test* distribution and adds two synthetic read-only WMS layers
before the new server starts. The output is never reused or overwritten.
This is a controlled test harness, NOT a GeoServer data_dir product feature.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from geopublisher.plans import digest_file, save_json

_NAMES = (
    ("synthetic_nodata", "nodata-gray.tif", ("GRAY_INDEX",)),
    ("synthetic_alpha", "alpha-rgba.tif", ("Red band", "Green band", "Blue band", "Alpha band")),
)
_BOUNDS = (110.0, 29.0, 111.0, 30.0)
_PIXELS = 128


def _set(root: ET.Element, path: str, value: object) -> None:
    node = root.find(path)
    if node is None:
        raise ValueError(f"Disposable GeoServer template is missing {path!r}.")
    node.text = str(value)


def _identifier(kind: str, name: str) -> str:
    code = hashlib.sha256(("GeoPublisher::disposable::" + kind + "::" + name).encode()).hexdigest()[:24]
    return f"{kind}InfoImpl--gp-synthetic-{code}"


def catalog_xml(
    store_xml: bytes, coverage_xml: bytes, layer_xml: bytes, name: str, filename: str, bands: tuple[str, ...],
) -> tuple[bytes, bytes, bytes]:
    """Create isolated store/coverage/layer XML from an existing sample template."""
    if (name, filename, bands) not in _NAMES:
        raise ValueError("Only the two explicitly generated synthetic fixtures are supported.")
    store = ET.fromstring(store_xml)
    coverage = ET.fromstring(coverage_xml)
    layer = ET.fromstring(layer_xml)
    store_id = _identifier("CoverageStore", name)
    coverage_id = _identifier("Coverage", name)
    _set(store, "id", store_id)
    _set(store, "name", name)
    _set(store, "description", "Disposable mathematical GeoTIFF WMS test")
    _set(store, "type", "GeoTIFF")
    _set(store, "url", "file:data/nurc/" + filename)
    _set(coverage, "id", coverage_id)
    _set(coverage, "name", name)
    _set(coverage, "nativeName", Path(filename).stem)
    _set(coverage, "title", "Disposable synthetic: " + name)
    _set(coverage, "description", "Mathematically generated, no real user data")
    _set(coverage, "store/id", store_id)
    _set(coverage, "nativeFormat", "GeoTIFF")
    _set(coverage, "metadata/entry", name + "_" + name)
    _set(coverage, "srs", "EPSG:4326")
    _set(coverage, "nativeBoundingBox/crs", "EPSG:4326")
    _set(coverage, "latLonBoundingBox/crs", "EPSG:4326")
    x0, y0, x1, y1 = _BOUNDS
    for section in ("nativeBoundingBox", "latLonBoundingBox"):
        for tag, value in (("minx", x0), ("miny", y0), ("maxx", x1), ("maxy", y1)):
            _set(coverage, f"{section}/{tag}", value)
    _set(coverage, "grid/range/high", f"{_PIXELS - 1} {_PIXELS - 1}")
    _set(coverage, "grid/crs", "EPSG:4326")
    _set(coverage, "grid/transform/scaleX", (x1 - x0) / _PIXELS)
    _set(coverage, "grid/transform/scaleY", -(y1 - y0) / _PIXELS)
    _set(coverage, "grid/transform/translateX", x0 + (x1 - x0) / (2 * _PIXELS))
    _set(coverage, "grid/transform/translateY", y1 - (y1 - y0) / (2 * _PIXELS))
    dimensions = coverage.find("dimensions")
    if dimensions is None:
        raise ValueError("Template is missing coverage dimensions.")
    dimensions.clear()
    for band in bands:
        dimension = ET.SubElement(dimensions, "coverageDimension")
        ET.SubElement(dimension, "name").text = band
        ET.SubElement(dimension, "description").text = "Synthetic [0,255]"
        range_node = ET.SubElement(dimension, "range")
        ET.SubElement(range_node, "min").text = "0.0"
        ET.SubElement(range_node, "max").text = "255.0"
    _set(layer, "id", _identifier("Layer", name))
    _set(layer, "name", name)
    _set(layer, "resource/id", coverage_id)
    # The GeoServer demo's global "raster" style is used; no persistent
    # style creation is needed in the test server.
    def xml_bytes(root: ET.Element) -> bytes:
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    return xml_bytes(store), xml_bytes(coverage), xml_bytes(layer)


def prepare(template: Path, samples: Path, destination: Path) -> dict:
    """Create a fresh test-only clone; the source tree is never modified."""
    template = template.resolve(strict=True)
    samples = samples.resolve(strict=True)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Refusing to overwrite an existing test instance.")
    if (
        not destination.name.startswith("geoserver-disposable-raster-")
        or destination.parent.name != "GeoPublisher-Workbench-validation"
        or not destination.parent.is_dir()
        or not (template / "start.jar").is_file()
        or not (template / "data_dir" / "workspaces" / "nurc" / "worldImageSample").is_dir()
    ):
        raise ValueError("Only a fresh disposable test GeoServer under the validation root is allowed.")
    if destination.is_relative_to(template) or destination.is_relative_to(samples):
        raise ValueError("Refusing a destination inside test input directories.")
    if any(path.is_symlink() for path in (template, samples, destination.parent)):
        raise ValueError("Symlinked test roots are not supported.")
    manifest = json.loads((samples / "result.json").read_text(encoding="utf-8"))
    if (
        manifest.get("status") != "LOCAL_GDAL_VALIDATION_ONLY"
        or manifest.get("data_origin") != "mathematically-generated"
        or manifest.get("gdal") != "3.13.3"
        or manifest.get("server_requests") != 0
    ):
        raise ValueError("Mathematical synthetic input manifest is invalid.")
    for name, filename, _ in _NAMES:
        source = samples / filename
        record = manifest["nodata" if name == "synthetic_nodata" else "rgba"]
        if source.is_symlink() or not source.is_file() or digest_file(source) != record["sha256"]:
            raise ValueError("Synthetic GeoTIFF hash does not match its local validation manifest.")
    original = template / "data_dir" / "workspaces" / "nurc" / "worldImageSample"
    inputs = tuple(
        (original / part).read_bytes()
        for part in ("coveragestore.xml", "Img_Sample/coverage.xml", "Img_Sample/layer.xml")
    )
    prepared = [
        (name, filename, catalog_xml(*inputs, name, filename, bands))
        for name, filename, bands in _NAMES
    ]
    shutil.copytree(template, destination)
    target_data = destination / "data_dir" / "data" / "nurc"
    target_data.mkdir(parents=True, exist_ok=True)
    target_workspace = destination / "data_dir" / "workspaces" / "nurc"
    records = []
    for name, filename, (store_xml, coverage_xml, layer_xml) in prepared:
        target_file = target_data / filename
        if target_file.exists():
            raise ValueError("The cloned GeoServer already contains the synthetic file name.")
        shutil.copy2(samples / filename, target_file)
        target = target_workspace / name
        target.mkdir(exist_ok=False)
        (target / "coveragestore.xml").write_bytes(store_xml)
        coverage = target / name
        coverage.mkdir()
        (coverage / "coverage.xml").write_bytes(coverage_xml)
        (coverage / "layer.xml").write_bytes(layer_xml)
        records.append(
            {"qualified_name": f"nurc:{name}", "file_sha256": digest_file(target_file), "filename": filename}
        )
    (destination / "DISPOSABLE-ONLY.txt").write_text(
        "DO NOT DEPLOY. Only use with 127.0.0.1 binding. Synthetic GIS data.\n", encoding="utf-8"
    )
    summary = {
        "status": "DISPOSABLE_CATALOG_PREPARED_OFFLINE",
        "source": "mathematically-generated",
        "workspace": "nurc",
        "server_writes": 0,
        "published_over_rest": False,
        "template_distribution": "GeoServer 2.24.4 local test copy",
        "layers": records,
    }
    save_json(destination / "prepared-catalog.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.template, args.samples, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
