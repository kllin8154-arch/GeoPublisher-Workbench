"""Offline tests for 206 PostgreSQL catalog read-only review; no real connections."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import psycopg
import pytest

from geopublisher.catalog_live import (
    DatabaseReviewError,
    assess_catalog_snapshot,
    probe_catalog,
)
from geopublisher.server_deploy import LinuxTarget


def sample_mapping(tmp_path: Path) -> Path:
    data = {
        "schema_version": 1, "target_schema": "demo_catalog",
        "schema_verified": False,
        "tables": {"node": "demo_nodes", "service": "demo_services"},
        "columns": {
            "node": {
                "sys": "sys", "key": "code", "title": "namec",
                "parent": "glcode", "kind": "type", "display_order": "disporder",
                "visible": "visible", "zoom_start": "zooms", "zoom_end": "zoome",
                "service_reference": "svr_name", "layer_index": "lay_idx",
            },
            "service": {
                "key": "code", "title": "namec", "kind": "type",
                "platform": "platform", "url": "url", "layer": "layer",
                "crs": "crs", "format": "format",
            },
        },
        "defaults": {
            "sys": "0", "wms_url": "geoserver/demo/wms", "workspace": "demo",
            "platform": "geoserver", "organization": 0,
        },
        "existing_parents": [{"code": "basemap", "title": "Basemap"}],
        "groups": [{"code": "geo_50k", "title": "Geology at 1:50k",
                    "parent": "basemap", "display_order": 10, "layer_index": 2}],
        "layers": [{
            "code": "sheet_n1", "title": "Synthetic sheet", "parent": "geo_50k",
            "service_key": "sample_service", "qualified_name": "demo:sample_sheet",
            "source_format": "shp", "publish_kind": "shapefile", "crs": "EPSG:4490",
            "service_mode": "create", "display_order": 10, "layer_index": 3,
            "zoom_start": 1, "zoom_end": 20,
            "wms": {"status": "PENDING", "visual_reviewed": False},
        }],
    }
    file = tmp_path / "business_tree_draft.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    return file


def db_snapshot(mapping: dict) -> dict:
    columns = []
    for role, table in mapping["tables"].items():
        values = set(mapping["columns"][role].values()) | {"id"}
        numeric_keys = (
            {"display_order", "visible", "zoom_start", "zoom_end", "layer_index", "organization"}
            if role == "node" else {"organization"}
        )
        numeric_cols = {
            col for key, col in mapping["columns"][role].items() if key in numeric_keys
        }
        for name in sorted(values):
            data_type = "integer" if name == "id" or name in numeric_cols else "character varying"
            columns.append((
                table, name, data_type,
                "NO" if name == "id" else "YES",
                "nextval('example_id_seq'::regclass)" if name == "id" else None,
                "NO", 100 if data_type == "character varying" else None,
            ))
    return {
        "identity": {
            "database": "demo_db", "role": "readonly", "schema": "demo_catalog",
            "read_only": "on", "server_version_num": "160010",
        },
        "relations": [("demo_nodes", "r"), ("demo_services", "r")],
        "columns": columns,
        "serial_sequences": {
            "node": '"demo_catalog".demo_nodes_id_seq',
            "service": '"demo_catalog".demo_services_id_seq',
        },
        "nodes": [("basemap", "Basemap", None, "0")],
        "services": [], "truncated": False,
    }


class Result:
    def __init__(self, data=None):
        self.data = data

    def fetchone(self):
        return self.data[0] if self.data else None

    def fetchall(self):
        return self.data or []


class FakeDatabase:
    def __init__(self, mapping):
        self.mapping = mapping
        self.snapshot = db_snapshot(mapping)
        self.commands = []
        self.is_closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.is_closed = True

    def execute(self, command, params=None):
        text = command.as_string(None) if hasattr(command, "as_string") else command
        self.commands.append((text, params))
        normalized = text.lower()
        if normalized.startswith(("begin", "set local", "rollback")):
            return Result()
        if "current_database()" in normalized:
            snap = self.snapshot["identity"]
            return Result([(snap["database"], snap["role"], snap["schema"],
                            snap["read_only"], snap["server_version_num"])])
        if "from pg_catalog.pg_class" in normalized:
            return Result(self.snapshot["relations"])
        if "from information_schema.columns" in normalized:
            return Result(self.snapshot["columns"])
        if "pg_get_serial_sequence" in normalized:
            assert params and params[0].startswith('"demo_catalog"')
            role = "node" if "demo_nodes" in params[0] else "service"
            return Result([(self.snapshot["serial_sequences"][role],)])
        if 'from "demo_nodes"' in normalized:
            assert params is not None
            assert "basemap" in params[0]
            return Result(self.snapshot["nodes"])
        if 'from "demo_services"' in normalized:
            assert params is not None
            return Result(self.snapshot["services"])
        raise AssertionError(f"Unexpected SQL: {text}")


class FakeTunnel:
    port = 15432

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        pass


def _run(tmp_path, monkeypatch, *, db=None, profile=None):
    import geopublisher.catalog_live as module

    file = profile or sample_mapping(tmp_path)
    parsed = json.loads(file.read_text("utf-8"))
    mock = db or FakeDatabase(parsed)
    connection_options = []

    def connect(**kwargs):
        connection_options.append(kwargs)
        return mock

    monkeypatch.setattr(module, "open_service", lambda session, **kw: FakeTunnel())
    target = LinuxTarget(mode="local", root="/srv/geopublisher/example")
    report = probe_catalog(
        target, object(), file, database="demo_db", username="readonly",
        password="ephemeral-secret", output=tmp_path / "new-db-review",
        connect=connect,
    )
    return report, mock, connection_options


def test_readonly_probe_generates_evidence_but_never_approves_or_changes_database(tmp_path, monkeypatch):
    source = sample_mapping(tmp_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    report, db, opts = _run(tmp_path, monkeypatch, profile=source)
    assert report["status"] == "READ_ONLY_EVIDENCE_READY_NOT_APPROVED"
    assert report["schema_verified_remains_manual"]
    assert report["database_modified"] is False
    assert db.is_closed
    assert opts[0]["host"] == "127.0.0.1"
    assert opts[0]["port"] == 15432
    assert opts[0]["dbname"] == "demo_db"
    assert opts[0]["autocommit"] is True
    assert "default_transaction_read_only=on" in opts[0]["options"]
    ops = [sql_text.upper() for sql_text, _ in db.commands]
    assert ops[0] == "BEGIN READ ONLY"
    assert ops[-1] == "ROLLBACK"
    assert all(not stmt.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP"))
               for stmt in ops)
    review = Path(report["review"]).read_text("utf-8")
    parsed = json.loads(review)
    assert parsed["review_file_sha256"] == digest
    assert parsed["schema_verified"] is False
    assert parsed["sql_executed"] is False
    assert "ephemeral-secret" not in review
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("mutation, expected", [
    (lambda s: s["nodes"].clear(), "basemap"),
    (lambda s: s["nodes"].append(("sheet_n1", "Old", "geo_50k", "0")), "sheet_n1"),
    (lambda s: s["services"].append(("sample_service", "demo:sample_sheet",
                                    "geoserver/demo/wms", "EPSG:4490", "image/png")), "sample_service"),
    (lambda s: s["serial_sequences"].update(node=None), "id"),
    (lambda s: s["relations"].clear(), "demo_nodes"),
    (lambda s: s.update(truncated=True), "安全上限"),
])
def test_schema_node_and_service_conflicts_are_blocking(tmp_path, mutation, expected):
    path = sample_mapping(tmp_path)
    profile = json.loads(path.read_text("utf-8"))
    snap = db_snapshot(profile)
    mutation(snap)
    outcome = assess_catalog_snapshot(profile, snap)
    assert outcome["status"] == "BLOCKED_PENDING_HUMAN_REVIEW"
    assert expected in json.dumps(outcome, ensure_ascii=False)
    assert outcome["database_modified"] is False


def test_existing_service_reuse_requires_exact_live_layer_url_and_crs(tmp_path):
    path = sample_mapping(tmp_path)
    profile = json.loads(path.read_text("utf-8"))
    profile["layers"][0]["service_mode"] = "reuse"
    snap = db_snapshot(profile)
    snap["services"].append((
        "sample_service", "demo:sample_sheet", "geoserver/demo/wms", "EPSG:4490", "image/png",
    ))
    result = assess_catalog_snapshot(profile, snap)
    assert result["status"] == "READ_ONLY_EVIDENCE_READY_NOT_APPROVED"
    snap["services"][0] = (
        "sample_service", "demo:another_sheet", "geoserver/demo/wms", "EPSG:4490", "image/png",
    )
    result = assess_catalog_snapshot(profile, snap)
    assert result["status"] == "BLOCKED_PENDING_HUMAN_REVIEW"


def test_database_identity_mismatch_closes_readonly_transaction_without_report(tmp_path, monkeypatch):
    import geopublisher.catalog_live as module

    file = sample_mapping(tmp_path)
    fake = FakeDatabase(json.loads(file.read_text("utf-8")))
    fake.snapshot["identity"]["schema"] = "wrong_schema"
    monkeypatch.setattr(module, "open_service", lambda *a, **kw: FakeTunnel())
    with pytest.raises(DatabaseReviewError) as error:
        probe_catalog(
            LinuxTarget(mode="local", root="/srv/geopublisher/example"), object(),
            file, database="demo_db", username="readonly",
            password="secret", output=tmp_path / "should-not-exist",
            connect=lambda **kwargs: fake,
        )
    assert error.value.code == "DATABASE_IDENTITY_MISMATCH"
    assert fake.commands[-1][0].upper() == "ROLLBACK"
    assert not (tmp_path / "should-not-exist").exists()


def test_unknown_or_dangerous_database_identifiers_are_rejected_before_connection(tmp_path, monkeypatch):
    import geopublisher.catalog_live as module

    file = sample_mapping(tmp_path)
    profile = json.loads(file.read_text("utf-8"))
    profile["tables"]["node"] = "demo_nodes;DROP_TABLE"
    file.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(module, "open_service",
                        lambda *_args, **_kw: pytest.fail("Must reject before tunnel"))
    with pytest.raises(Exception):
        probe_catalog(
            LinuxTarget(mode="local", root="/srv/geopublisher/example"), object(),
            file, database="demo_db", username="readonly",
            password="secret", output=tmp_path / "no-output",
        )
    assert not (tmp_path / "no-output").exists()


def test_existing_output_folder_blocks_before_any_connection(tmp_path, monkeypatch):
    import geopublisher.catalog_live as module

    file = sample_mapping(tmp_path)
    output = tmp_path / "exists"
    output.mkdir()
    monkeypatch.setattr(module, "open_service",
                        lambda *_args, **_kw: pytest.fail("Cannot contact database"))
    with pytest.raises(DatabaseReviewError) as error:
        probe_catalog(
            LinuxTarget(mode="local", root="/srv/geopublisher/example"), object(),
            file, database="demo_db", username="readonly",
            password="secret", output=output,
        )
    assert error.value.code == "UNSAFE_REPORT_OUTPUT"


def test_psycopg_error_is_sanitized_and_password_never_written(tmp_path, monkeypatch):
    import geopublisher.catalog_live as module

    file = sample_mapping(tmp_path)
    monkeypatch.setattr(module, "open_service", lambda *_args, **_kw: FakeTunnel())

    def fail(**kwargs):
        raise psycopg.OperationalError("sensitive-database-error-details")

    with pytest.raises(DatabaseReviewError) as error:
        probe_catalog(
            LinuxTarget(mode="local", root="/srv/geopublisher/example"), object(),
            file, database="demo_db", username="readonly",
            password="ephemeral-secret", output=tmp_path / "no-db-data",
            connect=fail,
        )
    assert error.value.code == "DATABASE_READONLY_PROBE_FAILED"
    assert "sensitive-database-error-details" not in str(error.value)
    assert not (tmp_path / "no-db-data").exists()



@pytest.mark.parametrize("field, mutation, warning", [
    ("disporder", "character varying", "数值类型"),
    ("code", "integer", "文本类型"),
])
def test_incompatible_postgres_field_types_keep_sql_blocked(tmp_path, field, mutation, warning):
    profile = json.loads(sample_mapping(tmp_path).read_text("utf-8"))
    snapshot = db_snapshot(profile)
    data = []
    for record in snapshot["columns"]:
        row = list(record)
        if row[0] == "demo_nodes" and row[1] == field:
            row[2] = mutation
        data.append(tuple(row))
    snapshot["columns"] = data
    assessed = assess_catalog_snapshot(profile, snapshot)
    assert assessed["status"] == "BLOCKED_PENDING_HUMAN_REVIEW"
    assert warning in " ".join(assessed["blocking_reasons"])


@pytest.mark.parametrize("field, warning", [
    ("type", "节点类型字段非空"),
    ("glcode", "父目录字段非空"),
])
def test_unwritable_null_fields_for_root_groups_are_blocked(tmp_path, field, warning):
    profile = json.loads(sample_mapping(tmp_path).read_text("utf-8"))
    snapshot = db_snapshot(profile)
    # New root group requires parent=NULL; node.kind is NULL for every group.
    if field == "glcode":
        profile["groups"][0]["parent"] = None
    snapshot["columns"] = [
        (*row[:3], "NO", *row[4:]) if row[0] == "demo_nodes" and row[1] == field else row
        for row in snapshot["columns"]
    ]
    result = assess_catalog_snapshot(profile, snapshot)
    assert result["status"] == "BLOCKED_PENDING_HUMAN_REVIEW"
    assert warning in " ".join(result["blocking_reasons"])
