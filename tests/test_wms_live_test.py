"""No-network tests for the read-only, loopback-only live WMS smoke harness."""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from geopublisher.engine import review_wms_views
from geopublisher.geoserver import GeoServerError
from geopublisher.wms_live_test import _BROAD, _CLOSE, _EMPTY, ReadOnlySldClient


def _png(*, blank: bool) -> bytes:
    image = Image.new("RGBA", (512, 512), (1, 2, 3, 0))
    if not blank:
        for y in range(80, 90):
            for x in range(200, 210):
                image.putpixel((x, y), (203, 60, 39, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:18881/geoserver",
        "http://example.com/geoserver",
        "http://127.0.0.1:18881/geoserver?token=secret",
        "http://127.0.0.1:18881/geoserver#fragment",
        "http://user:pass@127.0.0.1:18881/geoserver",
        "http://127.0.0.1:18881/other",
    ],
)
def test_rejects_non_disposable_or_credentialed_url(url):
    with pytest.raises(ValueError):
        ReadOnlySldClient(url, scale_style=True)


def test_scale_style_uses_self_contained_sld_and_only_readonly_gets():
    calls = []
    response_empty = _png(blank=True)
    response_valid = _png(blank=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "127.0.0.1"
        assert request.url.path == "/geoserver/tiger/wms"
        assert "Authorization" not in request.headers
        params = request.url.params
        assert params["SLD_BODY"].count("<MaxScaleDenominator>500000</MaxScaleDenominator>") == 1
        assert "layers" not in params and "styles" not in params
        calls.append(params["bbox"])
        return httpx.Response(
            200, content=response_empty if len(calls) == 1 else response_valid,
            headers={"content-type": "image/png"},
        )

    with ReadOnlySldClient("http://127.0.0.1:18881/geoserver", scale_style=True) as client:
        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        review = review_wms_views(client, "tiger", "poi", _BROAD)
        assert client.request_count == 2
    assert len(calls) == 2 and calls[0] != calls[1]
    assert review.status == "SUCCEEDED"
    assert review.selected_sample == 1
    assert review.samples[0]["evidence"]["status"] == "REVIEW_REQUIRED"
    assert review.samples[1]["evidence"]["status"] == "PASSED"
    assert review.image == response_valid


def test_normal_wms_request_keeps_named_layer_and_default_style():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        params = request.url.params
        assert params["layers"] == "tiger:poi" and params["styles"] == ""
        assert "SLD_BODY" not in params
        calls.append(1)
        return httpx.Response(200, content=_png(blank=False), headers={"content-type": "image/png"})

    with ReadOnlySldClient("http://127.0.0.1:18881/geoserver", scale_style=False) as client:
        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        review = review_wms_views(client, "tiger", "poi", _CLOSE)
    assert review.selected_sample == 0 and review.status == "SUCCEEDED"
    assert len(calls) == 1


def test_outside_bbox_remains_review_without_adding_write_requests():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        calls.append(request.url.params["bbox"])
        return httpx.Response(200, content=_png(blank=True), headers={"content-type": "image/png"})

    with ReadOnlySldClient("http://127.0.0.1:18881/geoserver", scale_style=False) as client:
        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        review = review_wms_views(client, "tiger", "poi", _EMPTY)
    assert len(calls) == 5 and len(set(calls)) == 5
    assert review.status == "REVIEW_REQUIRED" and review.selected_sample == 0


def test_http_failure_is_not_accepted_as_map_evidence():
    with ReadOnlySldClient("http://127.0.0.1:18881/geoserver", scale_style=True) as client:
        client._http.close()
        client._http = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, content=_png(blank=False), headers={"content-type": "image/png"})
            ),
            trust_env=False,
        )
        with pytest.raises(GeoServerError) as caught:
            review_wms_views(client, "tiger", "poi", _BROAD)
        assert caught.value.code == "LOOPBACK_WMS_FAILED"
        assert client.request_count == 1
