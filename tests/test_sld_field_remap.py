"""Strict offline SLD field remapping tests: all files are synthetic."""

from __future__ import annotations

import copy
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from geopublisher.config import ConfigError
from geopublisher.sld_field_remap import load_reviewed_field_map, rewrite_sld_field_references
from geopublisher.styles import validate_sld

SLD = "http://www.opengis.net/sld"
OGC = "http://www.opengis.net/ogc"
FES = "http://www.opengis.net/fes/2.0"


def _sha(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


def _synthetic_conversion(tmp_path):
    root = tmp_path / "synthetic-GDB-output"
    root.mkdir()
    name = "_FAULT"
    shp = root / f"{name}.shp"
    output_files = []
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        file = root / (name + suffix)
        file.write_bytes(("synthetic " + suffix).encode())
        output_files.append({"name": file.name, "sha256": _sha(file)})
    mapping = [
        {"original": "LongGeologyName", "shp_field": "LONGGE_ABC", "omitted": False},
        {"original": "中文类型", "shp_field": "F_123", "omitted": False},
        {"original": "Feature_Id", "shp_field": "Feature_Id", "omitted": False},
        {"original": "Element", "shp_field": "ELEMENT", "omitted": True},
    ]
    report = {
        "schema_version": 1,
        "operation": "GDB_TO_SHP_LOCAL_ONLY",
        "source_unchanged": True,
        "gdal_version": "3.13.3",
        "destination": str(root),
        "layers": [
            {
                "source_layer": name,
                "output_basename": name,
                "status": "EXPORTED_REVIEW_REQUIRED",
                "field_map": mapping,
                "output_files": output_files,
            }
        ],
    }
    report_path = root / "conversion-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    fields = [
        {"name": "LONGGE_ABC"},
        {"name": "F_123"},
        {"name": "Feature_Id"},
    ]
    return shp, fields, report_path


def _write_sld(path: Path, property_name: str = "LongGeologyName", *, second="中文类型"):
    value = (
        f'<sld:StyledLayerDescriptor xmlns:sld="{SLD}" xmlns:ogc="{OGC}" '
        f'xmlns:fes="{FES}" version="1.0.0">'
        "<sld:NamedLayer><sld:Name>my-map</sld:Name><sld:UserStyle>"
        "<sld:FeatureTypeStyle><sld:Rule>"
        "<ogc:Filter><ogc:PropertyIsEqualTo>"
        f"<ogc:PropertyName>{property_name}</ogc:PropertyName>"
        "<ogc:Literal>some class</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>"
        "<sld:PolygonSymbolizer/>"
        f"<sld:TextSymbolizer><sld:Label><fes:ValueReference>{second}"
        "</fes:ValueReference></sld:Label></sld:TextSymbolizer>"
        "</sld:Rule></sld:FeatureTypeStyle></sld:UserStyle>"
        "</sld:NamedLayer></sld:StyledLayerDescriptor>"
    )
    path.write_text(value, encoding="utf-8")
    return path


def test_direct_field_refs_rewritten_to_new_plan_copy_only(tmp_path):
    shp, fields, report = _synthetic_conversion(tmp_path)
    style = _write_sld(tmp_path / "qgis-original.sld")
    original_hash = _sha(style)
    mapping = load_reviewed_field_map(shp, fields=fields)
    dest = tmp_path / "frozen-style.sld"
    outcome = rewrite_sld_field_references(style, dest, mapping)
    assert outcome["explicitly_approved"] is True
    assert outcome["original_sld_sha256"] == original_hash == _sha(style)
    assert outcome["mapped_sld_sha256"] == _sha(dest)
    assert outcome["conversion_report_sha256"] == _sha(report)
    assert outcome["source_shp_sha256"] == _sha(shp)
    assert len(outcome["changes"]) == 2
    assert outcome["requires_visual_review"] is True
    assert validate_sld(dest, fields=fields)["valid"]
    root = ET.parse(dest).getroot()
    refs = [
        (node.text or "").strip()
        for node in root.iter()
        if node.tag in {f"{{{OGC}}}PropertyName", f"{{{FES}}}ValueReference"}
    ]
    assert refs == ["LONGGE_ABC", "F_123"]
    assert dest.read_bytes() != style.read_bytes()


def test_stale_report_refuses_mapping_even_if_a_file_was_replaced(tmp_path):
    shp, fields, _report = _synthetic_conversion(tmp_path)
    shp.write_bytes(b"modified synthetic source")
    with pytest.raises(ConfigError) as ex:
        load_reviewed_field_map(shp, fields=fields)
    assert ex.value.code == "FIELD_MAP_SOURCE_CHANGED"


def test_report_not_matching_current_dbf_field_schema_is_rejected(tmp_path):
    shp, fields, _report = _synthetic_conversion(tmp_path)
    changed = copy.deepcopy(fields)
    changed[1]["name"] = "DIFFERENT"
    with pytest.raises(ConfigError) as ex:
        load_reviewed_field_map(shp, fields=changed)
    assert ex.value.code == "FIELD_MAP_FIELDS_CHANGED"


@pytest.mark.parametrize("style_name", ("other/field", "concat(field)", "field[1]"))
def test_xpath_and_complex_references_are_never_guessed(tmp_path, style_name):
    shp, fields, _report = _synthetic_conversion(tmp_path)
    style = _write_sld(tmp_path / "qgis.sld", property_name=style_name)
    mapping = load_reviewed_field_map(shp, fields=fields)
    with pytest.raises(ConfigError) as ex:
        rewrite_sld_field_references(style, tmp_path / "frozen.sld", mapping)
    assert ex.value.code == "SLD_COMPLEX_REFERENCE"
    assert not (tmp_path / "frozen.sld").exists()


def test_unmapped_field_does_not_silently_disappear(tmp_path):
    shp, fields, _report = _synthetic_conversion(tmp_path)
    style = _write_sld(tmp_path / "qgis.sld", property_name="UnrelatedField")
    mapping = load_reviewed_field_map(shp, fields=fields)
    with pytest.raises(ConfigError) as ex:
        rewrite_sld_field_references(style, tmp_path / "frozen.sld", mapping)
    assert ex.value.code == "SLD_FIELD_UNKNOWN"


def test_sld_source_is_not_overwritten_or_reused_in_place(tmp_path):
    shp, fields, _report = _synthetic_conversion(tmp_path)
    style = _write_sld(tmp_path / "qgis.sld")
    mapping = load_reviewed_field_map(shp, fields=fields)
    with pytest.raises(ConfigError) as ex:
        rewrite_sld_field_references(style, style, mapping)
    assert ex.value.code == "SLD_REWRITE_EXISTS"


def test_report_cannot_authorize_omitted_or_ambiguous_columns(tmp_path):
    shp, fields, report_file = _synthetic_conversion(tmp_path)
    report = json.loads(report_file.read_text(encoding="utf-8"))
    report["layers"][0]["field_map"][0]["shp_field"] = "F_123"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ConfigError) as ex:
        load_reviewed_field_map(shp, fields=fields)
    assert ex.value.code == "FIELD_MAP_COLLISION"


def test_malicious_xml_entities_rejected_before_copy(tmp_path):
    shp, fields, _report = _synthetic_conversion(tmp_path)
    style = tmp_path / "qgis.sld"
    style.write_text(
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xx SYSTEM "file:///etc/passwd">]>'
        f'<StyledLayerDescriptor xmlns="{SLD}" version="1.0.0">'
        "<NamedLayer><Name>test</Name><UserStyle><FeatureTypeStyle>"
        "<Rule><PolygonSymbolizer/></Rule></FeatureTypeStyle></UserStyle></NamedLayer>"
        "</StyledLayerDescriptor>",
        encoding="utf-8",
    )
    mapping = load_reviewed_field_map(shp, fields=fields)
    with pytest.raises(ConfigError) as ex:
        rewrite_sld_field_references(style, tmp_path / "frozen.sld", mapping)
    assert ex.value.code == "SLD_REWRITE_UNSAFE"


def test_explicit_remap_requires_style_and_synthetic_shp_only(tmp_path):
    from geopublisher.config import Environment
    from geopublisher.plans import build_plan

    source = tmp_path / "synthetic.tif"
    source.write_bytes(b"dummy synthetic file; no GDAL invoked")
    env = Environment("test", "https://example.invalid/geoserver", "maps", str(tmp_path / "gdal"))
    with pytest.raises(ConfigError) as ex:
        build_plan(env, [source], tmp_path / "a", remap_sld_fields=True)
    assert ex.value.code == "SLD_REMAP_NEEDS_STYLE"
    assert not (tmp_path / "a").exists()
    style = tmp_path / "tiff-style.sld"
    _write_sld(style)
    with pytest.raises(ConfigError) as ex:
        build_plan(env, [source], tmp_path / "b", sld=style,
                   remap_sld_fields=True)
    assert ex.value.code == "SLD_REMAP_SHAPE_ONLY"
    assert not (tmp_path / "b").exists()


def test_function_wrapped_property_is_not_rewritten(tmp_path):
    shp, fields, _report = _synthetic_conversion(tmp_path)
    style = _write_sld(tmp_path / "qgis-function.sld")
    text = style.read_text(encoding="utf-8")
    text = text.replace(
        "<ogc:PropertyName>LongGeologyName</ogc:PropertyName>",
        '<ogc:Function name="strConcat"><ogc:PropertyName>LongGeologyName</ogc:PropertyName>'
        '<ogc:Literal>suffix</ogc:Literal></ogc:Function>',
    )
    style.write_text(text, encoding="utf-8")
    mapping = load_reviewed_field_map(shp, fields=fields)
    with pytest.raises(ConfigError) as ex:
        rewrite_sld_field_references(style, tmp_path / "target.sld", mapping)
    assert ex.value.code == "SLD_COMPLEX_REFERENCE"
    assert not (tmp_path / "target.sld").exists()
