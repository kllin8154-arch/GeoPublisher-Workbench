"""Stage synthetic SHP and frozen, remapped SLD in a STOPPED GeoServer clone.

This is a test fixture generator, not a product feature or REST publishing mode.
Only the local D: test lab is accepted. Absolutely no real GIS user files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import xml.etree.ElementTree as ET
from pathlib import Path

_LAYER = "SYNTHETIC_CLASS"
_PORT = 18884


def _validated_lab(lab: Path) -> Path:
    path = lab.absolute()
    if (
        os.name != "nt"
        or path.drive.upper() != "D:"
        or path.name != "synthetic-sld-lab-20261102"
        or path.parent.name != "GeoPublisher-Workbench-validation"
        or path.is_symlink()
        or any(parent.is_symlink() for parent in path.parents)
        or not (path / "SYNTHETIC-ONLY.txt").is_file()
    ):
        raise ValueError("Stopped-catalog preparation requires the explicit mathematical lab.")
    return path


def catalog_documents(
    store_bytes: bytes, feature_bytes: bytes, layer_bytes: bytes,
    style_sha: str, field_names: list[str],
) -> tuple[bytes, bytes, bytes, bytes]:
    """Pure deterministic conversion of GeoServer demo XML templates."""
    if len(style_sha) != 64 or not all(ch in "0123456789abcdef" for ch in style_sha):
        raise ValueError("Invalid frozen test style SHA256")
    if not field_names or any(not isinstance(name, str) or not name for name in field_names):
        raise ValueError("Invalid synthetic DBF schema")
    store = ET.fromstring(store_bytes)
    feature = ET.fromstring(feature_bytes)
    layer = ET.fromstring(layer_bytes)
    store_id = "DataStoreInfoImpl--gp-synthetic-sld-20261102"
    feature_id = "FeatureTypeInfoImpl--gp-synthetic-sld-20261102"
    layer_id = "LayerInfoImpl--gp-synthetic-sld-20261102"
    style_id = "StyleInfoImpl--gp-synthetic-sld-20261102"

    def update(root: ET.Element, key: str, value: str) -> None:
        node = root.find(key)
        if node is None:
            raise ValueError("GeoServer test template missing expected " + key)
        node.text = value

    update(store, "id", store_id)
    update(store, "name", "gp_test_classification")
    update(store, "connectionParameters/entry[@key='url']",
           "file:data/gp_test_classification/" + _LAYER + ".shp")
    update(store, "connectionParameters/entry[@key='namespace']", "http://www.openplans.org/topp")
    for key, value in (
        ("id", feature_id), ("name", _LAYER), ("nativeName", _LAYER),
        ("title", "MATHEMATICAL TEST - two classified polygons"),
        ("abstract", "Mathematical polygons, no production GIS data."),
        ("store/id", store_id), ("srs", "EPSG:4326"),
    ):
        update(feature, key, value)
    for section in ("nativeBoundingBox", "latLonBoundingBox"):
        for key, value in (
            ("minx", "110.1"), ("maxx", "110.9"),
            ("miny", "29.2"), ("maxy", "29.8"), ("crs", "EPSG:4326"),
        ):
            update(feature, section + "/" + key, value)
    attrs = feature.find("attributes")
    if attrs is None:
        raise ValueError("Missing featureType attributes template")
    attrs.clear()
    for field in ["the_geom", *field_names]:
        item = ET.SubElement(attrs, "attribute")
        ET.SubElement(item, "name").text = field
        ET.SubElement(item, "minOccurs").text = "0"
        ET.SubElement(item, "maxOccurs").text = "1"
        ET.SubElement(item, "nillable").text = "true"
    for key, value in (
        ("id", layer_id), ("name", _LAYER),
        ("resource/id", feature_id), ("defaultStyle/id", style_id),
    ):
        update(layer, key, value)
    styles = layer.find("styles")
    if styles is not None:
        styles.clear()
    style = ET.Element("style")
    ET.SubElement(style, "id").text = style_id
    ET.SubElement(style, "name").text = "gp_synthetic_field_classification"
    ET.SubElement(style, "filename").text = "gp_synthetic_field_classification.sld"
    def encode(root: ET.Element) -> bytes:
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    return encode(store), encode(feature), encode(layer), encode(style)


def prepare(lab: Path) -> dict:
    """Clone must already be stopped; never overwrite or alter reference data."""
    lab = _validated_lab(lab)
    server = lab / "geoserver-disposable-classification-20261102"
    data = server / "data_dir"
    if not (server / "DISPOSABLE-ONLY.txt").is_file():
        raise ValueError("Not a disposable GeoServer test directory")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", _PORT))
        except OSError:
            raise ValueError("Refusing to prepare catalog while the WMS port is occupied.") from None
    summary = json.loads((lab / "prepare-result.json").read_text(encoding="utf-8"))
    plan = json.loads((lab / "publish" / "plan.json").read_text(encoding="utf-8"))
    conversion = json.loads((lab / "converted" / "conversion-report.json").read_text(encoding="utf-8"))
    if (
        summary.get("data_origin") != "mathematical-polygons-only"
        or summary.get("plan_hash") != plan.get("plan_hash")
        or not plan.get("ready")
        or len(plan["resources"]) != 1
        or len(conversion["layers"]) != 1
    ):
        raise ValueError("Untrusted synthetic plan or conversion manifest")
    resource = plan["resources"][0]
    audit = resource["sld_field_rewrite"]
    if audit.get("changes") != [{"original": "LongFormationCategory", "shp_field": "LONGFO_E65"}]:
        raise ValueError("Unexpected field mapping, refuse catalog generation")
    frozen = Path(resource["style"]["path"])
    if not frozen.is_file() or hashlib.sha256(frozen.read_bytes()).hexdigest() != summary["frozen_style_sha256"]:
        raise ValueError("Frozen style hash changed after source verification.")
    from geopublisher.plans import verify_artifacts
    verify_artifacts(plan)
    store_path = data / "workspaces" / "topp" / "gp_test_classification"
    data_path = data / "data" / "gp_test_classification"
    sld_path = data / "styles" / "gp_synthetic_field_classification.sld"
    style_xml = data / "styles" / "gp_synthetic_field_classification.xml"
    if any(path.exists() or path.is_symlink() for path in (store_path, data_path, sld_path, style_xml)):
        raise ValueError("Refusing to overwrite existing GeoServer objects.")
    template = data / "workspaces" / "topp" / "states_shapefile"
    source_files = [
        lab / "converted" / (_LAYER + ext)
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg")
    ]
    assert all(item.is_file() and not item.is_symlink() for item in source_files)
    fields = [
        row["shp_field"] for row in conversion["layers"][0]["field_map"]
        if not row["omitted"]
    ]
    outputs = catalog_documents(
        (template / "datastore.xml").read_bytes(),
        (template / "states" / "featuretype.xml").read_bytes(),
        (template / "states" / "layer.xml").read_bytes(),
        summary["frozen_style_sha256"],
        fields,
    )
    store_path.mkdir(parents=False)
    data_path.mkdir(parents=False)
    layer_path = store_path / _LAYER
    layer_path.mkdir()
    (store_path / "datastore.xml").write_bytes(outputs[0])
    (layer_path / "featuretype.xml").write_bytes(outputs[1])
    (layer_path / "layer.xml").write_bytes(outputs[2])
    style_xml.write_bytes(outputs[3])
    shutil.copy2(frozen, sld_path)
    for item in source_files:
        shutil.copy2(item, data_path / item.name)
    result = {
        "status": "STOPPED_CATALOG_READY",
        "scope": "TEST_DATA_DIR_ONLY",
        "data_origin": "mathematically-generated",
        "wms_only_after_start": True,
        "preparation_had_network_requests": False,
        "style_sha256": summary["frozen_style_sha256"],
        "test_layer": "topp:SYNTHETIC_CLASS",
        "manifest": "prepare-result.json",
    }
    from geopublisher.plans import save_json
    save_json(lab / "stopped-catalog-result.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.lab)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
