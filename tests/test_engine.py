"""Stateful failure tests for publishing ownership and recovery.

This fake models persistent remote state independently of the SQLite journal.
It can lose a response after changing that state, which is the failure mode a
stateless HTTP mock alone cannot exercise.
"""

from __future__ import annotations

import copy
import io
import json
import threading
import uuid
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from geopublisher.config import ConfigError, Environment
from geopublisher.engine import RecoveryRequired, rollback_plan, run_plan
from geopublisher.geoserver import GeoServerError, MapResponse
from geopublisher.journal import Journal
from geopublisher.plans import digest_file, fingerprint


def make_plan(tmp_path, *, count=1, strategy="fail", enable_wfs=False, kinds=None):
    task_id = str(uuid.uuid4())
    output = tmp_path / "tasks" / task_id
    output.mkdir(parents=True)
    resources = []
    for index in range(count):
        rid = f"r{index + 1:04d}"
        directory = output / rid
        directory.mkdir()
        kind = kinds[index] if kinds else "shapefile"
        source = directory / ("synthetic-source.shp" if kind == "shapefile" else "synthetic-source.tif")
        source.write_text(f"synthetic dataset {index}", encoding="utf-8")
        artifact = directory / ("dataset.zip" if kind == "shapefile" else "dataset.tif")
        artifact.write_bytes(b"synthetic-package-" + str(index).encode())
        style = directory / "style.sld"
        style.write_text('<StyledLayerDescriptor xmlns="http://www.opengis.net/sld" version="1.0.0"/>')
        resources.append(
            {
                "resource_id": rid,
                "target_name": f"layer_{index}",
                "store_name": f"store_{index}_{task_id.replace('-', '')[:12]}",
                "style_name": f"style_{index}_{task_id.replace('-', '')[:12]}",
                "kind": kind,
                "inspection": {
                    "source": str(source),
                    "crs": "EPSG:4326",
                    "wgs84_bbox": [100, 20, 101, 21],
                    "native_name": "synthetic",
                    "encoding": "UTF-8",
                    "feature_count": 1 if kind == "shapefile" else None,
                },
                "artifact": {
                    "path": str(artifact),
                    "sha256": digest_file(artifact),
                    "source_hashes": [
                        {"path": str(source), "sha256": digest_file(source), "size": source.stat().st_size}
                    ],
                },
                "style": {"path": str(style), "sha256": digest_file(style)},
                "blocking_errors": [],
                "warnings": [],
            }
        )
    plan = {
        "schema_version": 1,
        "task_id": task_id,
        "environment": Environment(
            "synthetic", "https://example.invalid/geoserver", "maps", "/unused-gdal"
        ).to_dict(),
        "strategy": strategy,
        "enable_wfs": enable_wfs,
        "output_dir": str(output),
        "ready": True,
        "resources": resources,
        "catalog_mode": "export-only",
    }
    plan["plan_hash"] = fingerprint(plan)
    return plan


def test_new_source_structure_blocks_replay_but_does_not_prevent_owned_cleanup(tmp_path):
    from geopublisher.imports import prepare_imports

    plan = make_plan(tmp_path, count=2, kinds=["geotiff", "geotiff"])
    plan["schema_version"] = 2
    plan["import_intent"] = prepare_imports(
        [r["inspection"]["source"] for r in plan["resources"]], raster_mode="independent"
    )
    plan.pop("plan_hash")
    plan["plan_hash"] = fingerprint(plan)
    server = StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    first_write_count = len(server.mutations)
    (Path(plan["output_dir"]) / "pyramid.properties").write_text(
        "Levels=1,1\nLevelsDirs=full preview\nLevelsNum=2\nGeneralBounds=0,0 1,1\n"
    )
    with pytest.raises(ConfigError) as caught:
        run_plan(plan, client=server)
    assert caught.value.code == "UNSUPPORTED_PYRAMID"
    assert len(server.mutations) == first_write_count
    for resource in plan["resources"]:
        Path(resource["inspection"]["source"]).unlink()
    result = rollback_plan(plan, client=server)
    assert result["status"] == "ROLLED_BACK"
    assert not server.layers and not server.stores and not server.styles
    assert len(server.uploaded_files) == 2


def journal_path(plan):
    return Path(plan["output_dir"]).parent / "geopublisher.sqlite"


class StatefulGeoServer:
    """A single-workspace server with independent metadata and style content."""

    def __init__(self):
        self.workspace_exists = True
        self.stores = {}
        self.resources = {}
        self.layers = {}
        self.styles = {}
        self.style_bodies = {}
        self.uploaded_files = {}
        self.mutations = []
        self.wfs_calls = []
        self.map_calls = 0
        self.failures = {}
        self.probe_failure = None
        self.map_failures = 0
        self.before_mutation = None
        self.after_bind = None
        self.after_map = None

    def fail_once(self, action, *, after=False, unknown=False, status_code=None):
        self.failures[action] = {
            "after": after,
            "error": GeoServerError(
                "INJECTED_FAILURE",
                "Synthetic transport failure.",
                outcome_unknown=unknown,
                status_code=status_code,
            ),
        }

    def _begin(self, action, name):
        if self.before_mutation:
            self.before_mutation(action, name)
        self.mutations.append((action, name))
        failure = self.failures.get(action)
        if failure and not failure["after"]:
            self.failures.pop(action)
            raise failure["error"]

    def _end(self, action):
        failure = self.failures.get(action)
        if failure and failure["after"]:
            self.failures.pop(action)
            raise failure["error"]
        return {"status_code": 200, "operation": action}

    def probe(self):
        if self.probe_failure:
            raise self.probe_failure
        return {"available": True, "geoserver_version": "2.24.4"}

    def get_workspace(self, workspace):
        assert workspace == "maps"
        return {"name": workspace} if self.workspace_exists else None

    def get_store(self, workspace, name, kind):
        return copy.deepcopy(self.stores.get((workspace, name, kind)))

    def get_resource(self, workspace, store, name, kind):
        return copy.deepcopy(self.resources.get((workspace, store, name, kind)))

    def get_layer(self, workspace, name):
        return copy.deepcopy(self.layers.get((workspace, name)))

    def get_style(self, workspace, name):
        return copy.deepcopy(self.styles.get((workspace, name)))

    def get_style_content(self, workspace, name):
        if (workspace, name) not in self.styles:
            raise GeoServerError("NOT_FOUND", "Synthetic style is absent.", status_code=404)
        return self.style_bodies[(workspace, name)]

    def upload_dataset(self, workspace, name, kind, path, *, encoding=None):
        self._begin("UPLOAD", name)
        assert (workspace, name, kind) not in self.stores, "A completed upload must never be replayed."
        self.stores[(workspace, name, kind)] = {
            "name": name,
            "enabled": True,
            "type": kind,
            "workspace": {"name": workspace},
            "url": f"file:data/{workspace}/{name}/dataset",
        }
        self.uploaded_files[(workspace, name)] = Path(path).read_bytes()
        return self._end("UPLOAD")

    def publish_resource(self, workspace, store, name, kind, *, native_name=None, crs=None):
        self._begin("PUBLISH", name)
        assert (workspace, name) not in self.layers, "A completed publication must never be replayed."
        assert (workspace, store, kind) in self.stores
        self.resources[(workspace, store, name, kind)] = {
            "name": name,
            "nativeName": native_name or "synthetic",
            "srs": crs,
            "store": {"name": f"{workspace}:{store}"},
        }
        self.layers[(workspace, name)] = {
            "name": name,
            "enabled": True,
            "type": "VECTOR" if kind == "shapefile" else "RASTER",
            "defaultStyle": {"name": "generic"},
            "resource": {"name": f"{workspace}:{name}"},
        }
        return self._end("PUBLISH")

    def upload_style(self, workspace, name, path):
        self._begin("INSTALL_STYLE", name)
        assert (workspace, name) not in self.styles, "A completed style creation must never be replayed."
        self.styles[(workspace, name)] = {"name": name, "filename": name + ".sld", "format": "sld"}
        self.style_bodies[(workspace, name)] = Path(path).read_bytes()
        return self._end("INSTALL_STYLE")

    def bind_style(self, workspace, layer, style):
        self._begin("BIND_STYLE", layer)
        self.layers[(workspace, layer)]["defaultStyle"] = {"name": style}
        self.layers[(workspace, layer)].setdefault("queryable", True)
        self.layers[(workspace, layer)].setdefault("opaque", False)
        if self.after_bind:
            self.after_bind(self.layers[(workspace, layer)])
        return self._end("BIND_STYLE")

    def get_map(self, workspace, layer, *, bbox, crs, version, width, height):
        self.map_calls += 1
        if self.map_failures:
            self.map_failures -= 1
            raise GeoServerError("REQUEST_TIMEOUT", "Synthetic read timeout.")
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle((10, 10, width // 2, height // 2), fill=(180, 50, 30, 255))
        stream = io.BytesIO()
        image.save(stream, "PNG")
        if self.after_map:
            self.after_map(self)
        return MapResponse(200, {"content-type": "image/png"}, stream.getvalue())

    def get_features(self, workspace, layer, *, limit):
        self.wfs_calls.append((workspace, layer))
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"sample_id": 1},
                    "geometry": {"type": "Point", "coordinates": [100.5, 20.5]},
                }
            ],
        }

    def delete_layer(self, workspace, name):
        self._begin("DELETE_LAYER", name)
        self.layers.pop((workspace, name))
        return self._end("DELETE_LAYER")

    def delete_resource(self, workspace, store, name, kind):
        self._begin("DELETE_RESOURCE", name)
        assert (workspace, name) not in self.layers
        self.resources.pop((workspace, store, name, kind))
        return self._end("DELETE_RESOURCE")

    def delete_store(self, workspace, name, kind):
        self._begin("DELETE_STORE", name)
        assert not any(key[0] == workspace and key[1] == name for key in self.resources)
        self.stores.pop((workspace, name, kind))
        return self._end("DELETE_STORE")

    def delete_style(self, workspace, name):
        self._begin("DELETE_STYLE", name)
        assert not any(item.get("defaultStyle", {}).get("name") == name for item in self.layers.values())
        self.styles.pop((workspace, name))
        return self._end("DELETE_STYLE")


def mutation_counts(server):
    return Counter(action for action, _ in server.mutations)


def test_all_writes_have_durable_intent_before_remote_mutation(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()

    def confirm_intent(action, name):
        with Journal(journal_path(plan)) as audit:
            last = audit.operations(plan["task_id"])[-1]
            assert last["action"] == action
            assert last["outcome"] == "INTENT"

    server.before_mutation = confirm_intent
    report = run_plan(plan, client=server)
    assert report["status"] == "SUCCEEDED"
    assert report["catalog_status"] == "NOT_REGISTERED"
    assert mutation_counts(server) == {"UPLOAD": 1, "PUBLISH": 1, "INSTALL_STYLE": 1, "BIND_STYLE": 1}
    with Journal(journal_path(plan)) as audit:
        assert {op["outcome"] for op in audit.operations(plan["task_id"])} == {"SUCCEEDED"}
        assert {obj["kind"] for obj in audit.objects(plan["task_id"])} == {
            "store",
            "resource",
            "layer",
            "style",
        }


def test_completed_plan_replay_never_repeats_successful_writes(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    original_writes = list(server.mutations)
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    assert server.mutations == original_writes
    assert server.map_calls == 2


def test_resume_after_wms_read_timeout_reuses_published_objects(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    server.map_failures = 1
    assert run_plan(plan, client=server)["status"] == "FAILED"
    original_writes = list(server.mutations)
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    assert server.mutations == original_writes


def test_skip_does_not_claim_existing_layer_or_register_current_input(tmp_path):
    plan, server = make_plan(tmp_path, strategy="skip"), StatefulGeoServer()
    server.layers[("maps", "layer_0")] = {"name": "layer_0", "owner": "external"}
    report = run_plan(plan, client=server)
    assert report["status"] == "SKIPPED"
    assert not server.mutations
    assert not server.map_calls
    with Journal(journal_path(plan)) as audit:
        assert not audit.objects(plan["task_id"])
    bindings = json.loads((Path(plan["output_dir"]) / "catalog-bindings.json").read_text())
    assert bindings["layers"] == []


def test_fail_strategy_leaves_foreign_layer_untouched(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    foreign = {"name": "layer_0", "owner": "external"}
    server.layers[("maps", "layer_0")] = copy.deepcopy(foreign)
    report = run_plan(plan, client=server)
    assert report["status"] == "FAILED"
    assert report["resources"][0]["code"] == "RESOURCE_CONFLICT"
    assert not server.mutations
    assert server.layers[("maps", "layer_0")] == foreign


@pytest.mark.parametrize("action", ["UPLOAD", "PUBLISH", "INSTALL_STYLE", "BIND_STYLE"])
def test_lost_write_response_stops_and_is_never_blindly_retried(tmp_path, action):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    server.fail_once(action, after=True, unknown=True)
    assert run_plan(plan, client=server)["status"] == "MANUAL_ACTION_REQUIRED"
    writes = list(server.mutations)
    assert run_plan(plan, client=server)["status"] == "MANUAL_ACTION_REQUIRED"
    assert server.mutations == writes
    with pytest.raises(RecoveryRequired):
        rollback_plan(plan, client=server)
    assert server.mutations == writes


def test_unknown_before_binding_still_requires_reconciliation_not_replay(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    server.fail_once("BIND_STYLE", unknown=True)
    assert run_plan(plan, client=server)["status"] == "MANUAL_ACTION_REQUIRED"
    writes = list(server.mutations)
    assert run_plan(plan, client=server)["status"] == "MANUAL_ACTION_REQUIRED"
    assert server.mutations == writes
    assert server.layers[("maps", "layer_0")]["defaultStyle"] == {"name": "generic"}


def test_known_rejected_style_create_can_retry_without_duplicate_data(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    server.fail_once("INSTALL_STYLE", status_code=403)
    assert run_plan(plan, client=server)["status"] == "FAILED"
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    assert mutation_counts(server) == {"UPLOAD": 1, "PUBLISH": 1, "INSTALL_STYLE": 2, "BIND_STYLE": 1}
    assert len(server.styles) == 1


def test_same_style_metadata_with_changed_sld_body_blocks_resume(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    resource = plan["resources"][0]
    server.style_bodies[("maps", resource["style_name"])] += b"\n<!-- external edit -->"
    writes = list(server.mutations)
    assert run_plan(plan, client=server)["status"] == "MANUAL_ACTION_REQUIRED"
    assert server.mutations == writes


def test_external_layer_property_change_blocks_resume_and_cleanup(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    server.layers[("maps", "layer_0")]["enabled"] = False
    writes = list(server.mutations)
    assert run_plan(plan, client=server)["status"] == "MANUAL_ACTION_REQUIRED"
    with pytest.raises(RecoveryRequired):
        rollback_plan(plan, client=server)
    assert server.mutations == writes


def test_bind_does_not_absorb_unrelated_concurrent_changes_into_ownership(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    server.after_bind = lambda layer: layer.update(enabled=False)
    assert run_plan(plan, client=server)["status"] == "MANUAL_ACTION_REQUIRED"
    with Journal(journal_path(plan)) as audit:
        layer = next(obj for obj in audit.objects(plan["task_id"]) if obj["kind"] == "layer")
        assert layer["details"]["snapshot"]["enabled"] is True
        assert audit.operations(plan["task_id"])[-1]["outcome"] == "UNKNOWN"


def test_cleanup_is_scoped_to_owned_objects_and_keeps_uploaded_files(tmp_path):
    plan, server = make_plan(tmp_path, count=2), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    server.layers[("maps", "unrelated")] = {"name": "unrelated", "defaultStyle": {"name": "external"}}
    server.styles[("maps", "external")] = {"name": "external", "filename": "external.sld"}
    server.stores[("maps", "external", "shapefile")] = {"name": "external"}
    server.style_bodies[("maps", "external")] = b"external"
    source_bytes = {
        r["artifact"]["source_hashes"][0]["path"]: Path(
            r["artifact"]["source_hashes"][0]["path"]
        ).read_bytes()
        for r in plan["resources"]
    }
    uploaded = copy.deepcopy(server.uploaded_files)
    result = rollback_plan(plan, client=server)
    assert result["status"] == "ROLLED_BACK"
    assert result["server_data_retained"] is True
    assert len(result["removed"]) == 8
    assert server.layers == {
        ("maps", "unrelated"): {"name": "unrelated", "defaultStyle": {"name": "external"}}
    }
    assert set(server.styles) == {("maps", "external")}
    assert set(server.stores) == {("maps", "external", "shapefile")}
    assert server.uploaded_files == uploaded
    assert all(Path(path).read_bytes() == content for path, content in source_bytes.items())


def test_cleanup_checks_all_objects_before_first_deletion(tmp_path):
    plan, server = make_plan(tmp_path, count=2), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    # The first resource is changed; cleanup normally processes resources in
    # reverse order. The second resource must not be deleted before discovery.
    first = plan["resources"][0]
    server.style_bodies[("maps", first["style_name"])] += b"\n<!-- external change -->"
    writes = list(server.mutations)
    with pytest.raises(RecoveryRequired):
        rollback_plan(plan, client=server)
    assert server.mutations == writes
    assert len(server.layers) == 2
    with Journal(journal_path(plan)) as audit:
        event = next(event for event in audit.events(plan["task_id"]) if event["code"] == "CLEANUP_BLOCKED")
        assert event["level"] == "ERROR"
        assert event["details"]["removed_count"] == 0
    report = json.loads((Path(plan["output_dir"]) / "cleanup-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "MANUAL_ACTION_REQUIRED"
    assert report["removed"] == []
    assert report["server_data_retained"] is True


def test_unknown_delete_stops_further_cleanup_and_cannot_be_replayed(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    server.fail_once("DELETE_LAYER", after=True, unknown=True)
    with pytest.raises(GeoServerError):
        rollback_plan(plan, client=server)
    assert mutation_counts(server)["DELETE_LAYER"] == 1
    assert mutation_counts(server)["DELETE_RESOURCE"] == 0
    with Journal(journal_path(plan)) as audit:
        assert audit.get_task(plan["task_id"])["status"] == "MANUAL_ACTION_REQUIRED"
        assert audit.operations(plan["task_id"])[-1]["outcome"] == "UNKNOWN"
        assert any(event["code"] == "CLEANUP_BLOCKED" for event in audit.events(plan["task_id"]))
    writes = list(server.mutations)
    with pytest.raises(RecoveryRequired):
        rollback_plan(plan, client=server)
    assert server.mutations == writes


def test_cleaned_task_cannot_republish_with_stale_ownership(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    rollback_plan(plan, client=server)
    writes = list(server.mutations)
    with pytest.raises(ConfigError) as caught:
        run_plan(plan, client=server)
    assert caught.value.code == "TASK_ROLLED_BACK"
    assert server.mutations == writes


@pytest.mark.parametrize("failure", ["probe", "workspace"])
def test_pre_resource_failure_is_persisted_and_does_not_leave_running_task(tmp_path, failure):
    plan, server = make_plan(tmp_path, count=2), StatefulGeoServer()
    if failure == "probe":
        server.probe_failure = GeoServerError("CONNECTION_ERROR", "Synthetic connection error.")
    else:
        server.workspace_exists = False
    report = run_plan(plan, client=server)
    assert report["status"] == "FAILED"
    assert len(report["resources"]) == 2
    assert {resource["status"] for resource in report["resources"]} == {"NOT_STARTED"}
    with Journal(journal_path(plan)) as audit:
        assert audit.get_task(plan["task_id"])["status"] == "FAILED"
    assert not server.mutations


def test_unknown_write_reports_all_unstarted_resources(tmp_path):
    plan, server = make_plan(tmp_path, count=3), StatefulGeoServer()
    server.fail_once("UPLOAD", after=True, unknown=True)
    report = run_plan(plan, client=server)
    assert report["status"] == "MANUAL_ACTION_REQUIRED"
    assert [resource["status"] for resource in report["resources"]] == [
        "MANUAL_ACTION_REQUIRED",
        "NOT_STARTED",
        "NOT_STARTED",
    ]
    assert mutation_counts(server)["UPLOAD"] == 1


def test_cancel_at_safe_boundary_keeps_completed_resource_and_resumes(tmp_path):
    plan, server = make_plan(tmp_path, count=3), StatefulGeoServer()
    cancel = threading.Event()

    def completed(event):
        if event["code"] == "SUCCEEDED":
            cancel.set()

    report = run_plan(plan, client=server, cancel_event=cancel, on_event=completed)
    assert report["status"] == "CANCELLED"
    assert [resource["status"] for resource in report["resources"]] == [
        "SUCCEEDED",
        "CANCELLED",
        "NOT_STARTED",
    ]
    assert len(server.layers) == 1
    report = run_plan(plan, client=server)
    assert report["status"] == "SUCCEEDED"
    assert mutation_counts(server) == {"UPLOAD": 3, "PUBLISH": 3, "INSTALL_STYLE": 3, "BIND_STYLE": 3}


def test_plan_or_input_changes_are_rejected_before_network_mutations(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    changed = copy.deepcopy(plan)
    changed["strategy"] = "skip"
    with pytest.raises(ConfigError) as caught:
        run_plan(changed, client=server)
    assert caught.value.code == "PLAN_CHANGED"
    source = Path(plan["resources"][0]["artifact"]["source_hashes"][0]["path"])
    source.write_text("source was changed after preflight")
    with pytest.raises(ConfigError) as caught:
        run_plan(plan, client=server)
    assert caught.value.code == "SOURCE_CHANGED"
    assert not server.mutations


def test_unjournaled_same_name_store_is_never_adopted(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    resource = plan["resources"][0]
    server.stores[("maps", resource["store_name"], "shapefile")] = {
        "name": resource["store_name"],
        "owner": "external",
    }
    assert run_plan(plan, client=server)["status"] == "MANUAL_ACTION_REQUIRED"
    assert not server.mutations


def test_wfs_runs_only_for_enabled_vector_resources(tmp_path):
    plan, server = (
        make_plan(tmp_path, count=2, kinds=["shapefile", "geotiff"], enable_wfs=True),
        StatefulGeoServer(),
    )
    report = run_plan(plan, client=server)
    assert report["status"] == "SUCCEEDED"
    assert server.wfs_calls == [("maps", "layer_0")]
    assert report["resources"][1]["wfs"] is None


def test_resource_advertised_true_materialization_does_not_break_replay(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()

    def materialize_defaults(remote):
        for resource in remote.resources.values():
            resource.setdefault("advertised", True)

    server.after_map = materialize_defaults
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    writes = list(server.mutations)
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    assert server.mutations == writes


def test_explicit_resource_advertised_false_is_still_an_external_change(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    for resource in server.resources.values():
        resource["advertised"] = False
    writes = list(server.mutations)
    assert run_plan(plan, client=server)["status"] == "MANUAL_ACTION_REQUIRED"
    assert server.mutations == writes


def test_first_bind_materializes_only_queryable_and_opaque_defaults(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()

    def check_pre_bind(action, name):
        if action == "BIND_STYLE":
            assert "queryable" not in server.layers[("maps", name)]
            assert "opaque" not in server.layers[("maps", name)]

    server.before_mutation = check_pre_bind
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    with Journal(journal_path(plan)) as audit:
        layer = next(obj for obj in audit.objects(plan["task_id"]) if obj["kind"] == "layer")
        assert layer["details"]["snapshot"]["queryable"] is True
        assert layer["details"]["snapshot"]["opaque"] is False
    writes = list(server.mutations)
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    assert server.mutations == writes


def test_first_bind_does_not_accept_queryable_false_as_a_materialized_default(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    server.after_bind = lambda layer: layer.update(queryable=False)
    assert run_plan(plan, client=server)["status"] == "MANUAL_ACTION_REQUIRED"
    assert server.map_calls == 0


def test_successful_cleanup_inventory_is_explicitly_journal_only(tmp_path):
    plan, server = make_plan(tmp_path, count=2), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    result = rollback_plan(plan, client=server)
    inventory = result["recovery_inventory"]
    assert inventory["evidence_source"] == "LOCAL_JOURNAL_ONLY"
    assert inventory["requires_live_reconciliation"] is False
    assert len(inventory["objects"]) == 8
    assert {entry["state"] for entry in inventory["objects"]} == {
        "CONFIRMED_ABSENT_AT_LAST_CHECK"
    }
    assert inventory["unresolved_operations"] == []
    saved = json.loads(
        (Path(plan["output_dir"]) / "cleanup-report.json").read_text(encoding="utf-8")
    )
    assert saved["recovery_inventory"] == inventory


def test_interrupted_cleanup_reports_each_pending_and_unknown_object(tmp_path):
    plan, server = make_plan(tmp_path, count=2), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    # Resource 2 is first in cleanup order: its layer is deleted, but the
    # resource DELETE succeeds remotely and then loses the HTTP response.
    server.fail_once("DELETE_RESOURCE", after=True, unknown=True)
    with pytest.raises(GeoServerError):
        rollback_plan(plan, client=server)

    report = json.loads(
        (Path(plan["output_dir"]) / "cleanup-report.json").read_text(encoding="utf-8")
    )
    inventory = report["recovery_inventory"]
    assert report["status"] == "MANUAL_ACTION_REQUIRED"
    assert report["server_data_retained"] is True
    assert inventory["requires_live_reconciliation"] is True
    assert inventory["evidence_source"] == "LOCAL_JOURNAL_ONLY"
    by_key = {
        (item["resource_id"], item["kind"]): item["state"]
        for item in inventory["objects"]
    }
    assert by_key[("r0002", "layer")] == "CONFIRMED_ABSENT_AT_LAST_CHECK"
    assert by_key[("r0002", "resource")] == "DELETE_OUTCOME_UNKNOWN"
    assert by_key[("r0002", "store")] == "NOT_CONFIRMED_REMOVED"
    assert by_key[("r0002", "style")] == "NOT_CONFIRMED_REMOVED"
    assert {by_key[("r0001", kind)] for kind in ("layer", "resource", "store", "style")} == {
        "NOT_CONFIRMED_REMOVED"
    }
    assert inventory["unresolved_operations"] == [
        {"resource_id": "r0002", "action": "DELETE_RESOURCE", "outcome": "UNKNOWN"}
    ]
    mutations = list(server.mutations)
    with pytest.raises(RecoveryRequired):
        rollback_plan(plan, client=server)
    assert server.mutations == mutations
    assert "credentials" not in json.dumps(report).lower()


def test_cleanup_preflight_rejection_reports_uncleared_objects_without_deleting(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    assert run_plan(plan, client=server)["status"] == "SUCCEEDED"
    resource = plan["resources"][0]
    server.style_bodies[("maps", resource["style_name"])] += b"<!-- changed by actor -->"
    existing_mutations = list(server.mutations)
    with pytest.raises(RecoveryRequired):
        rollback_plan(plan, client=server)
    report = json.loads(
        (Path(plan["output_dir"]) / "cleanup-report.json").read_text(encoding="utf-8")
    )
    assert len(report["recovery_inventory"]["objects"]) == 4
    assert {x["state"] for x in report["recovery_inventory"]["objects"]} == {
        "NOT_CONFIRMED_REMOVED"
    }
    assert report["recovery_inventory"]["requires_live_reconciliation"] is True
    assert server.mutations == existing_mutations

    with Journal(journal_path(plan)) as audit:
        assert audit.get_task(plan["task_id"])["status"] == "MANUAL_ACTION_REQUIRED"


def _synthetic_empty_map(width: int, height: int) -> MapResponse:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return MapResponse(200, {"content-type": "image/png"}, stream.getvalue())


def test_wms_full_extent_success_does_not_issue_extra_requests(tmp_path):
    plan, server = make_plan(tmp_path), StatefulGeoServer()
    report = run_plan(plan, client=server)
    assert report["status"] == "SUCCEEDED"
    wms = report["resources"][0]["wms"]
    assert server.map_calls == 1
    assert wms["selected_sample"] == 0
    assert len(wms["samples"]) == 1
    assert wms["samples"][0]["bbox"] == wms["bbox"]


def test_wms_empty_full_extent_can_use_a_visible_zoomed_window(tmp_path):
    class SparseAtFullExtent(StatefulGeoServer):
        def get_map(self, workspace, layer, *, bbox, crs, version, width, height):
            if self.map_calls == 0:
                self.map_calls += 1
                return _synthetic_empty_map(width, height)
            return super().get_map(
                workspace, layer, bbox=bbox, crs=crs, version=version, width=width, height=height
            )

    plan, server = make_plan(tmp_path), SparseAtFullExtent()
    report = run_plan(plan, client=server)
    assert report["status"] == "SUCCEEDED"
    result = report["resources"][0]
    wms = result["wms"]
    assert server.map_calls == 2
    assert wms["selected_sample"] == 1
    assert len(wms["samples"]) == 2
    assert wms["samples"][0]["evidence"]["status"] == "REVIEW_REQUIRED"
    assert wms["samples"][1]["evidence"]["status"] == "PASSED"
    assert wms["bbox"] == wms["samples"][1]["bbox"]
    assert wms["samples"][0]["bbox"] != wms["samples"][1]["bbox"]
    assert digest_file(result["preview_path"]) == wms["evidence"]["sha256"]
    assert wms["crs"] == "EPSG:4326" and wms["version"] == "1.1.1"


def test_wms_all_empty_windows_stay_review_required(tmp_path):
    class AllTransparent(StatefulGeoServer):
        def get_map(self, workspace, layer, *, bbox, crs, version, width, height):
            self.map_calls += 1
            return _synthetic_empty_map(width, height)

    plan, server = make_plan(tmp_path), AllTransparent()
    report = run_plan(plan, client=server)
    assert report["status"] == "REVIEW_REQUIRED"
    wms = report["resources"][0]["wms"]
    assert server.map_calls == 5
    assert wms["selected_sample"] == 0
    assert len(wms["samples"]) == 5
    assert all(s["evidence"]["status"] == "REVIEW_REQUIRED" for s in wms["samples"])
    assert all(s["evidence"]["reasons"] == ["FULLY_TRANSPARENT"] for s in wms["samples"])
    assert wms["bbox"] == wms["samples"][0]["bbox"]


def test_wms_secondary_error_does_not_override_primary_review_with_false_success(tmp_path):
    class ErrorOnZoom(StatefulGeoServer):
        def get_map(self, workspace, layer, *, bbox, crs, version, width, height):
            self.map_calls += 1
            if self.map_calls == 1:
                return _synthetic_empty_map(width, height)
            raise GeoServerError("INJECTED_WMS_READ", "Synthetic secondary read error.")

    plan, server = make_plan(tmp_path), ErrorOnZoom()
    report = run_plan(plan, client=server)
    assert report["status"] == "REVIEW_REQUIRED"
    wms = report["resources"][0]["wms"]
    assert server.map_calls == 2
    assert wms["selected_sample"] == 0
    assert len(wms["samples"]) == 2
    assert wms["samples"][1]["status"] == "REVIEW_REQUIRED"
    assert wms["samples"][1]["code"] == "INJECTED_WMS_READ"
    assert "Synthetic secondary read error" not in json.dumps(report)


def test_wms_zoom_candidate_boxes_are_bounded_and_deterministic():
    from geopublisher.engine import _wms_review_bboxes

    actual = [100.0, 20.0, 101.0, 21.0]
    boxes = _wms_review_bboxes(actual)
    assert len(boxes) == 4
    assert boxes == _wms_review_bboxes(actual)
    assert len({tuple(bbox) for bbox in boxes}) == 4
    assert all(99 < x0 < x1 < 102 and 19 < y0 < y1 < 22 for x0, y0, x1, y1 in boxes)
    assert all((x1 - x0) < 0.6 and (y1 - y0) < 0.6 for x0, y0, x1, y1 in boxes)


def test_wms_xml_primary_failure_never_falls_back_to_zoomed_windows(tmp_path):
    class XmlFailure(StatefulGeoServer):
        def get_map(self, workspace, layer, *, bbox, crs, version, width, height):
            self.map_calls += 1
            return MapResponse(
                200,
                {"content-type": "image/png"},
                b"<ServiceExceptionReport>wrong layer</ServiceExceptionReport>",
            )

    plan, server = make_plan(tmp_path), XmlFailure()
    report = run_plan(plan, client=server)
    assert report["status"] == "FAILED"
    assert report["resources"][0]["code"] == "WMS_EXCEPTION"
    assert server.map_calls == 1


def test_wms_legitimate_uniform_color_requires_review_even_after_zoom(tmp_path):
    class SolidRaster(StatefulGeoServer):
        def get_map(self, workspace, layer, *, bbox, crs, version, width, height):
            self.map_calls += 1
            image = Image.new("RGB", (width, height), (14, 98, 190))
            output = io.BytesIO()
            image.save(output, "PNG")
            return MapResponse(200, {"content-type": "image/png"}, output.getvalue())

    plan, server = make_plan(tmp_path, kinds=["geotiff"]), SolidRaster()
    report = run_plan(plan, client=server)
    assert report["status"] == "REVIEW_REQUIRED"
    assert server.map_calls == 5
    assert all(
        sample["evidence"]["reasons"] == ["UNIFORM_IMAGE"]
        for sample in report["resources"][0]["wms"]["samples"]
    )
