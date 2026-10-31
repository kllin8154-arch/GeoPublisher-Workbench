import io

import pytest
from PIL import Image

from geopublisher.verification import VerificationError, verify_image, wms_bbox


def png(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def test_nonempty_transparent_vector_preview_passes():
    image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    image.putpixel((10, 10), (255, 0, 0, 255))
    result = verify_image(png(image), "image/png", expected_size=(32, 32))
    assert result["status"] == "PASSED"
    assert result["visible_pixels"] == 1
    assert result["non_background_pixels"] == 1
    assert len(result["sha256"]) == 64


@pytest.mark.parametrize("allow_empty", [False, True])
def test_transparent_hidden_rgb_cannot_pass_even_with_allow_empty(allow_empty):
    image = Image.new("RGBA", (32, 32), (1, 2, 3, 0))
    image.putpixel((10, 10), (255, 0, 0, 0))
    result = verify_image(
        png(image), "image/png; charset=binary", expected_size=(32, 32), allow_empty=allow_empty
    )
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["reasons"] == ["FULLY_TRANSPARENT"]
    assert result["visible_pixels"] == 0
    assert result["alternate_acceptance_allowed"] is allow_empty


@pytest.mark.parametrize("color", ["white", "black", "red"])
def test_uniform_raster_requires_review_instead_of_false_automatic_pass(color):
    result = verify_image(png(Image.new("RGB", (32, 32), color)), "image/png", expected_size=(32, 32))
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["reasons"] == ["UNIFORM_IMAGE"]


def test_actual_gradient_raster_passes():
    image = Image.new("RGB", (32, 32))
    image.putdata([(x * 7, y * 7, 100) for y in range(32) for x in range(32)])
    assert verify_image(png(image), "image/png", expected_size=(32, 32))["status"] == "PASSED"


@pytest.mark.parametrize(
    "body",
    [
        b"<ServiceExceptionReport>server failure</ServiceExceptionReport>",
        b"\xef\xbb\xbf <?xml version='1.0'?><ExceptionReport/>",
        b"<!DOCTYPE html><html>login</html>",
    ],
)
def test_xml_or_html_error_body_cannot_pass_with_image_content_type(body):
    with pytest.raises(VerificationError) as caught:
        verify_image(body, "image/png")
    assert caught.value.code == "WMS_EXCEPTION"
    assert "server failure" not in str(caught.value)


def test_content_type_and_actual_file_signature_both_required():
    content = png(Image.new("RGB", (32, 32), "red"))
    with pytest.raises(VerificationError) as caught:
        verify_image(content, "application/octet-stream", expected_size=(32, 32))
    assert caught.value.code == "INVALID_CONTENT_TYPE"
    with pytest.raises(VerificationError) as caught:
        verify_image(b"not a png", "image/png", expected_size=(32, 32))
    assert caught.value.code == "INVALID_IMAGE"


def test_wrong_dimensions_and_corrupt_image_fail():
    content = png(Image.new("RGB", (32, 32), "red"))
    with pytest.raises(VerificationError) as caught:
        verify_image(content, "image/png", expected_size=(64, 64))
    assert caught.value.code == "IMAGE_SIZE_MISMATCH"
    with pytest.raises(VerificationError) as caught:
        verify_image(content[:40], "image/png", expected_size=(32, 32))
    assert caught.value.code == "INVALID_IMAGE"


def test_palette_transparency_uses_alpha():
    image = Image.new("P", (16, 16), 0)
    image.putpalette([255, 0, 0] + [0, 0, 0] * 255)
    image.info["transparency"] = 0
    result = verify_image(png(image), "image/png", expected_size=(16, 16))
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["visible_pixels"] == 0


def test_axis_order_is_explicit_and_input_is_not_mutated():
    bbox = [100, 20, 110, 30]
    assert wms_bbox(bbox, "EPSG:4326", "1.1.1") == (100, 20, 110, 30)
    assert wms_bbox(bbox, "EPSG:4326", "1.3.0") == (20, 100, 30, 110)
    assert wms_bbox(bbox, "CRS:84", "1.3.0") == (100, 20, 110, 30)
    assert wms_bbox([0, 0, 1000, 1000], "EPSG:3857", "1.3.0") == (0, 0, 1000, 1000)
    assert bbox == [100, 20, 110, 30]


def test_unknown_13_axis_order_is_not_guessed():
    with pytest.raises(VerificationError) as caught:
        wms_bbox([100, 20, 110, 30], "EPSG:4490", "1.3.0")
    assert caught.value.code == "UNSUPPORTED_AXIS_ORDER"


@pytest.mark.parametrize(
    "bbox",
    [
        [1, 2, 1, 3],
        [10, 10, 0, 20],
        [0, 0, 10],
        [float("nan"), 0, 1, 1],
        [0, 0, float("inf"), 1],
        [True, 0, 5, 5],
    ],
)
def test_invalid_extent_is_rejected(bbox):
    with pytest.raises(VerificationError) as caught:
        wms_bbox(bbox, "EPSG:4326", "1.1.1")
    assert caught.value.code == "INVALID_BBOX"


def test_projected_units_cannot_be_passed_as_wgs84_bbox():
    with pytest.raises(VerificationError) as caught:
        wms_bbox([100000, 200000, 110000, 210000], "EPSG:4326", "1.1.1")
    assert caught.value.code == "BBOX_OUT_OF_RANGE"
