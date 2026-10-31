"""Conservative WMS response checks.

Passing these checks proves that a correctly sized, non-uniform PNG was returned.
It does not prove cartographic or geographic correctness; those require a known
reference area or a human review. Uniform/transparent results always need review.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import warnings
from typing import Iterable

from PIL import Image, ImageChops, UnidentifiedImageError


class VerificationError(ValueError):
    """A response cannot be used as valid map evidence."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_CRS_RE = re.compile(r"^(?:EPSG:[0-9]{1,8}|CRS:84)$", re.IGNORECASE)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_PIXELS = 16 * 1024 * 1024


def wms_bbox(bbox: Iterable[float], crs: str, version: str) -> tuple[float, float, float, float]:
    """Convert an already-reprojected x/y envelope to WMS wire axis order.

    This function does not reproject coordinates. To avoid silently guessing an
    axis order, WMS 1.3.0 is limited to the three explicitly supported CRSs.
    """
    if version not in {"1.1.1", "1.3.0"}:
        raise VerificationError("UNSUPPORTED_WMS_VERSION", "Only WMS 1.1.1 and 1.3.0 are supported.")
    if not isinstance(crs, str) or not _CRS_RE.fullmatch(crs):
        raise VerificationError("INVALID_CRS", "WMS verification requires an EPSG code or CRS:84.")
    try:
        raw = tuple(bbox)
        if len(raw) != 4 or any(isinstance(value, bool) for value in raw):
            raise ValueError
        values = tuple(float(value) for value in raw)
    except (TypeError, ValueError, OverflowError):
        raise VerificationError(
            "INVALID_BBOX", "The bounding box must contain four finite numbers."
        ) from None
    if any(not math.isfinite(value) for value in values):
        raise VerificationError("INVALID_BBOX", "The bounding box must contain four finite numbers.")
    x_min, y_min, x_max, y_max = values
    if x_min >= x_max or y_min >= y_max:
        raise VerificationError("INVALID_BBOX", "The bounding box must have positive width and height.")
    normalized = crs.upper()
    if normalized in {"EPSG:4326", "CRS:84"}:
        if not (-180 <= x_min < x_max <= 180 and -90 <= y_min < y_max <= 90):
            raise VerificationError(
                "BBOX_OUT_OF_RANGE", "The geographic bounding box is outside valid bounds."
            )
    if version == "1.3.0":
        if normalized == "EPSG:4326":
            return y_min, x_min, y_max, x_max
        if normalized not in {"CRS:84", "EPSG:3857"}:
            raise VerificationError(
                "UNSUPPORTED_AXIS_ORDER", "Use WMS 1.1.1 for a CRS whose 1.3.0 axis order is not verified."
            )
    return x_min, y_min, x_max, y_max


def verify_image(
    content: bytes,
    content_type: str,
    *,
    expected_size: tuple[int, int] = (512, 512),
    allow_empty: bool = False,
) -> dict:
    """Validate a WMS PNG and return serializable evidence without the image.

    ``allow_empty`` records that the plan permits an alternate/manual acceptance
    path. It deliberately never turns an empty image into an automatic pass.
    """
    if not isinstance(content, bytes) or not content:
        raise VerificationError("EMPTY_RESPONSE", "The map response is empty.")
    if len(content) > _MAX_IMAGE_BYTES:
        raise VerificationError("IMAGE_TOO_LARGE", "The map response exceeds the verification size limit.")
    if (
        len(expected_size) != 2
        or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in expected_size)
        or expected_size[0] * expected_size[1] > _MAX_PIXELS
    ):
        raise VerificationError("INVALID_IMAGE_SIZE", "The expected image size is invalid.")
    prefix = content.lstrip(b"\xef\xbb\xbf \t\r\n")[:128].lower()
    if prefix.startswith(b"<"):
        raise VerificationError("WMS_EXCEPTION", "The service returned XML or HTML instead of a map image.")
    media_type = str(content_type).split(";", 1)[0].strip().lower()
    if media_type != "image/png":
        raise VerificationError("INVALID_CONTENT_TYPE", "The map response must have Content-Type image/png.")
    if not content.startswith(_PNG_SIGNATURE):
        raise VerificationError("INVALID_IMAGE", "The map response does not contain a PNG image.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as decoded:
                if decoded.format != "PNG" or getattr(decoded, "n_frames", 1) != 1:
                    raise VerificationError("INVALID_IMAGE", "Only a single-frame PNG is accepted.")
                if decoded.size != tuple(expected_size):
                    raise VerificationError(
                        "IMAGE_SIZE_MISMATCH", "The map dimensions do not match the request."
                    )
                if decoded.width * decoded.height > _MAX_PIXELS:
                    raise VerificationError(
                        "IMAGE_TOO_LARGE", "The map exceeds the verification pixel limit."
                    )
                decoded.verify()
            with Image.open(io.BytesIO(content)) as decoded:
                rgba = decoded.convert("RGBA")
                rgba.load()
    except VerificationError:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise VerificationError("IMAGE_TOO_LARGE", "The map exceeds safe image decoding limits.") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise VerificationError(
            "INVALID_IMAGE", "The PNG response is corrupt or cannot be decoded."
        ) from None

    pixel_count = rgba.width * rgba.height
    alpha = rgba.getchannel("A")
    histogram = alpha.histogram()
    transparent_count = histogram[0]
    visible_count = pixel_count - transparent_count
    # Hidden RGB values in fully transparent pixels must not make a blank map
    # look non-uniform. Composite onto white before counting differences.
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    composite = Image.alpha_composite(background, rgba).convert("RGB")
    colors = composite.getcolors(maxcolors=1)
    uniform = colors is not None
    difference = ImageChops.invert(composite)
    red, green, blue = difference.split()
    maximum_difference = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    non_background = pixel_count - maximum_difference.histogram()[0]
    reasons: list[str] = []
    if visible_count == 0:
        reasons.append("FULLY_TRANSPARENT")
    elif uniform:
        reasons.append("UNIFORM_IMAGE")
    elif non_background == 0:
        reasons.append("BACKGROUND_ONLY")
    return {
        "status": "REVIEW_REQUIRED" if reasons else "PASSED",
        "sha256": hashlib.sha256(content).hexdigest(),
        "width": rgba.width,
        "height": rgba.height,
        "byte_size": len(content),
        "visible_pixels": visible_count,
        "transparent_fraction": transparent_count / pixel_count,
        "non_background_pixels": non_background,
        "uniform": uniform,
        "reasons": reasons,
        "alternate_acceptance_allowed": bool(allow_empty),
    }
