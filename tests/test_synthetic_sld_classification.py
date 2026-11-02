"""No-network proof of the generated classification WMS acceptance tooling."""

from __future__ import annotations

import importlib.util
import io
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image, ImageDraw


def _load_script(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location("math_wms_fixture_" + name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOURCE = _load_script("integration_sld_classification.py")
CATALOG = _load_script("prepare_disposable_sld_catalog.py")


def _png(*, swapped=False, outside_opaque=False):
    image = Image.new("RGBA", (512, 512), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    left = (39, 98, 206, 255) if swapped else (212, 59, 54, 255)
    right = (212, 59, 54, 255) if swapped else (39, 98, 206, 255)
    draw.rectangle((120, 200, 210, 310), fill=left)
    draw.rectangle((299, 200, 390, 310), fill=right)
    if outside_opaque:
        image.putpixel((256, 55), (0, 0, 0, 255))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_math_only_two_polygon_sources_have_expected_classes_and_geometry():
    data = SOURCE.sample_geojson()
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 2
    fields = [item["properties"][SOURCE._GDB_FIELD] for item in data["features"]]
    assert fields == ["LEFT", "RIGHT"]
    assert all(item["geometry"]["type"] == "Polygon" for item in data["features"])
    assert all(len(item["geometry"]["coordinates"][0]) == 5 for item in data["features"])
    assert all("合成" in item["properties"]["中文标签"] for item in data["features"])


def test_original_sld_has_two_filters_and_will_require_field_rewriting(tmp_path):
    from geopublisher.styles import OGC, SLD, validate_sld

    path = tmp_path / "synthetic.sld"
    path.write_text(SOURCE.original_class_sld(), encoding="utf-8")
    assert validate_sld(path)["valid"] is True
    root = ET.parse(path).getroot()
    rules = root.findall(".//{" + SLD + "}Rule")
    names = [node.text for node in root.findall(".//{" + OGC + "}PropertyName")]
    classes = [node.text for node in root.findall(".//{" + OGC + "}Literal")]
    assert len(rules) == 2
    assert names == ["LongFormationCategory", "LongFormationCategory"]
    assert classes == ["LEFT", "RIGHT"]
    assert SOURCE._LEFT in path.read_text(encoding="utf-8")
    assert SOURCE._RIGHT in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("swapped", "outside_opaque", "expected_correct", "expected_background"),
    [
        (False, False, True, True),
        (True, False, False, True),
        (False, True, True, False),
    ],
)
def test_real_wms_pixel_acceptance_requires_correct_position_and_transparency(
    swapped, outside_opaque, expected_correct, expected_background
):
    proof = SOURCE.pixel_evidence(_png(swapped=swapped, outside_opaque=outside_opaque))
    assert proof["wms_status"] == "PASSED"
    assert proof["both_class_colors_match"] is expected_correct
    assert proof["outside_is_transparent"] is expected_background
    assert proof["left_rgba"][3] == 255
    assert proof["right_rgba"][3] == 255


def _templates():
    store = (
        b"<dataStore><id>old-store</id><name>old</name><workspace>"
        b"<id>topp</id></workspace><connectionParameters>"
        b'<entry key="namespace">http://www.openplans.org/topp</entry>'
        b'<entry key="url">file:data/shapefiles/states.shp</entry>'
        b"</connectionParameters></dataStore>"
    )
    feature = (
        b"<featureType><id>old-feature</id><name>states</name><nativeName>states</nativeName>"
        b"<title>USA</title><abstract>map</abstract><store><id>old-store</id></store>"
        b"<srs>EPSG:4326</srs>"
        b"<nativeBoundingBox><minx>0</minx><maxx>1</maxx><miny>0</miny><maxy>1</maxy>"
        b"<crs>EPSG:4326</crs></nativeBoundingBox>"
        b"<latLonBoundingBox><minx>0</minx><maxx>1</maxx><miny>0</miny><maxy>1</maxy>"
        b"<crs>EPSG:4326</crs></latLonBoundingBox>"
        b"<attributes><attribute><name>the_geom</name></attribute></attributes></featureType>"
    )
    layer = (
        b"<layer><id>old-layer</id><name>states</name><resource><id>old-feature</id>"
        b"</resource><defaultStyle><id>old-style</id></defaultStyle>"
        b"<styles><style><id>old-extra</id></style></styles></layer>"
    )
    return store, feature, layer


def test_stopped_catalog_template_rewrites_only_generated_layer_references():
    store, feature, layer, style = CATALOG.catalog_documents(
        *_templates(), "a" * 64, ["LONGFO_E65", "F_A12"]
    )
    ds, feat, layer_info, style_info = [ET.fromstring(part) for part in (store, feature, layer, style)]
    assert ds.findtext("name") == "gp_test_classification"
    assert ds.findtext("connectionParameters/entry[@key='url']") == (
        "file:data/gp_test_classification/SYNTHETIC_CLASS.shp"
    )
    assert feat.findtext("nativeName") == layer_info.findtext("name") == "SYNTHETIC_CLASS"
    assert feat.findtext("store/id") == ds.findtext("id")
    assert layer_info.findtext("resource/id") == feat.findtext("id")
    assert layer_info.findtext("defaultStyle/id") == style_info.findtext("id")
    assert [a.findtext("name") for a in feat.findall("attributes/attribute")] == [
        "the_geom", "LONGFO_E65", "F_A12"
    ]
    assert not layer_info.findall("styles/style")
    assert feat.findtext("latLonBoundingBox/minx") == "110.1"
    assert feat.findtext("latLonBoundingBox/maxy") == "29.8"


def test_test_harness_refuses_non_disposable_locations(tmp_path):
    with pytest.raises(ValueError):
        SOURCE._allowed_lab(tmp_path)
    with pytest.raises(ValueError):
        CATALOG._validated_lab(tmp_path)
    with pytest.raises(ValueError):
        SOURCE.prepare(tmp_path, tmp_path / "gdal")
    with pytest.raises(ValueError):
        CATALOG.catalog_documents(*_templates(), "not-a-sha256", ["field"])


def test_wms_harness_does_not_contain_rest_write_mode():
    """Running the examples must never accidentally use mutating API paths."""
    source = Path(SOURCE.__file__).read_text(encoding="utf-8")
    assert 'choices=("prepare", "verify-readonly")' in source
    assert "run_plan(" not in source
    assert "create_workspace(" not in source
    assert "rollback_plan(" not in source
