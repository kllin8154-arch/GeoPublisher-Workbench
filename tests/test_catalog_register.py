"""Synthetic-only safety tests for opt-in business catalog database registration."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from PIL import Image
from test_catalog_live import db_snapshot, sample_mapping

from geopublisher import catalog_register
from geopublisher.catalog_live import DatabaseReviewError
from geopublisher.catalog_register import register_catalog, registration_phrase
from geopublisher.cli import parser
from geopublisher.server_deploy import LinuxTarget


def approved_review(tmp_path: Path):
    source = sample_mapping(tmp_path)
    image = tmp_path / "synthetic-wms.png"
    Image.frombytes("RGB", (128, 128), os.urandom(128 * 128 * 3)).save(image)
    profile = json.loads(source.read_text("utf-8"))
    profile["schema_verified"] = True
    profile["groups"][0]["classification_reviewed"] = True
    profile["layers"][0]["classification_reviewed"] = True
    profile["layers"][0]["wms"] = {
        "status": "PASSED", "visual_reviewed": True, "png": str(image),
    }
    source.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    target = LinuxTarget(mode="local", root="/srv/geopublisher/synthetic", postgres_port=5432)
    evidence = tmp_path / "business_db_readonly_review.json"
    evidence.write_text(json.dumps({
        "status": "READ_ONLY_EVIDENCE_READY_NOT_APPROVED",
        "blocking_reasons": [], "sql_executed": False,
        "database_modified": False, "review_required": True,
        "review_file_sha256": digest, "source_review_file": str(source),
        "target": target.safe_info(),
        "database_identity": {
            "database": "demo_db", "schema": "demo_catalog", "read_only": "on",
        },
    }, ensure_ascii=False), encoding="utf-8")
    return target, source, evidence, digest, profile


class FakeConnection:
    def __init__(self, *, fail_write=False):
        self.commands = []
        self.fail_write = fail_write
        self.nextset_calls = 0

    def nextset(self):
        self.nextset_calls += 1
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.commands.append((str(query), params))
        if self.fail_write and isinstance(query, str) and "LOCK TABLE" in query:
            raise psycopg.OperationalError("Synthetic connection loss")
        return self


def _invoke(tmp_path, monkeypatch, *, fail_write=False, post_verify=None):
    target, source, evidence, digest, profile = approved_review(tmp_path)
    monkeypatch.setattr(catalog_register, "open_service",
                        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(port=5432)))
    monkeypatch.setattr(catalog_register, "_read_snapshot",
                        lambda _conn, _profile, *, database: db_snapshot(profile))
    monkeypatch.setattr(catalog_register, "_post_verify",
                        post_verify if post_verify else lambda _conn, _profile: [])
    seen = []
    connection = FakeConnection(fail_write=fail_write)

    def connect(**kwargs):
        seen.append(kwargs)
        return connection

    kwargs = {
        "database": "demo_db", "username": "synthetic_writer",
        "password": "fake-test-only-secret", "output": tmp_path / "register-output",
        "confirmation": registration_phrase("demo_db", digest), "connect": connect,
    }
    return (target, source, evidence, digest, profile, connection, seen, kwargs)


def test_registration_requires_exact_typed_database_and_file_digest():
    digest = "a" * 64
    assert registration_phrase("demo_db", digest) == "REGISTER demo_db aaaaaaaaaaaa"
    with pytest.raises(Exception):
        registration_phrase("demo_db; DROP TABLE x", digest)
    with pytest.raises(DatabaseReviewError):
        registration_phrase("demo_db", "invalid")


@pytest.mark.parametrize("tamper", [
    "wrong_confirmation", "changed_profile", "wrong_target", "wrong_db",
    "blocked_review", "not_verified", "missing_evidence",
])
def test_all_unreviewed_inputs_block_before_any_connection(tmp_path, monkeypatch, tamper):
    target, source, evidence, digest, profile, _connection, seen, kwargs = _invoke(
        tmp_path, monkeypatch
    )
    if tamper == "wrong_confirmation":
        kwargs["confirmation"] = "REGISTER demo_db 000000000000"
    elif tamper == "changed_profile":
        source.write_text(source.read_text("utf-8") + " ", encoding="utf-8")
    elif tamper == "wrong_target":
        stored = json.loads(evidence.read_text("utf-8"))
        stored["target"]["host"] = "192.0.2.10"
        evidence.write_text(json.dumps(stored), encoding="utf-8")
    elif tamper == "wrong_db":
        kwargs["database"] = "other_db"
        kwargs["confirmation"] = registration_phrase("other_db", digest)
    elif tamper == "blocked_review":
        stored = json.loads(evidence.read_text("utf-8"))
        stored["status"] = "BLOCKED_PENDING_HUMAN_REVIEW"
        evidence.write_text(json.dumps(stored), encoding="utf-8")
    elif tamper == "not_verified":
        profile["schema_verified"] = False
        source.write_text(json.dumps(profile), encoding="utf-8")
    else:
        evidence.unlink()
    with pytest.raises(DatabaseReviewError):
        register_catalog(target, SimpleNamespace(target=target), source, evidence, **kwargs)
    assert seen == []
    assert not kwargs["output"].exists()


def test_authorized_registration_uses_only_generated_sql_and_audits_verified_result(
    tmp_path, monkeypatch
):
    target, source, evidence, digest, profile, connection, seen, kwargs = _invoke(
        tmp_path, monkeypatch
    )
    result = register_catalog(target, SimpleNamespace(target=target), source, evidence, **kwargs)
    assert result["status"] == "VERIFIED_REGISTERED"
    assert result["groups"] == result["layers"] == 1
    assert len(seen) == 1
    assert seen[0]["host"] == "127.0.0.1" and seen[0]["port"] == 5432
    assert seen[0]["application_name"].endswith("authorized_registration")
    assert any("LOCK TABLE" in statement and "INSERT INTO" in statement
               for statement, _ in connection.commands)
    assert connection.nextset_calls == 1
    audit = (kwargs["output"] / "catalog_registration_audit.json").read_text("utf-8")
    assert json.loads(audit)["status"] == "VERIFIED_REGISTERED"
    assert json.loads(audit)["profile_sha256"] == digest
    assert "fake-test-only-secret" not in audit
    assert (kwargs["output"] / "generated_sql" / "02_register_reviewed.sql").is_file()


def test_live_preflight_collision_blocks_before_write(tmp_path, monkeypatch):
    target, source, evidence, digest, profile, connection, seen, kwargs = _invoke(
        tmp_path, monkeypatch
    )
    collision = db_snapshot(profile)
    collision["nodes"].append(("geo_50k", "Geology at 1:50k", "basemap", "0"))
    monkeypatch.setattr(catalog_register, "_read_snapshot",
                        lambda _conn, _profile, *, database: collision)
    with pytest.raises(DatabaseReviewError):
        register_catalog(target, SimpleNamespace(target=target), source, evidence, **kwargs)
    assert len(seen) == 1
    assert not connection.commands
    audit = json.loads((kwargs["output"] / "catalog_registration_audit.json").read_text("utf-8"))
    assert audit["status"] == "BLOCKED_WITHOUT_WRITE"
    assert audit["blocked"]


def test_unknown_commit_must_not_retry_and_reports_manual_reconciliation(tmp_path, monkeypatch):
    target, source, evidence, digest, profile, conn, seen, kwargs = _invoke(
        tmp_path, monkeypatch, fail_write=True
    )
    with pytest.raises(DatabaseReviewError) as err:
        register_catalog(target, SimpleNamespace(target=target), source, evidence, **kwargs)
    assert err.value.code == "CATALOG_OUTCOME_UNKNOWN"
    assert sum("LOCK TABLE" in item[0] for item in conn.commands) == 1
    audit = json.loads((kwargs["output"] / "catalog_registration_audit.json").read_text("utf-8"))
    assert audit["status"] == "MANUAL_RECONCILIATION_REQUIRED"


def test_failed_readonly_postverification_never_claims_registered(tmp_path, monkeypatch):
    target, source, evidence, digest, profile, conn, seen, kwargs = _invoke(
        tmp_path, monkeypatch,
        post_verify=lambda _conn, _profile: ["node:sheet_n1"],
    )
    with pytest.raises(DatabaseReviewError) as err:
        register_catalog(target, SimpleNamespace(target=target), source, evidence, **kwargs)
    assert err.value.code == "CATALOG_VERIFY_UNKNOWN"
    audit = json.loads((kwargs["output"] / "catalog_registration_audit.json").read_text("utf-8"))
    assert audit["status"] == "MANUAL_RECONCILIATION_REQUIRED"


def test_catalog_register_cli_does_not_support_unattended_yes():
    args = [
        "server-catalog-register", "--profile", "target.json",
        "--catalog-json", "tree.json", "--evidence", "review.json",
        "--database", "demo_db", "--database-user", "writer",
        "--output", "audit",
    ]
    assert parser().parse_args(args).command == "server-catalog-register"
    with pytest.raises(SystemExit):
        parser().parse_args(args + ["--yes"])
