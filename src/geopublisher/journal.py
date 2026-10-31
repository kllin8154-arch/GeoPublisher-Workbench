"""Durable, local task evidence and cooperative publishing locks.

Every write commits before returning. An operation left at INTENT after a
crash is deliberately *not* assumed to have failed: callers must reconcile
the remote outcome. Locks coordinate users of this journal only; they do not
lock GeoServer or other publishing programs.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
APPLICATION_ID = 0x47505742  # GPWB

_SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "clientsecret",
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "token",
        "privatekey",
        "connectionstring",
        "dsn",
    }
)
_SECRET_TEXT = re.compile(
    r"(?i)(?:\b(?:proxy-)?authorization\s*[:=]\s*\S+|"
    r"\b(?:password|passwd|pwd|client_secret|access_token|refresh_token|api_key)\s*=\s*\S+)"
)
_URL_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")
_OUTCOMES = frozenset({"SUCCEEDED", "FAILED", "UNKNOWN"})


class JournalError(RuntimeError):
    """A stable, safe journal failure; messages never include input values."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LockConflict(JournalError):
    def __init__(self, owner_task_id: str) -> None:
        super().__init__("LOCKED", "The publishing resource is locked by another journal session.")
        self.owner_task_id = owner_task_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_value(value: Any, *, depth: int = 0) -> None:
    """Reject recognizable credentials; this is not a general secret detector.

    Credential *references* such as password_env and credentialRef are allowed.
    Callers must still sanitize free text and server responses before writing.
    """
    if depth > 64:
        raise JournalError("INVALID_JSON", "Journal values exceed the supported nesting depth.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise JournalError("INVALID_JSON", "Journal object keys must be strings.")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _SECRET_KEYS:
                raise JournalError("SENSITIVE_DATA", "Credential material cannot be saved in the journal.")
            _safe_value(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _safe_value(item, depth=depth + 1)
    elif isinstance(value, str):
        if _SECRET_TEXT.search(value):
            raise JournalError("SENSITIVE_DATA", "Credential material cannot be saved in the journal.")
        for match in _URL_TEXT.finditer(value):
            try:
                parsed = urlsplit(match.group())
            except ValueError:
                raise JournalError("INVALID_JSON", "A journal URL is malformed.") from None
            if parsed.username is not None or parsed.password is not None:
                raise JournalError("SENSITIVE_DATA", "URL credentials cannot be saved in the journal.")
    elif value is None or isinstance(value, (bool, int)):
        pass
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise JournalError("INVALID_JSON", "Journal numbers must be finite.")
    else:
        raise JournalError("INVALID_JSON", "Journal values must be JSON serializable.")


def _json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise JournalError("INVALID_JSON", "A journal payload must be a JSON object.")
    _safe_value(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise JournalError("INVALID_VALUE", "A journal text field is empty or invalid.")
    _safe_value(value)
    return value


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return True  # Unknown owner: fail closed.
    if os.name == "nt":
        # os.kill(pid, 0) is not a portable process-existence probe: Windows
        # may treat a non-console signal as TerminateProcess. Use a read-only
        # process handle instead, and keep the lock on any ambiguous result.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != 87  # Only ERROR_INVALID_PARAMETER means no PID.
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


_SCHEMA = (
    """CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, status TEXT NOT NULL, plan_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE resources (
        task_id TEXT NOT NULL REFERENCES tasks(task_id), resource_id TEXT NOT NULL,
        status TEXT NOT NULL, stage TEXT NOT NULL, details_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(task_id, resource_id)
    )""",
    """CREATE TABLE operations (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL REFERENCES tasks(task_id), resource_id TEXT NOT NULL,
        action TEXT NOT NULL, target_json TEXT NOT NULL, expected_json TEXT,
        outcome TEXT NOT NULL CHECK(outcome IN ('INTENT','SUCCEEDED','FAILED','UNKNOWN')),
        evidence_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE INDEX operations_task_resource ON operations(task_id, resource_id, sequence)""",
    """CREATE TABLE owned_objects (
        task_id TEXT NOT NULL REFERENCES tasks(task_id), resource_id TEXT NOT NULL,
        kind TEXT NOT NULL, name TEXT NOT NULL, initial_fingerprint TEXT NOT NULL,
        fingerprint TEXT NOT NULL, initial_details_json TEXT NOT NULL, details_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(task_id, resource_id, kind, name)
    )""",
    """CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id), resource_id TEXT,
        level TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL,
        details_json TEXT, created_at TEXT NOT NULL
    )""",
    """CREATE INDEX events_task ON events(task_id, event_id)""",
    """CREATE TABLE task_locks (
        lock_key TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        owner_token TEXT NOT NULL, hostname TEXT NOT NULL, pid INTEGER NOT NULL,
        acquired_at TEXT NOT NULL
    )""",
)


class Journal:
    """Thread-safe connection to a local SQLite journal.

    Separate instances use separate owner tokens. Even the same task cannot
    acquire a lock held by another instance. A crashed process's locks survive
    and can only be recovered explicitly, on the same host, after its PID exits.
    The database must live on a local filesystem, not an SMB/NFS shared folder.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._owner_token = uuid.uuid4().hex
        self._hostname = platform.node()
        self._pid = os.getpid()
        self._closed = False
        try:
            self._db = sqlite3.connect(
                str(self.path), timeout=10, isolation_level=None, check_same_thread=False
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=FULL")
            self._migrate()
            mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise JournalError("UNSUPPORTED_STORAGE", "The journal requires local SQLite WAL support.")
        except JournalError:
            if hasattr(self, "_db"):
                self._db.close()
            raise
        except sqlite3.Error:
            if hasattr(self, "_db"):
                self._db.close()
            raise JournalError(
                "JOURNAL_OPEN_FAILED", "The local journal could not be opened safely."
            ) from None

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection without releasing locks or changing evidence."""
        with self._mutex:
            if not self._closed:
                self._db.close()
                self._closed = True

    def _check(self) -> None:
        if self._closed:
            raise JournalError("JOURNAL_CLOSED", "The journal is closed.")
        if os.getpid() != self._pid:
            raise JournalError(
                "PROCESS_CHANGED", "Open a new journal connection after starting a child process."
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            self._check()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                yield self._db
                self._db.commit()
            except BaseException as exc:
                if self._db.in_transaction:
                    self._db.rollback()
                if isinstance(exc, sqlite3.Error):
                    raise JournalError(
                        "JOURNAL_WRITE_FAILED", "A journal transaction could not be committed."
                    ) from None
                raise

    def _migrate(self) -> None:
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise JournalError(
                    "SCHEMA_TOO_NEW", "This journal was created by a newer application version."
                )
            if version == 0:
                tables = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if tables or app_id not in (0, APPLICATION_ID):
                    raise JournalError(
                        "JOURNAL_FORMAT_INVALID",
                        "The file is not an empty or supported GeoPublisher journal.",
                    )
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif app_id != APPLICATION_ID:
                raise JournalError(
                    "JOURNAL_FORMAT_INVALID", "The file is not a supported GeoPublisher journal."
                )

    def _fetch(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._mutex:
            self._check()
            try:
                return self._db.execute(sql, parameters).fetchall()
            except sqlite3.Error:
                raise JournalError("JOURNAL_READ_FAILED", "Journal records could not be read.") from None

    @staticmethod
    def _require_task(db: sqlite3.Connection, task_id: str) -> None:
        if db.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is None:
            raise JournalError("TASK_NOT_FOUND", "The requested task is not present in this journal.")

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in list(result):
            if key.endswith("_json"):
                value = result.pop(key)
                result[key[:-5]] = json.loads(value) if value is not None else None
        return result

    def create_task(self, task_id: str, plan: dict[str, Any]) -> None:
        task_id, payload, timestamp = _text(task_id), _json(plan), _now()
        with self._transaction() as db:
            row = db.execute("SELECT plan_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise JournalError(
                        "TASK_CONFLICT", "An existing task has a different immutable execution plan."
                    )
                return
            db.execute(
                "INSERT INTO tasks(task_id,status,plan_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                (task_id, "DRAFT", payload, timestamp, timestamp),
            )

    def get_task(self, task_id: str) -> dict[str, Any]:
        rows = self._fetch("SELECT * FROM tasks WHERE task_id=?", (_text(task_id),))
        if not rows:
            raise JournalError("TASK_NOT_FOUND", "The requested task is not present in this journal.")
        return self._decode(rows[0])

    def list_tasks(self) -> list[dict[str, Any]]:
        return [
            self._decode(row) for row in self._fetch("SELECT * FROM tasks ORDER BY created_at DESC,task_id")
        ]

    def set_task_status(self, task_id: str, status: str) -> None:
        task_id, status = _text(task_id), _text(status, maximum=128)
        with self._transaction() as db:
            self._require_task(db, task_id)
            db.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=?", (status, _now(), task_id))

    def set_resource(
        self,
        task_id: str,
        resource_id: str,
        *,
        status: str,
        stage: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        task_id, resource_id = _text(task_id), _text(resource_id)
        status, stage, payload = _text(status, maximum=128), _text(stage, maximum=128), _json(details)
        timestamp = _now()
        with self._transaction() as db:
            self._require_task(db, task_id)
            row = db.execute(
                "SELECT details_json FROM resources WHERE task_id=? AND resource_id=?", (task_id, resource_id)
            ).fetchone()
            if payload is None:
                payload = row[0] if row else "{}"
            db.execute(
                """INSERT INTO resources(task_id,resource_id,status,stage,details_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id,resource_id) DO UPDATE SET
                status=excluded.status,stage=excluded.stage,details_json=excluded.details_json,updated_at=excluded.updated_at""",
                (task_id, resource_id, status, stage, payload, timestamp, timestamp),
            )

    def get_resources(self, task_id: str) -> list[dict[str, Any]]:
        return [
            self._decode(row)
            for row in self._fetch(
                "SELECT * FROM resources WHERE task_id=? ORDER BY created_at,resource_id", (_text(task_id),)
            )
        ]

    def begin_operation(
        self,
        task_id: str,
        resource_id: str,
        action: str,
        target: dict[str, Any],
        expected: dict[str, Any] | None = None,
    ) -> str:
        task_id, resource_id, action = _text(task_id), _text(resource_id), _text(action, maximum=128)
        target_json, expected_json = _json(target), _json(expected)
        operation_id, timestamp = uuid.uuid4().hex, _now()
        with self._transaction() as db:
            self._require_task(db, task_id)
            db.execute(
                """INSERT INTO operations(operation_id,task_id,resource_id,action,target_json,expected_json,
                outcome,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'INTENT',NULL,?,?)""",
                (
                    operation_id,
                    task_id,
                    resource_id,
                    action,
                    target_json,
                    expected_json,
                    timestamp,
                    timestamp,
                ),
            )
        return operation_id

    def finish_operation(
        self, operation_id: str, *, outcome: str, evidence: dict[str, Any] | None = None
    ) -> None:
        operation_id, payload = _text(operation_id), _json(evidence)
        if outcome not in _OUTCOMES:
            raise JournalError(
                "INVALID_OUTCOME", "An operation outcome must be SUCCEEDED, FAILED or UNKNOWN."
            )
        with self._transaction() as db:
            row = db.execute(
                "SELECT outcome,evidence_json FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise JournalError(
                    "OPERATION_NOT_FOUND", "The requested operation is not present in this journal."
                )
            if row[0] in {"SUCCEEDED", "FAILED"}:
                if row[0] == outcome and row[1] == payload:
                    return
                raise JournalError(
                    "OPERATION_FINALIZED", "Final operation evidence cannot be silently changed."
                )
            db.execute(
                "UPDATE operations SET outcome=?,evidence_json=?,updated_at=? WHERE operation_id=?",
                (outcome, payload, _now(), operation_id),
            )

    def operations(self, task_id: str, resource_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM operations WHERE task_id=?", (_text(task_id),)
        if resource_id is not None:
            sql += " AND resource_id=?"
            params += (_text(resource_id),)
        return [self._decode(row) for row in self._fetch(sql + " ORDER BY sequence", params)]

    def record_object(
        self,
        task_id: str,
        resource_id: str,
        kind: str,
        name: str,
        *,
        fingerprint: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        key = tuple(_text(value) for value in (task_id, resource_id, kind, name))
        fingerprint, payload, timestamp = _text(fingerprint), _json(details) or "{}", _now()
        with self._transaction() as db:
            self._require_task(db, task_id)
            row = db.execute(
                "SELECT fingerprint,details_json FROM owned_objects WHERE task_id=? AND resource_id=? AND kind=? AND name=?",
                key,
            ).fetchone()
            if row:
                if row[0] != fingerprint or row[1] != payload:
                    raise JournalError(
                        "OBJECT_CONFLICT",
                        "Existing ownership evidence differs; use an explicit compare-and-swap refresh.",
                    )
                return
            db.execute(
                """INSERT INTO owned_objects(task_id,resource_id,kind,name,initial_fingerprint,fingerprint,
                initial_details_json,details_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                key + (fingerprint, fingerprint, payload, payload, timestamp, timestamp),
            )

    def refresh_object(
        self,
        task_id: str,
        resource_id: str,
        kind: str,
        name: str,
        *,
        expected_fingerprint: str,
        fingerprint: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Update the last observed state after an owned write, preserving creation evidence."""
        key = tuple(_text(value) for value in (task_id, resource_id, kind, name))
        expected_fingerprint, fingerprint = _text(expected_fingerprint), _text(fingerprint)
        payload = _json(details)
        with self._transaction() as db:
            row = db.execute(
                "SELECT fingerprint,details_json FROM owned_objects WHERE task_id=? AND resource_id=? AND kind=? AND name=?",
                key,
            ).fetchone()
            if row is None:
                raise JournalError(
                    "OBJECT_NOT_FOUND", "No ownership evidence exists for the requested object."
                )
            if row[0] != expected_fingerprint:
                raise JournalError("OBJECT_CHANGED", "Ownership evidence has changed since it was read.")
            db.execute(
                """UPDATE owned_objects SET fingerprint=?,details_json=?,updated_at=?
                WHERE task_id=? AND resource_id=? AND kind=? AND name=? AND fingerprint=?""",
                (fingerprint, payload if payload is not None else row[1], _now())
                + key
                + (expected_fingerprint,),
            )

    def objects(self, task_id: str, resource_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM owned_objects WHERE task_id=?", (_text(task_id),)
        if resource_id is not None:
            sql += " AND resource_id=?"
            params += (_text(resource_id),)
        return [self._decode(row) for row in self._fetch(sql + " ORDER BY created_at,kind,name", params)]

    def add_event(
        self,
        task_id: str,
        level: str,
        code: str,
        message: str,
        *,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        task_id, level, code, message = (
            _text(task_id),
            _text(level, maximum=64),
            _text(code, maximum=128),
            _text(message),
        )
        resource_id = _text(resource_id) if resource_id is not None else None
        payload = _json(details)
        with self._transaction() as db:
            self._require_task(db, task_id)
            db.execute(
                "INSERT INTO events(task_id,resource_id,level,code,message,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, resource_id, level, code, message, payload, _now()),
            )

    def events(self, task_id: str) -> list[dict[str, Any]]:
        return [
            self._decode(row)
            for row in self._fetch(
                "SELECT * FROM events WHERE task_id=? ORDER BY event_id", (_text(task_id),)
            )
        ]

    def acquire_lock(self, task_id: str, lock_key: str) -> None:
        task_id, lock_key = _text(task_id), _text(lock_key)
        with self._transaction() as db:
            self._require_task(db, task_id)
            row = db.execute(
                "SELECT task_id,owner_token FROM task_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            if row is not None:
                if row[0] == task_id and row[1] == self._owner_token:
                    return
                raise LockConflict(row[0])
            db.execute(
                "INSERT INTO task_locks(lock_key,task_id,owner_token,hostname,pid,acquired_at) VALUES(?,?,?,?,?,?)",
                (lock_key, task_id, self._owner_token, self._hostname, self._pid, _now()),
            )

    def release_locks(self, task_id: str) -> None:
        """Release only this connection's locks, never another running session's."""
        with self._transaction() as db:
            db.execute(
                "DELETE FROM task_locks WHERE task_id=? AND owner_token=?",
                (_text(task_id), self._owner_token),
            )

    def recover_locks(self, task_id: str) -> int:
        """Explicitly release a crashed task's locks if all foreign owners are dead.

        No mutation occurs if any owner is alive, unreachable or on another host.
        PID reuse fails safely (the lock stays held). Moving the database between
        hosts does not authorize automatic recovery.
        """
        task_id = _text(task_id)
        with self._transaction() as db:
            self._require_task(db, task_id)
            rows = db.execute("SELECT * FROM task_locks WHERE task_id=?", (task_id,)).fetchall()
            recoverable = []
            for row in rows:
                if row["owner_token"] == self._owner_token:
                    continue
                if not self._hostname or row["hostname"] != self._hostname:
                    raise JournalError(
                        "LOCK_RECOVERY_UNSAFE",
                        "A lock belongs to another or unknown host; manual review is required.",
                    )
                if _pid_alive(row["pid"]):
                    raise JournalError("LOCK_OWNER_ACTIVE", "The previous lock owner may still be running.")
                recoverable.append(row["lock_key"])
            for lock_key in recoverable:
                db.execute("DELETE FROM task_locks WHERE lock_key=? AND task_id=?", (lock_key, task_id))
            return len(recoverable)
