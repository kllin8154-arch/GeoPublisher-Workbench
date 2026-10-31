"""Safety properties of persisted publishing intent, ownership and local locks."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from geopublisher.journal import APPLICATION_ID, Journal, JournalError, LockConflict


def child_environment() -> dict[str, str]:
    env = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_process_crash_preserves_write_intent_for_reconciliation(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    script = """
import os,sys
from geopublisher.journal import Journal
j=Journal(sys.argv[1])
j.create_task('publish', {'version': 1, 'targets': ['layer__fixed']})
j.set_task_status('publish', 'RUNNING')
j.set_resource('publish','r1', status='RUNNING',stage='UPLOADING')
j.begin_operation('publish','r1','UPLOAD',{'store':'layer__fixed'},{'absent':True})
j.acquire_lock('publish','local:workspace:layer__fixed')
os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], env=child_environment(), timeout=15)
    assert result.returncode == 17
    with Journal(path) as journal:
        assert journal.get_task("publish")["status"] == "RUNNING"
        operation = journal.operations("publish")[0]
        assert operation["outcome"] == "INTENT"
        assert operation["evidence"] is None
        assert operation["target"]["store"] == "layer__fixed"
        assert journal.get_resources("publish")[0]["stage"] == "UPLOADING"
        # Nothing guesses that an unacknowledged remote upload failed.
        journal.finish_operation(
            operation["operation_id"], outcome="UNKNOWN", evidence={"reason": "response_lost"}
        )
        assert journal.recover_locks("publish") == 1
        journal.acquire_lock("publish", "local:workspace:layer__fixed")
        journal.release_locks("publish")


def test_unknown_can_be_reconciled_but_final_evidence_is_immutable(tmp_path: Path) -> None:
    with Journal(tmp_path / "jobs.sqlite") as journal:
        journal.create_task("t", {"version": 1})
        operation = journal.begin_operation("t", "r", "CREATE", {"layer": "l"})
        journal.finish_operation(operation, outcome="UNKNOWN", evidence={"timeout": True})
        journal.finish_operation(operation, outcome="SUCCEEDED", evidence={"fingerprint": "verified"})
        journal.finish_operation(operation, outcome="SUCCEEDED", evidence={"fingerprint": "verified"})
        with pytest.raises(JournalError) as failure:
            journal.finish_operation(operation, outcome="FAILED", evidence={"error": "different"})
        assert failure.value.code == "OPERATION_FINALIZED"
        assert journal.operations("t")[0]["outcome"] == "SUCCEEDED"
        assert journal.operations("t")[0]["evidence"] == {"fingerprint": "verified"}


def test_active_owner_cannot_be_displaced_by_same_task_or_other_task(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    with Journal(path) as first, Journal(path) as second:
        first.create_task("a", {})
        first.create_task("b", {})
        first.acquire_lock("a", "server:workspace:layer")
        first.acquire_lock("a", "server:workspace:layer")
        for contender in ("a", "b"):
            with pytest.raises(LockConflict) as failure:
                second.acquire_lock(contender, "server:workspace:layer")
            assert failure.value.owner_task_id == "a"
        second.release_locks("a")
        with pytest.raises(JournalError) as failure:
            second.recover_locks("a")
        assert failure.value.code == "LOCK_OWNER_ACTIVE"
        first.release_locks("a")
        second.acquire_lock("b", "server:workspace:layer")
        second.release_locks("b")


def test_locks_are_atomic_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    with Journal(path) as journal:
        journal.create_task("parent", {})
        journal.create_task("child", {})
    script = """
import sys
from geopublisher.journal import Journal
with Journal(sys.argv[1]) as j:
 j.acquire_lock('child','shared-resource')
 print('locked', flush=True)
 sys.stdin.readline()
 j.release_locks('child')
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        env=child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"
        with Journal(path) as journal:
            with pytest.raises(LockConflict):
                journal.acquire_lock("parent", "shared-resource")
        _, error = proc.communicate("release\n", timeout=10)
        assert proc.returncode == 0, error
        with Journal(path) as journal:
            journal.acquire_lock("parent", "shared-resource")
            journal.release_locks("parent")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


def test_lock_recovery_refuses_another_host_and_is_all_or_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "jobs.sqlite"
    with Journal(path) as first:
        first.create_task("t", {})
        first.acquire_lock("t", "a")
        first.acquire_lock("t", "b")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE task_locks SET hostname='different-host' WHERE lock_key='b'")
    monkeypatch.setattr("geopublisher.journal._pid_alive", lambda _: False)
    with Journal(path) as second:
        with pytest.raises(JournalError) as failure:
            second.recover_locks("t")
        assert failure.value.code == "LOCK_RECOVERY_UNSAFE"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM task_locks").fetchone()[0] == 2


def test_original_ownership_snapshot_survives_guarded_refresh(tmp_path: Path) -> None:
    with Journal(tmp_path / "jobs.sqlite") as journal:
        journal.create_task("t", {})
        journal.record_object("t", "r", "layer", "roads", fingerprint="before", details={"style": "default"})
        journal.refresh_object(
            "t",
            "r",
            "layer",
            "roads",
            expected_fingerprint="before",
            fingerprint="after",
            details={"style": "own-style"},
        )
        state = journal.objects("t", "r")[0]
        assert state["initial_fingerprint"] == "before"
        assert state["initial_details"] == {"style": "default"}
        assert state["fingerprint"] == "after"
        assert state["details"] == {"style": "own-style"}
        with pytest.raises(JournalError) as failure:
            journal.refresh_object(
                "t", "r", "layer", "roads", expected_fingerprint="before", fingerprint="stale"
            )
        assert failure.value.code == "OBJECT_CHANGED"
        with pytest.raises(JournalError) as failure:
            journal.record_object("t", "r", "layer", "roads", fingerprint="hijacked")
        assert failure.value.code == "OBJECT_CONFLICT"
        assert journal.objects("t")[0]["fingerprint"] == "after"


def test_plan_replay_is_idempotent_and_does_not_reset_status(tmp_path: Path) -> None:
    with Journal(tmp_path / "jobs.sqlite") as journal:
        journal.create_task("t", {"target": "fixed", "version": 1})
        journal.set_task_status("t", "SUCCEEDED")
        journal.create_task("t", {"version": 1, "target": "fixed"})
        assert journal.get_task("t")["status"] == "SUCCEEDED"
        with pytest.raises(JournalError) as failure:
            journal.create_task("t", {"target": "new-on-every-retry", "version": 1})
        assert failure.value.code == "TASK_CONFLICT"
        assert journal.get_task("t")["plan"]["target"] == "fixed"


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "never-persist-this-value"},
        {"items": [{"apiKey": "never-persist-this-value"}]},
        {"headers": {"Authorization": "never-persist-this-value"}},
        {"headers": {"Set-Cookie": "never-persist-this-value"}},
        {"url": "https://user:never-persist-this-value@example.invalid/geoserver"},
        {"error": "Authorization: Basic never-persist-this-value"},
        {"database": "host=example.invalid password=never-persist-this-value"},
    ],
)
def test_sensitive_values_are_rejected_before_persistence(tmp_path: Path, payload: dict) -> None:
    path = tmp_path / "jobs.sqlite"
    with Journal(path) as journal:
        with pytest.raises(JournalError) as failure:
            journal.create_task("bad", payload)
        assert failure.value.code == "SENSITIVE_DATA"
        assert "never-persist" not in str(failure.value)
        assert journal.list_tasks() == []
        journal.create_task(
            "good", {"password_env": "GEOPUB_PASSWORD", "credentialRef": "keyring://app/user"}
        )
        assert len(journal.list_tasks()) == 1
        for candidate in tmp_path.iterdir():
            if candidate.is_file():
                assert b"never-persist-this-value" not in candidate.read_bytes()


def test_event_message_and_evidence_cannot_bypass_secret_checks(tmp_path: Path) -> None:
    with Journal(tmp_path / "jobs.sqlite") as journal:
        journal.create_task("t", {})
        op = journal.begin_operation("t", "r", "UPLOAD", {"name": "test"})
        with pytest.raises(JournalError):
            journal.finish_operation(op, outcome="FAILED", evidence={"password": "sensitive"})
        assert journal.operations("t")[0]["outcome"] == "INTENT"
        with pytest.raises(JournalError):
            journal.add_event("t", "ERROR", "HTTP", "Authorization: Basic sensitive")
        assert journal.events("t") == []


def test_resource_status_preserves_details_and_all_records_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "任务" / "jobs.sqlite"
    with Journal(path) as journal:
        journal.create_task("t", {"input": "测试目录/地图.shp"})
        journal.set_resource("t", "r", status="RUNNING", stage="UPLOAD", details={"progress": 42})
        journal.set_resource("t", "r", status="MANUAL_ACTION_REQUIRED", stage="RECONCILE")
        journal.add_event(
            "t", "WARN", "RESPONSE_LOST", "需要核对服务端状态", resource_id="r", details={"attempt": 1}
        )
    with Journal(path) as journal:
        assert journal.get_resources("t")[0]["details"] == {"progress": 42}
        assert journal.events("t")[0]["message"] == "需要核对服务端状态"
        assert journal.list_tasks()[0]["plan"]["input"] == "测试目录/地图.shp"


def test_sql_text_is_stored_as_data_not_executed(tmp_path: Path) -> None:
    hostile_id = "task'); DROP TABLE tasks; --"
    with Journal(tmp_path / "jobs.sqlite") as journal:
        journal.create_task(hostile_id, {"target": "x' OR '1'='1"})
        journal.set_task_status(hostile_id, "READY")
        assert journal.get_task(hostile_id)["status"] == "READY"
        assert len(journal.list_tasks()) == 1


def test_foreign_database_and_future_version_are_not_modified(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.sqlite"
    with sqlite3.connect(foreign) as db:
        db.execute("CREATE TABLE important(value TEXT)")
        db.execute("INSERT INTO important VALUES('keep me')")
    with pytest.raises(JournalError) as failure:
        Journal(foreign)
    assert failure.value.code == "JOURNAL_FORMAT_INVALID"
    with sqlite3.connect(foreign) as db:
        assert db.execute("SELECT value FROM important").fetchone()[0] == "keep me"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    future = tmp_path / "future.sqlite"
    with sqlite3.connect(future) as db:
        db.execute("PRAGMA user_version=99")
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
    with pytest.raises(JournalError) as failure:
        Journal(future)
    assert failure.value.code == "SCHEMA_TOO_NEW"
    with sqlite3.connect(future) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_missing_parent_task_does_not_leave_partial_records(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    with Journal(path) as journal:
        with pytest.raises(JournalError) as failure:
            journal.begin_operation("missing", "r", "CREATE", {"name": "test"})
        assert failure.value.code == "TASK_NOT_FOUND"
        assert journal.operations("missing") == []
        journal.create_task("valid", {})
        assert journal.get_task("valid")["status"] == "DRAFT"


def test_invalid_json_never_reaches_the_journal(tmp_path: Path) -> None:
    with Journal(tmp_path / "jobs.sqlite") as journal:
        for payload in ({"number": float("nan")}, {"object": object()}, {1: "not a string key"}):
            with pytest.raises(JournalError) as failure:
                journal.create_task("t", payload)
            assert failure.value.code == "INVALID_JSON"
        assert journal.list_tasks() == []


def test_closed_connection_fails_with_a_stable_error(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "jobs.sqlite")
    journal.close()
    journal.close()
    with pytest.raises(JournalError) as failure:
        journal.list_tasks()
    assert failure.value.code == "JOURNAL_CLOSED"
