from __future__ import annotations

from pathlib import Path

import pytest

from geopublisher.styles import StyleError, validate_sld, write_default_sld


def wrap(inner: str, *, version="1.0.0") -> str:
    return f'''<StyledLayerDescriptor xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc" xmlns:xlink="http://www.w3.org/1999/xlink" version="{version}">
    <NamedLayer><Name>example</Name><UserStyle><FeatureTypeStyle><Rule>{inner}</Rule></FeatureTypeStyle></UserStyle></NamedLayer>
    </StyledLayerDescriptor>'''


def put(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "style.sld"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "geometry", ["Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"]
)
def test_default_vector_styles_are_valid_and_deterministic(tmp_path, geometry):
    path = tmp_path / f"{geometry}.sld"
    write_default_sld(path, name='测试 <图层> "一"', geometry_type=geometry)
    before = path.read_bytes()
    assert validate_sld(path)["valid"]
    write_default_sld(path, name='测试 <图层> "一"', geometry_type=geometry)
    assert path.read_bytes() == before
    assert b"&lt;" in before


@pytest.mark.parametrize("bands", [1, 2, 3, 4])
def test_default_raster_styles(tmp_path, bands):
    path = write_default_sld(tmp_path / "raster.sld", name="raster", band_count=bands)
    report = validate_sld(path)
    assert report["valid"]
    assert report["symbolizers"] == ["RasterSymbolizer"]


def test_missing_field_is_reported_exactly(tmp_path):
    path = put(
        tmp_path,
        wrap(
            "<TextSymbolizer><Label><ogc:PropertyName>missing_name</ogc:PropertyName></Label></TextSymbolizer>"
        ),
    )
    report = validate_sld(path, fields=[{"name": "name", "type": "String"}])
    assert not report["valid"]
    assert report["field_references"] == ["missing_name"]
    assert any(item["code"] == "STYLE_FIELD_MISSING" for item in report["blocking_errors"])


def test_matching_field_passes(tmp_path):
    path = put(
        tmp_path,
        wrap("<TextSymbolizer><Label><ogc:PropertyName>name</ogc:PropertyName></Label></TextSymbolizer>"),
    )
    assert validate_sld(path, fields=["name"])["valid"]


@pytest.mark.parametrize(
    "location", ["https://example.invalid/image.svg", "file:///private/icon.png", "local.svg"]
)
def test_external_images_block_even_local_paths(tmp_path, location):
    path = put(
        tmp_path,
        wrap(
            f'<PointSymbolizer><Graphic><ExternalGraphic><OnlineResource xlink:href="{location}"/></ExternalGraphic></Graphic></PointSymbolizer>'
        ),
    )
    report = validate_sld(path)
    assert not report["valid"]
    assert any(item["code"] == "EXTERNAL_STYLE_RESOURCE" for item in report["blocking_errors"])


def test_xml_entities_and_dtd_are_rejected(tmp_path):
    content = '<!DOCTYPE StyledLayerDescriptor [<!ENTITY secret SYSTEM "file:///etc/passwd">]>' + wrap(
        "<TextSymbolizer><Label>&secret;</Label></TextSymbolizer>"
    )
    report = validate_sld(put(tmp_path, content))
    assert not report["valid"]
    assert report["blocking_errors"][0]["code"] == "INVALID_SLD_XML"


def test_qml_and_unknown_sld_versions_rejected(tmp_path):
    assert not validate_sld(put(tmp_path, "<qgis/>"))["valid"]
    assert not validate_sld(put(tmp_path, wrap("<PolygonSymbolizer/>", version="2.0.0")))["valid"]


def test_sld11_se_symbolizers_supported(tmp_path):
    content = wrap('<se:PolygonSymbolizer xmlns:se="http://www.opengis.net/se"/>', version="1.1.0")
    assert validate_sld(put(tmp_path, content))["valid"]


def test_scale_ranges_are_recorded_and_invalid_limits_block(tmp_path):
    content = wrap(
        "<MinScaleDenominator>5000</MinScaleDenominator><MaxScaleDenominator>10000</MaxScaleDenominator><PolygonSymbolizer/>"
    )
    report = validate_sld(put(tmp_path, content))
    assert report["valid"]
    assert report["scale_ranges"] == [{"MinScaleDenominator": 5000, "MaxScaleDenominator": 10000}]
    report = validate_sld(put(tmp_path, content.replace("10000", "1000")))
    assert not report["valid"]


def test_existing_different_style_never_overwritten(tmp_path):
    path = put(tmp_path, "keep me")
    with pytest.raises(StyleError) as error:
        write_default_sld(path, name="test", geometry_type="Point")
    assert error.value.code == "STYLE_ALREADY_EXISTS"
    assert path.read_text() == "keep me"


def test_unknown_geometry_has_no_silent_polygon_fallback(tmp_path):
    with pytest.raises(StyleError) as error:
        write_default_sld(tmp_path / "unknown.sld", name="unknown", geometry_type="GeometryCollection")
    assert error.value.code == "GEOMETRY_STYLE_UNSUPPORTED"


def test_four_band_rgba_preserves_native_alpha_but_other_rgb_styles_choose_channels(tmp_path):
    rgba_path = write_default_sld(
        tmp_path / "rgba.sld", name="rgba", band_count=4, alpha_band=4
    )
    rgba_text = rgba_path.read_text(encoding="utf-8")
    assert validate_sld(rgba_path)["valid"]
    assert "RasterSymbolizer" in rgba_text
    assert "ChannelSelection" not in rgba_text
    assert "Opacity" in rgba_text

    rgb_path = write_default_sld(tmp_path / "rgb.sld", name="rgb", band_count=3)
    assert "ChannelSelection" in rgb_path.read_text(encoding="utf-8")
    four_without_alpha = write_default_sld(
        tmp_path / "four-multispectral.sld", name="multi", band_count=4
    )
    assert "ChannelSelection" in four_without_alpha.read_text(encoding="utf-8")

    with pytest.raises(StyleError) as caught:
        write_default_sld(tmp_path / "bad.sld", name="bad", band_count=4, alpha_band=5)
    assert caught.value.code == "INVALID_ALPHA_BAND"
