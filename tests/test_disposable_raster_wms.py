"""No-network tests of the disposable raster catalog and WMS mask gate."""

from __future__ import annotations

import importlib.util
import io
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw


def _load_script(filename: str, module_name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CATALOG = _load_script("prepare_disposable_raster_catalog.py", "disposable_catalog")
WMS = _load_script("validate_disposable_raster_wms.py", "synthetic_raster_wms")


def _xml_templates() -> tuple[bytes, bytes, bytes]:
    """Small trusted XML fixture matching the offline GeoServer sample shape."""
    store = (
        b"<coverageStore><id>old-store</id><name>old</name><description>old</description>"
        b"<type>GeoTIFF</type><url>file:old</url><workspace><id>nurc</id></workspace></coverageStore>"
    )
    coverage = (
        b"<coverage><id>old-coverage</id><name>old</name><nativeName>old</nativeName>"
        b"<title>old</title><description>old</description><store><id>old-store</id></store>"
        b"<nativeFormat>WorldImage</nativeFormat><srs>EPSG:4326</srs>"
        b"<nativeBoundingBox><minx>0</minx><miny>0</miny><maxx>1</maxx><maxy>1</maxy>"
        b"<crs>EPSG:4326</crs></nativeBoundingBox>"
        b"<latLonBoundingBox><minx>0</minx><miny>0</miny><maxx>1</maxx><maxy>1</maxy>"
        b"<crs>EPSG:4326</crs></latLonBoundingBox>"
        b"<metadata><entry>old</entry></metadata>"
        b"<grid><range><high>10 10</high></range><crs>EPSG:4326</crs>"
        b"<transform><scaleX>1</scaleX><scaleY>-1</scaleY>"
        b"<translateX>0</translateX><translateY>0</translateY></transform></grid>"
        b"<dimensions><coverageDimension><name>old</name></coverageDimension></dimensions></coverage>"
    )
    layer = (
        b"<layer><id>old-layer</id><name>old</name><type>RASTER</type>"
        b"<resource><id>old-coverage</id></resource><defaultStyle><id>raster</id></defaultStyle></layer>"
    )
    return store, coverage, layer


@pytest.mark.parametrize("fixture", CATALOG._NAMES)
def test_catalog_rewrites_only_known_synthetic_layers(fixture):
    name, filename, bands = fixture
    originals = _xml_templates()
    results = CATALOG.catalog_xml(*originals, name, filename, bands)
    assert all(result != original for result, original in zip(results, originals))
    store, coverage, layer = map(ET.fromstring, results)
    assert store.findtext("name") == name
    assert store.findtext("url") == f"file:data/nurc/{filename}"
    assert coverage.findtext("name") == layer.findtext("name") == name
    assert coverage.findtext("nativeName") == filename[:-4]
    assert coverage.findtext("store/id") == store.findtext("id")
    assert layer.findtext("resource/id") == coverage.findtext("id")
    assert coverage.findtext("nativeBoundingBox/minx") == "110.0"
    assert coverage.findtext("nativeBoundingBox/miny") == "29.0"
    assert coverage.findtext("latLonBoundingBox/maxy") == "30.0"
    assert coverage.findtext("grid/range/high") == "127 127"
    assert coverage.findtext("grid/transform/scaleX") == str(1 / 128)
    assert coverage.findtext("grid/transform/scaleY") == str(-1 / 128)
    assert [item.findtext("name") for item in coverage.findall("dimensions/coverageDimension")] == list(bands)


def test_catalog_rejects_mismatched_filename_and_existing_destination(tmp_path):
    with pytest.raises(ValueError, match="explicitly generated"):
        CATALOG.catalog_xml(*_xml_templates(), "synthetic_alpha", "nodata-gray.tif", ("Alpha band",))
    base = tmp_path / "GeoPublisher-Workbench-validation"
    dest = base / "geoserver-disposable-raster-test"
    dest.mkdir(parents=True)
    sentinel = dest / "kept.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    with pytest.raises(ValueError, match="overwrite"):
        CATALOG.prepare(tmp_path, tmp_path, dest)
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def _image_bytes(layer: str, case: str) -> bytes:
    image = Image.new("RGBA", (512, 512), (255, 255, 255, 0))
    painter = ImageDraw.Draw(image)
    if case == "full":
        color = (120, 120, 120, 255) if layer == "synthetic_nodata" else (120, 80, 50, 96)
        painter.rectangle((128, 128, 383, 383), fill=color)
        if layer == "synthetic_alpha":
            painter.rectangle((144, 144, 367, 367), fill=(120, 80, 50, 255))
    if case == "valid_window":
        painter.rectangle((0, 0, 511, 511), fill=(100, 150, 90, 255))
        painter.rectangle((90, 90, 420, 420), fill=(150, 90, 120, 255))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def test_mathematical_mask_detects_premultiplied_and_semitransparent_pixels():
    with Image.open(io.BytesIO(_image_bytes("synthetic_alpha", "full"))) as image:
        result = WMS._visible_mask_evidence(image.convert("RGBA"), "synthetic_alpha", "full")
    assert result["mask_matches_expected_geometry"] is True
    assert result["alpha_histogram_matches"] is True
    assert result["alpha_histogram_nonzero_bins"] == {"0": 196608, "96": 15360, "255": 50176}
    altered = Image.new("RGBA", (512, 512), (255, 255, 255, 0))
    ImageDraw.Draw(altered).rectangle((128, 128, 383, 383), fill=(100, 40, 70, 255))
    rejected = WMS._visible_mask_evidence(altered, "synthetic_alpha", "full")
    assert rejected["mask_matches_expected_geometry"] is True
    assert rejected["alpha_histogram_matches"] is False
    altered.putpixel((1, 1), (250, 0, 0, 255))
    rejected = WMS._visible_mask_evidence(altered, "synthetic_alpha", "full")
    assert rejected["mask_matches_expected_geometry"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/geoserver",
        "https://127.0.0.1:18882/geoserver",
        "http://user:pass@127.0.0.1:18882/geoserver",
        "http://127.0.0.1:18882/geoserver?source=test",
    ],
)
def test_disposable_wms_only_accepts_safe_loopback_urls(url):
    with pytest.raises(ValueError, match="loopback"):
        WMS.sample(url)


def test_scoped_wms_capabilities_accepts_bare_layer_names():
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<WMT_MS_Capabilities><Capability><Layer>'
        b'<Layer><Name>synthetic_nodata</Name></Layer>'
        b'<Layer><Name>synthetic_alpha</Name></Layer>'
        b'</Layer></Capability></WMT_MS_Capabilities>'
    )
    assert WMS._scoped_layer_names(xml) == {"synthetic_nodata", "synthetic_alpha"}


def test_full_readonly_workflow_uses_exactly_21_get_requests(monkeypatch):
    """Exercise both strict mathematical masks and the engine's five-window review."""
    original_client = httpx.Client
    calls: list[str] = []
    caps = (
        b"<WMT_MS_Capabilities><Capability><Layer>"
        b"<Layer><Name>synthetic_nodata</Name></Layer>"
        b"<Layer><Name>synthetic_alpha</Name></Layer>"
        b"</Layer></Capability></WMT_MS_Capabilities>"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "127.0.0.1"
        assert request.url.path == "/geoserver/nurc/wms"
        assert "authorization" not in request.headers
        action = request.url.params["request"]
        calls.append(action)
        if action == "GetCapabilities":
            return httpx.Response(200, content=caps, headers={"content-type": "application/vnd.ogc.wms_xml"})
        layer = request.url.params["layers"].split(":")[-1]
        xmin, ymin, xmax, ymax = map(float, request.url.params["bbox"].split(","))
        if xmax < 5 or ymax < 5 or (xmax <= 110.26 and ymin >= 29.74):
            case = "outside"
        elif 110.249 <= xmin <= 110.251 and 110.749 <= xmax <= 110.751:
            case = "valid_window"
        else:
            case = "full"
        return httpx.Response(200, content=_image_bytes(layer, case), headers={"content-type": "image/png"})

    monkeypatch.setattr(
        WMS.httpx, "Client",
        lambda **options: original_client(transport=httpx.MockTransport(respond), **options),
    )
    result = WMS.sample("http://127.0.0.1:18882/geoserver")
    assert len(calls) == result["expected_requests"] == 21
    assert calls.count("GetCapabilities") == 1
    assert calls.count("GetMap") == 20
    assert WMS.validate_report(result) == []
    result["layers"]["synthetic_alpha"]["full"]["mathematical_mask"]["alpha_histogram_matches"] = False
    assert "synthetic_alpha:full:BAD_MASK" in WMS.validate_report(result)
