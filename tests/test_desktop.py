"""Offscreen checks of user-facing safeguards and responsive worker execution."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from geopublisher.config import Environment  # noqa: E402
from geopublisher.desktop import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    value = MainWindow(work_dir=tmp_path / "work")
    value.apply_environment(
        Environment("local-test", "http://127.0.0.1:8080/geoserver", "test", str(tmp_path / "gdal"))
    )
    yield value
    assert value._thread is None, "A test must wait for its worker before closing the window."
    value.close()
    app.processEvents()


def wait_until(app, predicate, *, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert predicate(), "The UI did not reach the expected state."


def sample_plan(window, *, ready=True):
    return {
        "schema_version": 1,
        "task_id": "00000000-0000-0000-0000-000000000001",
        "environment": window.environment().to_dict(),
        "strategy": "fail",
        "ready": ready,
        "output_dir": str(window._work_dir / "task-1"),
        "resources": [
            {
                "resource_id": "r0001",
                "target_name": "sample",
                "kind": "geotiff",
                "inspection": {"crs": "EPSG:4326"},
                "blocking_errors": [] if ready else [{"code": "CRS_REQUIRED", "message": "请确认坐标系"}],
                "warnings": [],
            }
        ],
    }


def test_startup_does_not_create_working_files_or_scan_sources(window):
    assert not window._work_dir.exists()
    assert window.sources.count() == 0
    assert window.steps.currentIndex() == 0
    assert not window.execute_button.isEnabled()


def test_profile_export_never_contains_session_credentials(window, tmp_path):
    window.session_username.setText("private-user")
    window.session_password.setText("never-write-this-password")
    target = tmp_path / "profile.json"
    window.save_environment(target)
    text = target.read_text(encoding="utf-8")
    saved = json.loads(text)
    assert "never-write-this-password" not in text
    assert "private-user" not in text
    assert saved["password_env"] == "GEOPUB_PASSWORD"
    assert saved["username_env"] == "GEOPUB_USERNAME"
    assert Environment.load(target).environment_key == "local-test"


def test_multiple_independent_tiffs_are_accepted_and_duplicates_ignored(window, tmp_path):
    files = [tmp_path / "a.tif", tmp_path / "b.tiff", tmp_path / "c.qml"]
    for path in files:
        path.write_bytes(b"fixture")
    window.add_sources([str(files[0]), str(files[1]), str(files[0]), str(files[2])])
    assert window.sources.count() == 2
    assert not window.execute_button.isEnabled()


def test_blocked_plan_cannot_be_executed(window, monkeypatch):
    window.review_plan(sample_plan(window, ready=False))
    assert "请确认坐标系" in window.check_details.toPlainText()
    assert not window.execute_button.isEnabled()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: pytest.fail("Blocked plan asked to run.")
    )
    window._confirm_run()
    assert window._thread is None


def test_input_change_invalidates_previously_reviewed_plan(window):
    window.review_plan(sample_plan(window))
    assert window.execute_button.isEnabled()
    window.workspace.setText("different_workspace")
    assert window._plan is None
    assert not window.execute_button.isEnabled()


def test_worker_keeps_event_loop_responsive_and_ready_plan_appears_after_completion(window, app):
    gate = threading.Event()
    plan = sample_plan(window)
    ticks = []

    def operation(_):
        assert gate.wait(3)
        return plan

    window._start_worker(operation, window.review_plan)
    QTimer.singleShot(20, lambda: ticks.append("responsive"))
    try:
        wait_until(app, lambda: bool(ticks))
        assert window._thread is not None
        assert not window.inspect_button.isEnabled()
    finally:
        gate.set()
        wait_until(app, lambda: window._thread is None)
    assert window.steps.currentIndex() == 1
    assert window.execute_button.isEnabled()


def test_declining_confirmation_performs_no_write(window, monkeypatch):
    window.review_plan(sample_plan(window))
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    window._confirm_run()
    assert window._thread is None
    assert window.steps.currentIndex() == 1


def test_execution_uses_reviewed_plan_and_session_credentials_without_saving_them(window, app, monkeypatch):
    plan = sample_plan(window)
    window.review_plan(plan)
    window.session_username.setText("session-user")
    window.session_password.setText("session-secret")
    recorded = {}

    def run_plan(actual_plan, journal_path=None, *, credentials=None, on_event=None, cancel_event=None):
        recorded.update(plan=actual_plan, journal_path=journal_path, credentials=credentials)
        on_event({"resource_id": "r0001", "text": "正在核对服务端"})
        assert isinstance(cancel_event, threading.Event)
        return {
            "status": "MANUAL_ACTION_REQUIRED",
            "resources": [{"resource_id": "r0001", "status": "MANUAL_ACTION_REQUIRED"}],
        }

    module = types.ModuleType("geopublisher.engine")
    module.run_plan = run_plan
    monkeypatch.setitem(sys.modules, "geopublisher.engine", module)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window._confirm_run()
    wait_until(app, lambda: window._thread is None)
    assert recorded["plan"] is plan
    assert recorded["credentials"] == ("session-user", "session-secret")
    assert "session-secret" not in json.dumps(plan)
    assert "需要人工核对" in window.result_title.text()
    assert "没有可展示" in window.preview.text()
    assert "正在核对服务端" in window.run_log.toPlainText()


def test_cancel_sets_checkpoint_request_and_does_not_kill_worker(window, app):
    started = threading.Event()

    def operation(_):
        started.set()
        assert window._cancel.wait(3)
        return {"status": "CANCELLED"}

    window._plan = sample_plan(window)
    window._start_worker(operation, window.show_result)
    wait_until(app, started.is_set)
    window._request_cancel()
    wait_until(app, lambda: window._thread is None)
    assert window._cancel.is_set()
    assert window.result_title.text() == "已安全取消"


def test_preview_only_displays_existing_task_artifacts(window, tmp_path):
    plan = sample_plan(window)
    output = Path(plan["output_dir"])
    folder = output / "r0001"
    folder.mkdir(parents=True)
    image = QImage(32, 32, QImage.Format.Format_RGB32)
    image.fill(QColor("green"))
    assert image.save(str(folder / "wms.png"))
    outside = tmp_path / "unrelated.png"
    assert image.save(str(outside))
    window.review_plan(plan)
    window.show_result(
        {"status": "SUCCEEDED", "resources": [{"resource_id": "r0001", "preview_path": str(outside)}]}
    )
    assert window._preview_paths == [(folder / "wms.png").resolve()]
    assert window._preview_pixmap is not None
    assert not window._preview_pixmap.isNull()


def test_loading_another_profile_clears_session_credentials(window):
    window.session_username.setText("private-user")
    window.session_password.setText("private-password")
    window.apply_environment(window.environment())
    assert window.session_username.text() == ""
    assert window.session_password.text() == ""
