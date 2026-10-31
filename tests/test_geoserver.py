from __future__ import annotations

import json

import httpx
import pytest

from geopublisher.geoserver import GeoServerClient, GeoServerError


def client(handler):
    return GeoServerClient(
        "https://example.invalid/geoserver",
        "publisher",
        "do-not-log-me",
        transport=httpx.MockTransport(handler),
    )


def test_resource_getter_unwraps_and_only_404_means_missing():
    def handler(request):
        if request.url.path.endswith("/missing.json"):
            return httpx.Response(404)
        return httpx.Response(200, json={"workspace": {"name": "maps"}})

    with client(handler) as gs:
        assert gs.get_workspace("maps") == {"name": "maps"}
        assert gs.get_workspace("missing") is None


@pytest.mark.parametrize("status,code", [(401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (500, "SERVER_ERROR")])
def test_read_errors_not_converted_to_absent_objects(status, code):
    with client(lambda req: httpx.Response(status, text="do-not-log-me")) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.get_workspace("maps")
    assert caught.value.code == code
    assert not caught.value.outcome_unknown
    assert "do-not-log-me" not in str(caught.value)


def test_probe_reads_geoserver_version():
    with client(
        lambda req: httpx.Response(
            200, json={"about": {"resource": [{"@name": "GeoServer", "Version": "2.24.4"}]}}
        )
    ) as gs:
        result = gs.probe()
    assert result["available"] is True
    assert result["geoserver_version"] == "2.24.4"


def test_probe_supports_version_endpoint_forbidden_without_retrying_auth():
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if "about" in request.url.path:
            return httpx.Response(403)
        return httpx.Response(200, json={"workspaces": {"workspace": []}})

    with client(handler) as gs:
        assert gs.probe()["warnings"] == ["VERSION_NOT_AUTHORIZED"]
    assert len(requests) == 2


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteError, httpx.RemoteProtocolError]
)
def test_unknown_write_result_is_not_retried(failure):
    requests = []

    def handler(request):
        requests.append(request)
        raise failure("do-not-log-me", request=request)

    with client(handler) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.bind_style("maps", "roads", "roads_style")
    assert len(requests) == 1
    assert caught.value.outcome_unknown is True
    assert "do-not-log-me" not in str(caught.value)


@pytest.mark.parametrize(
    "status,unknown",
    [
        (500, True),
        (502, True),
        (503, True),
        (408, True),
        (429, True),
        (401, False),
        (403, False),
        (409, False),
    ],
)
def test_write_status_classification(status, unknown):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="Authorization: secret")

    with client(handler) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.bind_style("maps", "roads", "style")
    assert caught.value.outcome_unknown is unknown
    assert len(requests) == 1
    assert "secret" not in json.dumps(caught.value.to_dict())


def test_redirect_never_forwards_credentials_to_another_origin():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.invalid/collect"})

    with client(handler) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.get_workspace("maps")
    assert caught.value.code == "REDIRECT_REJECTED"
    assert len(requests) == 1
    assert requests[0].url.host == "example.invalid"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:password@example.invalid/geoserver",
        "file:///tmp/geoserver",
        "https://example.invalid/geoserver?token=a",
        "https://example.invalid/geoserver#x",
        "https://example.invalid/a/../geoserver",
        "https://example.invalid/%2e%2e/geoserver",
        "https://example.invalid/geoserver\n",
    ],
)
def test_endpoint_rejects_credentials_and_ambiguous_urls(endpoint):
    with pytest.raises(GeoServerError, match="endpoint"):
        GeoServerClient(endpoint, "user", "password")


def test_tls_cannot_be_disabled():
    with pytest.raises(GeoServerError) as caught:
        GeoServerClient("https://example.invalid", "user", "password", verify=False)
    assert caught.value.code == "TLS_VERIFICATION_REQUIRED"


@pytest.mark.parametrize(
    "name", ["../roads", "roads/other", "ws:roads", "roads?recurse=true", "道路", "a%2fb", "a" * 129]
)
def test_invalid_resource_name_fails_before_network(name):
    def handler(request):
        pytest.fail("Invalid names must not reach the network.")

    with client(handler) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.get_layer("maps", name)
    assert caught.value.code == "INVALID_NAME"


@pytest.mark.parametrize(
    "kind,suffix,store_path,media",
    [
        ("shapefile", ".zip", "datastores", "application/zip"),
        ("geotiff", ".tif", "coveragestores", "image/tiff"),
    ],
)
def test_upload_is_create_only_streaming_and_disables_auto_publication(
    tmp_path, kind, suffix, store_path, media
):
    source = tmp_path / ("synthetic" + suffix)
    content = b"synthetic input" * 100
    source.write_bytes(content)
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        assert request.method == "PUT"
        assert f"/{store_path}/dataset/" in request.url.path
        assert request.url.params["configure"] == "none"
        assert request.headers["Content-Type"] == media
        assert int(request.headers["Content-Length"]) == len(content)
        assert request.read() == content
        return httpx.Response(201)

    with client(handler) as gs:
        result = gs.upload_dataset("maps", "dataset", kind, source, encoding="UTF-8")
    assert [r.method for r in requests] == ["GET", "PUT"]
    assert result == {"status_code": 201, "method": "PUT", "operation": "upload_dataset"}
    assert source.read_bytes() == content
    if kind == "shapefile":
        assert requests[-1].url.params["charset"] == "UTF-8"


def test_upload_refuses_existing_store_without_a_write(tmp_path):
    source = tmp_path / "input.zip"
    source.write_bytes(b"synthetic")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"dataStore": {"name": "dataset"}})

    with client(handler) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.upload_dataset("maps", "dataset", "shapefile", source)
    assert caught.value.code == "CONFLICT"
    assert [r.method for r in requests] == ["GET"]


def test_upload_timeout_after_bytes_sent_is_unknown(tmp_path):
    source = tmp_path / "input.tif"
    source.write_bytes(b"synthetic")
    writes = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        writes.append(request.read())
        raise httpx.ReadTimeout("response lost", request=request)

    with client(handler) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.upload_dataset("maps", "dataset", "geotiff", source)
    assert writes == [b"synthetic"]
    assert caught.value.outcome_unknown is True


@pytest.mark.parametrize(
    "kind,collection,wrapper",
    [("shapefile", "featuretypes", "featureType"), ("geotiff", "coverages", "coverage")],
)
def test_publish_resource_uses_correct_resource_and_native_name(kind, collection, wrapper):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        body = json.loads(request.content)
        assert request.url.path.endswith("/" + collection)
        assert body[wrapper]["name"] == "published"
        assert body[wrapper]["nativeName"] == "原始数据"
        assert body[wrapper]["srs"] == "EPSG:4326"
        return httpx.Response(201)

    with client(handler) as gs:
        gs.publish_resource("maps", "store", "published", kind, native_name="原始数据", crs="EPSG:4326")
    assert [r.method for r in requests] == ["GET", "POST"]


def test_get_resource_has_distinct_store_and_layer_names():
    def handler(request):
        assert (
            request.url.path
            == "/geoserver/rest/workspaces/maps/datastores/store_unique/featuretypes/roads.json"
        )
        return httpx.Response(200, json={"featureType": {"name": "roads", "nativeName": "source"}})

    with client(handler) as gs:
        assert gs.get_resource("maps", "store_unique", "roads", "shapefile")["nativeName"] == "source"


def test_style_creation_and_binding_are_workspace_scoped(tmp_path):
    source = tmp_path / "synthetic.sld"
    source.write_text("<StyledLayerDescriptor/>", encoding="utf-8")
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        if request.method == "POST":
            assert request.url.path == "/geoserver/rest/workspaces/maps/styles"
            assert request.url.params["name"] == "roads_style"
            assert request.read() == source.read_bytes()
        if request.method == "PUT":
            assert json.loads(request.content) == {
                "layer": {"defaultStyle": {"name": "roads_style", "workspace": "maps"}}
            }
        return httpx.Response(201 if request.method == "POST" else 200)

    with client(handler) as gs:
        gs.upload_style("maps", "roads_style", source)
        gs.bind_style("maps", "roads", "roads_style")
    assert [r.method for r in requests] == ["GET", "POST", "PUT"]


def test_style_existing_name_is_not_overwritten(tmp_path):
    path = tmp_path / "style.sld"
    path.write_text("<StyledLayerDescriptor/>")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"style": {"name": "existing"}})

    with client(handler) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.upload_style("maps", "existing", path)
    assert caught.value.code == "CONFLICT"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "version,expected_bbox,crs_key",
    [
        ("1.1.1", "100,20,110,30", "srs"),
        ("1.3.0", "20,100,30,110", "crs"),
    ],
)
def test_map_checks_default_style_and_axis_order(version, expected_bbox, crs_key):
    def handler(request):
        assert request.url.path == "/geoserver/maps/wms"
        assert request.url.params["layers"] == "maps:roads"
        assert "styles" in request.url.params
        assert request.url.params["styles"] == ""
        assert request.url.params["bbox"] == expected_bbox
        assert request.url.params[crs_key] == "EPSG:4326"
        assert request.url.params["transparent"] == "true"
        return httpx.Response(
            200, headers={"Content-Type": "image/png", "Set-Cookie": "do-not-store"}, content=b"png"
        )

    with client(handler) as gs:
        response = gs.get_map("maps", "roads", bbox=[100, 20, 110, 30], version=version)
    assert response.content == b"png"
    assert not hasattr(response, "request")
    assert "set-cookie" not in response.headers


def test_wfs_sample_is_explicit_bounded_and_validated():
    def handler(request):
        assert request.url.path == "/geoserver/maps/ows"
        assert request.url.params["typeNames"] == "maps:roads"
        assert request.url.params["count"] == "2"
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {"id": 1}, "geometry": None}],
            },
        )

    with client(handler) as gs:
        assert len(gs.get_features("maps", "roads", limit=2)["features"]) == 1


def test_wfs_exception_document_cannot_pass_as_success():
    with client(lambda req: httpx.Response(200, text="<ExceptionReport>hidden</ExceptionReport>")) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.get_features("maps", "roads")
    assert caught.value.code == "INVALID_RESPONSE"


def test_exact_deletions_never_recurse_or_purge_data():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "DELETE"
        assert request.url.params["recurse"] == "false"
        assert request.url.params.get("purge") not in {"all", "true"}
        return httpx.Response(200)

    with client(handler) as gs:
        gs.delete_layer("maps", "roads")
        gs.delete_resource("maps", "store", "roads", "shapefile")
        gs.delete_store("maps", "store", "shapefile")
        gs.delete_store("maps", "raster", "geotiff")
        gs.delete_style("maps", "roads_style")
    assert len(requests) == 5
    assert requests[3].url.params["purge"] == "none"


def test_server_metadata_redacts_embedded_credentials():
    payload = {
        "dataStore": {
            "name": "store",
            "connectionParameters": {
                "entry": [
                    {"@key": "passwd", "$": "server-secret"},
                    {"@key": "url", "$": "file:data/maps/input.shp"},
                ]
            },
            "password": "other-secret",
            "link": "https://user:secret@example.invalid/path",
        }
    }
    with client(lambda req: httpx.Response(200, json=payload)) as gs:
        result = gs.get_store("maps", "store", "shapefile")
    text = json.dumps(result)
    assert "server-secret" not in text
    assert "other-secret" not in text
    assert "user:secret" not in text
    assert "input.shp" in text


def test_oversized_response_is_bounded_and_safe():
    with client(lambda req: httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 1))) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.get_workspace("maps")
    assert caught.value.code == "RESPONSE_TOO_LARGE"


def test_response_failure_after_write_acknowledgement_remains_unknown():
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"first"
            raise httpx.ReadError("connection broke")

    with client(lambda req: httpx.Response(200, stream=BrokenStream())) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.bind_style("maps", "roads", "style")
    assert caught.value.outcome_unknown is True


def test_invalid_json_structure_is_not_treated_as_a_resource():
    with client(lambda req: httpx.Response(200, json={"success": True})) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.get_layer("maps", "roads")
    assert caught.value.code == "INVALID_RESPONSE"


def test_raw_style_body_is_read_from_explicit_workspace_endpoint():
    sld = b'<StyledLayerDescriptor xmlns="http://www.opengis.net/sld" version="1.0.0"/>'

    def handler(request):
        assert request.method == "GET"
        assert request.url.host == "example.invalid"
        assert request.url.path == "/geoserver/rest/workspaces/maps/styles/roads_style.sld"
        assert request.url.params["raw"] == "true"
        return httpx.Response(200, content=sld, headers={"Content-Type": "application/vnd.ogc.sld+xml"})

    with client(handler) as gs:
        assert gs.get_style_content("maps", "roads_style") == sld


@pytest.mark.parametrize(
    "body",
    [
        b"<html>login</html>",
        b"<ServiceExceptionReport>secret</ServiceExceptionReport>",
        b"<StyledLayerDescriptor>",
        b'<!DOCTYPE root [<!ENTITY token "hidden">]><StyledLayerDescriptor>&token;</StyledLayerDescriptor>',
    ],
)
def test_style_body_rejects_error_pages_and_unsafe_xml(body):
    with client(lambda req: httpx.Response(200, content=body)) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.get_style_content("maps", "roads_style")
    assert caught.value.code == "INVALID_STYLE_RESPONSE"
    assert "secret" not in str(caught.value)
    assert "hidden" not in str(caught.value)


def test_missing_style_content_is_an_explicit_error():
    with client(lambda req: httpx.Response(404)) as gs:
        with pytest.raises(GeoServerError) as caught:
            gs.get_style_content("maps", "roads_style")
    assert caught.value.code == "NOT_FOUND"
