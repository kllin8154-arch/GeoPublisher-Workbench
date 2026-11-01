"""Read-only WMS sampling for synthetic NoData/Alpha GeoTIFFs.

Never sends REST writes or authentication. Only loops back to the GeoServer
server explicitly cloned under the local validation workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree
from PIL import Image, ImageChops, ImageDraw

from geopublisher.engine import review_wms_views
from geopublisher.geoserver import GeoServerError, MapResponse
from geopublisher.verification import VerificationError, verify_image

_CASES = (
    ("full", [110.0, 29.0, 111.0, 30.0]),
    ("valid_window", [110.25, 29.25, 110.75, 29.75]),
    ("nodata_corner", [110.0, 29.75, 110.25, 30.0]),
    ("outside", [0.0, 0.0, 1.0, 1.0]),
)
_LAYERS = ("synthetic_nodata", "synthetic_alpha")
_PIXELS = ((32, 32), (129, 129), (256, 256), (384, 384), (500, 500))


def _scoped_layer_names(xml: bytes) -> set[str]:
    """WMS scoped capabilities may use the bare layer name, unlike global WMS."""
    root = ElementTree.fromstring(xml)
    names = set()
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "Layer":
            continue
        for child in item:
            if child.tag.rsplit("}", 1)[-1] == "Name" and child.text:
                names.add(child.text.strip())
    return names


def _visible_mask_evidence(rgba: Image.Image, layer: str, case_name: str) -> dict:
    """Compare true GeoServer pixels with known 128px mathematical image masks."""
    alpha = rgba.getchannel("A")
    counts = {str(index): count for index, count in enumerate(alpha.histogram()) if count}
    expected = Image.new("L", rgba.size, 0)
    if case_name == "full":
        ImageDraw.Draw(expected).rectangle((128, 128, 383, 383), fill=255)
    elif case_name == "valid_window":
        expected.paste(255, (0, 0, *rgba.size))
    binary = alpha.point(lambda value: 255 if value else 0)
    mask_matches = ImageChops.difference(expected, binary).getbbox() is None
    expected_counts = (
        {"0": 196608, "255": 65536}
        if layer == "synthetic_nodata" and case_name == "full"
        else {"0": 196608, "96": 15360, "255": 50176}
        if layer == "synthetic_alpha" and case_name == "full"
        else None
    )
    # Other sub-windows are only expected to be entirely valid or empty.
    return {
        "mask_matches_expected_geometry": mask_matches,
        "alpha_histogram_nonzero_bins": counts,
        "expected_full_alpha_histogram": expected_counts,
        "alpha_histogram_matches": expected_counts is None or counts == expected_counts,
        "all_expected_pixels_covered": mask_matches,
    }


class ReadOnlyRasterAdapter:
    """The engine's GetMap protocol, constrained to the disposable raster layers."""

    def __init__(self, http: httpx.Client, base_url: str):
        self.http = http
        self.url = base_url.rstrip("/") + "/nurc/wms"
        self.requests = 0

    def get_map(self, workspace: str, layer: str, *, bbox, crs, version, width, height):
        if (
            workspace != "nurc"
            or layer not in _LAYERS
            or crs != "EPSG:4326"
            or version != "1.1.1"
            or (width, height) != (512, 512)
        ):
            raise ValueError("Only synthetic rasters and fixed WMS test semantics are allowed.")
        self.requests += 1
        response = self.http.get(
            self.url,
            params={
                "service": "WMS", "request": "GetMap", "version": version,
                "layers": f"nurc:{layer}", "styles": "", "srs": crs,
                "format": "image/png", "transparent": "true",
                "bbox": ",".join(format(float(v), ".17g") for v in bbox),
                "width": str(width), "height": str(height),
            },
            headers={"Accept": "image/png"},
        )
        if response.status_code != 200 or len(response.content) > 32 * 1024 * 1024:
            raise GeoServerError("DISPOSABLE_WMS_FAILED", "Disposable raster GetMap was not a valid small response.")
        return MapResponse(200, {"content-type": response.headers.get("content-type", "")}, response.content)


def sample(base_url: str) -> dict:
    url = urlsplit(base_url)
    if (
        url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or url.path.rstrip("/") != "/geoserver"
        or url.query
        or url.fragment
        or url.username
        or url.password
    ):
        raise ValueError("WMS sampling is restricted to disposable loopback GeoServer.")
    results: dict[str, dict] = {}
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as http:
        caps = http.get(
            base_url.rstrip("/") + "/nurc/wms",
            params={"service": "WMS", "version": "1.1.1", "request": "GetCapabilities"},
        )
        if caps.status_code != 200:
            raise RuntimeError("Read-only WMS GetCapabilities failed.")
        scoped_names = _scoped_layer_names(caps.content)
        present = {layer: (layer in scoped_names or f"nurc:{layer}" in scoped_names) for layer in _LAYERS}
        for layer in _LAYERS:
            records: dict[str, dict] = {}
            for case_name, bbox in _CASES:
                response = http.get(
                    base_url.rstrip("/") + "/nurc/wms",
                    params={
                        "service": "WMS",
                        "request": "GetMap",
                        "version": "1.1.1",
                        "layers": f"nurc:{layer}",
                        "styles": "",
                        "srs": "EPSG:4326",
                        "format": "image/png",
                        "transparent": "true",
                        "bbox": ",".join(str(v) for v in bbox),
                        "width": "512",
                        "height": "512",
                    },
                    headers={"Accept": "image/png"},
                )
                record: dict = {
                    "http_status": response.status_code,
                    "content_type": response.headers.get("content-type", "").split(";")[0],
                    "response_bytes": len(response.content),
                    "wms_bbox": bbox,
                    "sha256": hashlib.sha256(response.content).hexdigest(),
                }
                try:
                    if response.status_code != 200:
                        raise VerificationError("HTTP_ERROR", "WMS request returned an error status.")
                    evidence = verify_image(response.content, response.headers.get("content-type", ""))
                    with Image.open(io.BytesIO(response.content)) as image:
                        rgba = image.convert("RGBA")
                        record["sample_rgba"] = {
                            f"{x},{y}": list(rgba.getpixel((x, y))) for x, y in _PIXELS
                        }
                        record["mathematical_mask"] = _visible_mask_evidence(rgba, layer, case_name)
                    record["evidence"] = evidence
                except VerificationError as exc:
                    record["status"] = "ERROR"
                    record["code"] = exc.code
                records[case_name] = record
            results[layer] = records
        engine_reviews = {}
        for layer in _LAYERS:
            adapter = ReadOnlyRasterAdapter(http, base_url)
            full = review_wms_views(adapter, "nurc", layer, list(_CASES[0][1]))
            wrong = review_wms_views(adapter, "nurc", layer, list(_CASES[-1][1]))
            engine_reviews[layer] = {
                "full_status": full.status,
                "full_requests": full.selected_sample + 1 if full.status == "SUCCEEDED" else len(full.samples),
                "full_selected_sample": full.selected_sample,
                "full_sha256": full.evidence["sha256"],
                "outside_status": wrong.status,
                "outside_samples": len(wrong.samples),
                "outside_selected_sample": wrong.selected_sample,
                "outside_visible_pixels": wrong.evidence["visible_pixels"],
                "requests_total": adapter.requests,
            }
    return {
        "data_origin": "mathematically-generated",
        "server": base_url,
        "expected_requests": 21,
        "operation": "WMS_GET_ONLY",
        "auth_provided": False,
        "server_writes": 0,
        "layers_in_capabilities": present,
        "layers": results,
        "engine_review": engine_reviews,
    }


def require_disposable_catalog(catalog: Path) -> dict:
    """Ensure a local test-owned GeoServer tree rather than an arbitrary service."""
    catalog = catalog.absolute()
    if (
        catalog.is_symlink()
        or not catalog.name.startswith("geoserver-disposable-raster-")
        or catalog.parent.name != "GeoPublisher-Workbench-validation"
        or not (catalog / "DISPOSABLE-ONLY.txt").is_file()
    ):
        raise ValueError("Only the prepared disposable raster test catalog is allowed.")
    record = json.loads((catalog / "prepared-catalog.json").read_text(encoding="utf-8"))
    if (
        record.get("status") != "DISPOSABLE_CATALOG_PREPARED_OFFLINE"
        or record.get("source") != "mathematically-generated"
        or record.get("server_writes") != 0
        or {item["qualified_name"] for item in record["layers"]}
        != {"nurc:synthetic_nodata", "nurc:synthetic_alpha"}
    ):
        raise ValueError("The prepared catalog does not match the synthetic test manifest.")
    for item in record["layers"]:
        filename = Path(item["filename"])
        if filename.is_absolute() or len(filename.parts) != 1:
            raise ValueError("The synthetic catalog contains an invalid filename.")
        physical = catalog / "data_dir" / "data" / "nurc" / filename
        if not physical.is_file() or hashlib.sha256(physical.read_bytes()).hexdigest() != item["file_sha256"]:
            raise ValueError("The copied synthetic raster has changed since preparation.")
    return record


def validate_report(result: dict) -> list[str]:
    """Strict, deterministic mask and scope gate with no hidden manual success."""
    problems = []
    if result.get("operation") != "WMS_GET_ONLY" or result.get("server_writes") != 0:
        problems.append("UNSAFE_OPERATION")
    for layer in _LAYERS:
        if result["layers_in_capabilities"].get(layer) is not True:
            problems.append(f"{layer}:MISSING_CAPABILITIES")
        for name, _ in _CASES:
            record = result["layers"].get(layer, {}).get(name, {})
            evidence = record.get("evidence", {})
            if record.get("http_status") != 200 or record.get("content_type") != "image/png":
                problems.append(f"{layer}:{name}:NOT_PNG")
                continue
            expected_status = "PASSED" if name in {"full", "valid_window"} else "REVIEW_REQUIRED"
            expected_visible = 65536 if name == "full" else 262144 if name == "valid_window" else 0
            if evidence.get("status") != expected_status or evidence.get("visible_pixels") != expected_visible:
                problems.append(f"{layer}:{name}:BAD_VISIBILITY")
            mask = record.get("mathematical_mask", {})
            if not mask.get("mask_matches_expected_geometry") or not mask.get("alpha_histogram_matches"):
                problems.append(f"{layer}:{name}:BAD_MASK")
            if record.get("sha256") != evidence.get("sha256"):
                problems.append(f"{layer}:{name}:BAD_CHECKSUM")
        full = result["layers"].get(layer, {}).get("full", {})
        colors = full.get("sample_rgba", {})
        if colors.get("32,32", [None] * 4)[3] != 0:
            problems.append(f"{layer}:OUTSIDE_ALPHA")
        if colors.get("256,256", [None] * 4)[3] != 255:
            problems.append(f"{layer}:INTERIOR_ALPHA")
        border = colors.get("129,129", [None] * 4)[3]
        if border != (96 if layer == "synthetic_alpha" else 255):
            problems.append(f"{layer}:BORDER_ALPHA")
    for layer in _LAYERS:
        check = result.get("engine_review", {}).get(layer, {})
        if (
            check.get("full_status") != "SUCCEEDED"
            or check.get("full_selected_sample") != 0
            or check.get("full_requests") != 1
            or check.get("outside_status") != "REVIEW_REQUIRED"
            or check.get("outside_samples") != 5
            or check.get("outside_selected_sample") != 0
            or check.get("outside_visible_pixels") != 0
            or check.get("requests_total") != 6
        ):
            problems.append(f"{layer}:ENGINE_WMS_INTEGRATION")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18882/geoserver")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite an existing verification report.")
    require_disposable_catalog(args.catalog)
    result = sample(args.url)
    failures = validate_report(result)
    result["acceptance_status"] = "PASSED" if not failures else "FAILED"
    result["acceptance_failures"] = failures
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for layer, cases in result["layers"].items():
        print("LAYER", layer, "listed", result["layers_in_capabilities"][layer])
        for name, record in cases.items():
            evidence = record.get("evidence", {})
            print(
                "  ", name, record["http_status"], record["content_type"],
                evidence.get("status", record.get("code")),
                "visible", evidence.get("visible_pixels"),
                "transparent", evidence.get("transparent_fraction"),
            )
    print("RASTER_WMS_ACCEPTANCE", result["acceptance_status"], failures)
    if failures:
        raise SystemExit("The strict synthetic raster WMS acceptance gate failed.")


if __name__ == "__main__":
    main()
