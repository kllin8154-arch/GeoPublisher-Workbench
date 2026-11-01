"""Math-only NoData/Alpha fixture tests; native GDAL runs only when installed."""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path

import pytest
from PIL import Image

from geopublisher.verification import verify_image

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_raster_mask_local.py"
_SPEC = importlib.util.spec_from_file_location("raster_mask_local_fixture", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_FIXTURE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FIXTURE)


def test_generated_nodata_and_alpha_inputs_are_deterministic(tmp_path):
    pgm, png = _FIXTURE.make_synthetic_inputs(tmp_path / "synthetic")
    data = pgm.read_bytes()
    header = b"P5\n128 128\n255\n"
    assert data.startswith(header)
    pixels = data[len(header):]
    assert len(pixels) == 128 * 128
    assert pixels[0] == 0
    assert pixels[32 * 128 + 32] > 0
    assert pixels[95 * 128 + 95] > 0
    assert pixels[96 * 128 + 95] == 0
    with Image.open(png) as image:
        assert image.size == (128, 128)
        assert image.getpixel((0, 0)) == (230, 45, 90, 0)
        assert image.getpixel((32, 32))[3] == 96
        assert image.getpixel((60, 60))[3] == 255
        resized = image.resize((512, 512), resample=Image.Resampling.NEAREST)
        buffer = io.BytesIO()
        resized.save(buffer, "PNG")
    evidence = verify_image(buffer.getvalue(), "image/png", expected_size=(512, 512))
    assert evidence["status"] == "PASSED"
    assert evidence["visible_pixels"] == 65536
    assert evidence["transparent_fraction"] == 0.75


def test_output_folder_must_be_new_even_without_native_runtime(tmp_path):
    preexisting = tmp_path / "already-exists"
    preexisting.mkdir()
    untouched = preexisting / "do-not-delete.txt"
    untouched.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="new output"):
        _FIXTURE.generate_gdal_fixtures(tmp_path / "missing-runtime", preexisting)
    assert untouched.read_text(encoding="utf-8") == "existing"


def test_native_gdal_nodata_alpha_smoke(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    if os.name == "nt":
        gdal = repo / ".pixi" / "envs" / "default" / "Library" / "bin"
        exe = "gdalinfo.exe"
    else:
        gdal = repo / ".pixi" / "envs" / "default" / "bin"
        exe = "gdalinfo"
    if not (gdal / exe).is_file():
        pytest.skip("Pinned native GDAL is not installed in this test runner")
    report = _FIXTURE.generate_gdal_fixtures(gdal, tmp_path / "fresh")
    assert report["status"] == "LOCAL_GDAL_VALIDATION_ONLY"
    assert report["server_requests"] == 0
    assert report["gdal"] == "3.13.3"
    assert report["nodata"]["noDataValue"] == 0
    assert report["rgba"]["alpha_band"] == 4
    assert report["local_png_only"]["visible_pixels"] == 65536
    assert report["geoserver_nodata_alpha_tested"] is False
