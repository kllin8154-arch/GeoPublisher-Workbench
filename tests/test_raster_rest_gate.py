"""No-network safety tests for disposable REST raster integration tooling."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest


def load_rest_gate():
    script = Path(__file__).resolve().parents[1] / "scripts" / "integration_raster_rest.py"
    spec = importlib.util.spec_from_file_location("geopub_disposable_rest_gate", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REST = load_rest_gate()


@pytest.mark.parametrize(
    "url",
    (
        "https://127.0.0.1:18883/geoserver",
        "http://127.0.0.1:8080/geoserver",
        "http://example.com/geoserver",
        "http://127.0.0.1:18883/geoserver?unsafe=true",
        "http://user:password@127.0.0.1:18883/geoserver",
    ),
)
def test_gate_refuses_any_non_exact_loopback_endpoint(tmp_path, url):
    root = tmp_path / "GeoPublisher-Workbench-validation" / "geoserver-disposable-rest-20261101"
    root.mkdir(parents=True)
    with pytest.raises(ValueError, match="disposable loopback"):
        REST.validate_lab(root, tmp_path / "sources", url, tmp_path / "result")


def test_gate_rejects_modified_or_unrecognized_raster_inputs(tmp_path):
    base = tmp_path / "GeoPublisher-Workbench-validation"
    root = base / "geoserver-disposable-rest-20261101"
    root.mkdir(parents=True)
    (root / "DISPOSABLE-REST-ONLY.txt").write_text("Test-only", encoding="utf-8")
    (root / "server.pid").write_text("1234", encoding="utf-8")
    sources = base / "rest-sources"
    sources.mkdir()
    for name in REST._FIXTURES:
        (sources / name).write_bytes(b"untrusted or modified TIFF")
    with pytest.raises(ValueError, match="not the expected deterministic"):
        REST.validate_lab(
            root, sources, "http://127.0.0.1:18883/geoserver", base / "unused"
        )
    existing = base / "existing-result"
    existing.mkdir()
    with pytest.raises(ValueError, match="new"):
        REST.validate_lab(
            root, sources, "http://127.0.0.1:18883/geoserver", existing
        )


def test_lost_response_transport_never_blindly_retries_the_same_put():
    calls = []

    def mock_server(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, content=b"synthetic acknowledgement")

    transport = REST.LoseTiffUploadResponse(httpx.MockTransport(mock_server))
    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.put("http://127.0.0.1:18883/geoserver/rest/workspaces/test/coveragestores/a/file.geotiff")
        assert transport.dropped is True
        assert transport.write_count == 1
        assert calls == [
            ("PUT", "/geoserver/rest/workspaces/test/coveragestores/a/file.geotiff")
        ]
        # No automatic retry occurs within the transport, and a separate GET
        # remains read-only even after the lost write response.
        client.get("http://127.0.0.1:18883/geoserver/rest/workspaces/test")
    assert len(calls) == 2
    assert transport.write_count == 1


def test_audited_client_counts_only_mutation_methods_without_payloads():
    calls = []

    def mock_server(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"workspace": {"name": "test"}})

    with REST.AuditedClient(
        "http://127.0.0.1:18883/geoserver", "synthetic-user", "do-not-save-me",
        transport=httpx.MockTransport(mock_server),
    ) as client:
        client._request("GET", "/rest/workspaces/test.json", operation="read workspace")
        client._request(
            "POST", "/rest/workspaces", operation="create workspace",
            payload={"workspace": {"name": "test"}},
        )
        assert client.writes == ["create workspace"]
    assert calls == ["GET", "POST"]
    assert "do-not-save-me" not in str(client.writes)
