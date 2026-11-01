"""Read-only live WMS scale/style verification on a disposable loopback GeoServer.

Only publicly bundled demo feature tiger:poi is requested. The test adds an
inline WMS SLD_BODY *GET* parameter; it does not create or alter server styles.
No real user GIS files or credentials are required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .engine import review_wms_views
from .geoserver import GeoServerError, MapResponse

# At broad scale hide the mark; at quadrant scale show the red points.
_SCALE_SLD = (
    '<StyledLayerDescriptor version="1.0.0" '
    'xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc">'
    "<NamedLayer><Name>tiger:poi</Name><UserStyle><Title>Disposable scale test</Title>"
    "<FeatureTypeStyle><Rule><MaxScaleDenominator>500000</MaxScaleDenominator>"
    "<PointSymbolizer><Graphic><Mark><WellKnownName>circle</WellKnownName>"
    '<Fill><CssParameter name="fill">#D04A36</CssParameter></Fill></Mark>'
    "<Size>10</Size></Graphic></PointSymbolizer>"
    "</Rule></FeatureTypeStyle></UserStyle></NamedLayer></StyledLayerDescriptor>"
)
_BROAD = [-74.6, 40.2, -73.4, 41.2]
_CLOSE = [-74.015, 40.705, -73.995, 40.726]
_EMPTY = [0.0, 0.0, 1.0, 1.0]


class ReadOnlySldClient:
    """A strictly GET-only adapter returning genuine GeoServer PNGs."""

    def __init__(self, base_url: str, *, scale_style: bool):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/geoserver"
        ):
            raise ValueError("The test requires http://127.0.0.1:<port>/geoserver.")
        self.url = base_url.rstrip("/") + "/tiger/wms"
        self.style = scale_style
        self.request_count = 0
        self._http = httpx.Client(trust_env=False, follow_redirects=False, timeout=30)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._http.close()

    def get_map(self, workspace, layer, *, bbox, crs, version, width, height):
        if (workspace, layer, crs, version, width, height) != (
            "tiger", "poi", "EPSG:4326", "1.1.1", 512, 512
        ):
            raise ValueError("Only the known bundled tiger:poi demo layer is permitted.")
        params = {
            "service": "WMS",
            "request": "GetMap",
            "version": "1.1.1",
            "layers": "tiger:poi",
            "styles": "",
            "format": "image/png",
            "transparent": "true",
            "width": "512",
            "height": "512",
            "srs": "EPSG:4326",
            "bbox": ",".join(format(float(x), ".17g") for x in bbox),
        }
        if self.style:
            # Without LAYERS this is self-contained SLD mode, not a style library
            # whose unnamed rule would be ignored in favor of the default style.
            params.pop("layers")
            params.pop("styles")
            params["SLD_BODY"] = _SCALE_SLD
        self.request_count += 1
        try:
            response = self._http.get(self.url, params=params, headers={"Accept": "image/png"})
        except httpx.RequestError:
            raise GeoServerError("LOOPBACK_WMS_FAILED", "Isolated WMS request did not complete.") from None
        if response.status_code != 200:
            raise GeoServerError(
                "LOOPBACK_WMS_FAILED", f"Isolated WMS returned HTTP {response.status_code}.",
                status_code=response.status_code,
            )
        if len(response.content) > 32 * 1024 * 1024:
            raise GeoServerError("RESPONSE_TOO_LARGE", "Isolated WMS PNG exceeded 32 MiB.")
        return MapResponse(
            response.status_code, {"content-type": response.headers.get("content-type", "")},
            response.content,
        )


def _brief(review, request_count: int) -> dict:
    return {
        "status": review.status,
        "selected_sample": review.selected_sample,
        "requests": request_count,
        "sample_count": len(review.samples),
        "sample_statuses": [
            sample["evidence"]["status"] if "evidence" in sample else sample.get("status")
            for sample in review.samples
        ],
        "selected_bbox": review.bbox,
        "image_sha256": hashlib.sha256(review.image).hexdigest(),
        "visible_pixels": review.evidence["visible_pixels"],
        "reason_codes": [sample.get("code") for sample in review.samples if "code" in sample],
    }


def run(base_url: str) -> dict:
    with ReadOnlySldClient(base_url, scale_style=False) as client:
        normal = review_wms_views(client, "tiger", "poi", _CLOSE)
        normal_record = _brief(normal, client.request_count)
    with ReadOnlySldClient(base_url, scale_style=True) as client:
        scaled = review_wms_views(client, "tiger", "poi", _BROAD)
        scale_record = _brief(scaled, client.request_count)
    with ReadOnlySldClient(base_url, scale_style=False) as client:
        empty = review_wms_views(client, "tiger", "poi", _EMPTY)
        empty_record = _brief(empty, client.request_count)
    return {
        "data_origin": "bundled-geoserver-tiger-poi-demo",
        "server_writes": 0,
        "authentication_provided": False,
        "all_requests": "WMS_GET_ONLY",
        "ordinary": normal_record,
        "scale_style": scale_record,
        "outside_bbox": empty_record,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18881/geoserver")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite an existing run.")
    report = run(args.url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not (
        report["ordinary"]["status"] == "SUCCEEDED"
        and report["ordinary"]["requests"] == 1
        and report["scale_style"]["status"] == "SUCCEEDED"
        and report["scale_style"]["selected_sample"] > 0
        and report["scale_style"]["sample_statuses"][0] == "REVIEW_REQUIRED"
        and report["outside_bbox"]["status"] == "REVIEW_REQUIRED"
        and report["outside_bbox"]["requests"] == 5
    ):
        raise SystemExit("WMS live read-only acceptance did not meet the strict gate.")


if __name__ == "__main__":
    main()
