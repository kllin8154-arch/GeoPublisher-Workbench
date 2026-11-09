"""SSH transport for immutable vector/GeoTIFF publish plans; all fake targets."""
from __future__ import annotations

import json

import pytest

from geopublisher.server_deploy import LinuxTarget, ServerDeployError
from geopublisher.server_plan import publish_plan_via_tunnel, validate_server_binding


def target():
    return LinuxTarget(mode="local", root="/srv/geopublisher/synthetic")


def env_plan(host="127.0.0.1", port=8080):
    return {
        "task_id": "example-only",
        "environment": {"base_url": f"http://{host}:{port}/geoserver"},
        "output_dir": "/tmp/synthetic-output",
    }


def test_server_binding_matches_only_exact_target():
    profile = target()
    validate_server_binding(env_plan(), profile)
    for url in ("http://192.168.0.5:8080/geoserver", "http://127.0.0.1:8888/geoserver",
                "https://127.0.0.1:8080/geoserver", "http://127.0.0.1:8080/other"):
        plan = env_plan()
        plan["environment"]["base_url"] = url
        with pytest.raises(ServerDeployError) as exc:
            validate_server_binding(plan, profile)
        assert exc.value.code == "PLAN_SERVER_MISMATCH"


def test_remote_ssh_binding_refuses_wrong_plan_host(tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("not-used")
    profile = LinuxTarget(mode="ssh", root="/zbcx/geopub-test",
                          host="192.168.77.22", username="tester", auth="agent",
                          known_hosts=str(known))
    validate_server_binding(env_plan("192.168.77.22"), profile)
    with pytest.raises(ServerDeployError):
        validate_server_binding(env_plan("192.168.77.23"), profile)


def test_publish_plan_uses_existing_engine_not_new_unjournaled_upload(tmp_path, monkeypatch):
    planfile = tmp_path / "plan.json"
    planfile.write_text(json.dumps(env_plan()), encoding="utf-8")
    calls = []
    monkeypatch.setattr("geopublisher.server_plan.load_plan", lambda p: env_plan())
    monkeypatch.setattr("geopublisher.server_plan.verify_artifacts", lambda p: calls.append("verify"))
    class FakeLocal:
        def run(self, command, *args, **kw):
            assert command == "os"
            calls.append("linux-check")
            return "Linux"
    class Port:
        geoserver_url = "http://127.0.0.1:19999/geoserver"
        def __enter__(self): return self
        def __exit__(self, *_args): pass
    monkeypatch.setattr("geopublisher.server_plan.open_service",
                        lambda session, **kw: Port())
    class Client:
        def __init__(self, base, username, password, **kw):
            assert base == Port.geoserver_url
            assert username == "publisher"
            assert password == "never-written"
            calls.append("connection-created")
        def __enter__(self): return self
        def __exit__(self, *_args): pass
    monkeypatch.setattr("geopublisher.server_plan.GeoServerClient", Client)
    monkeypatch.setattr("geopublisher.server_plan.run_plan",
                        lambda p, **kw: (calls.append("journaled-run"), {"status": "SUCCEEDED"})[1])
    result = publish_plan_via_tunnel(
        target(), planfile, FakeLocal(), username="publisher", password="never-written"
    )
    assert calls == ["verify", "linux-check", "connection-created", "journaled-run"]
    assert result["transport"] == "LINUX_LOOPBACK"
    assert result["business_sql_executed"] is False
    assert "never-written" not in json.dumps(result)


def test_mismatched_plan_prevents_server_access(tmp_path, monkeypatch):
    planfile = tmp_path / "plan.json"
    planfile.write_text("synthetic-only")
    plan = env_plan(host="192.168.77.22")
    monkeypatch.setattr("geopublisher.server_plan.load_plan", lambda p: plan)
    monkeypatch.setattr("geopublisher.server_plan.open_service",
                        lambda *_args, **_kw: pytest.fail("No network before plan target validation"))
    with pytest.raises(ServerDeployError) as err:
        publish_plan_via_tunnel(target(), planfile, object(), username="u", password="p")
    assert err.value.code == "PLAN_SERVER_MISMATCH"
