"""Automatic GeoServer-managed data delivery: zero Xftp/SSH/manual ZIP steps.

The fake server persists uploaded bytes independently of the Windows plan.
Only synthetic fixtures are used; no site credentials or GIS files leave LAN.
"""
from __future__ import annotations

from pathlib import Path

from test_engine import StatefulGeoServer, make_plan

from geopublisher.engine import run_plan


def test_full_batch_is_preflighted_before_the_first_rest_upload(tmp_path):
    plan = make_plan(tmp_path, count=24)
    server = StatefulGeoServer()
    # Last of 24 already exists: never upload first 23 before detecting it.
    server.layers[("maps", "layer_23")] = {"name": "layer_23", "owner": "foreign"}
    result = run_plan(plan, client=server)
    assert result["status"] == "FAILED"
    assert result["code"] == "BATCH_RESOURCE_CONFLICT"
    assert server.mutations == []
    assert not server.uploaded_files
    assert result["data_transfer"] == "AUTOMATIC_GEOSERVER_REST_UPLOAD"
    assert result["server_storage"] == "GEOSERVER_MANAGED_DATA_DIR"


def test_full_batch_is_published_by_rest_upload_without_ssh_or_manual_transfer(tmp_path):
    plan = make_plan(tmp_path, count=24)
    server = StatefulGeoServer()
    result = run_plan(plan, client=server)
    assert result["status"] == "SUCCEEDED"
    assert len(server.uploaded_files) == 24
    assert len(server.stores) == 24
    assert len(server.layers) == 24
    assert len(server.styles) == 24
    assert all(
        store["url"].startswith("file:data/maps/")
        for store in server.stores.values()
    )
    assert result["data_transfer"] == "AUTOMATIC_GEOSERVER_REST_UPLOAD"
    assert result["server_storage"] == "GEOSERVER_MANAGED_DATA_DIR"
    # The client can be disconnected once GeoServer received each copy.
    for resource in plan["resources"]:
        Path(resource["inspection"]["source"]).unlink()
    assert len(server.uploaded_files) == 24
    assert len(server.layers) == 24


def test_staged_server_files_and_auto_rest_are_explicitly_distinguished(tmp_path):
    plan = make_plan(tmp_path, count=2)
    server = StatefulGeoServer()
    paths = {
        r["resource_id"]: f"/zbcx/geopublisher/releases/test/data/{r['resource_id']}/synthetic.shp"
        for r in plan["resources"]
    }
    # An external-file publishing adapter is deliberately not provided
    # here; the main report format still must label its delivery mode.
    from geopublisher.engine import Publisher
    from geopublisher.journal import Journal

    with Journal(tmp_path / "status.sqlite") as journal:
        journal.create_task(plan["task_id"], plan)
        publisher = Publisher(plan, journal, server, staged_paths=paths)
        report = publisher.report("FAILED", [])
        assert report["data_transfer"] == "VERIFIED_SERVER_PATH"
        assert report["server_storage"] == "EXISTING_SERVER_FILES"
        assert server.mutations == []
