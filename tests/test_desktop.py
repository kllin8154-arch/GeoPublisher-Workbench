"""Offscreen checks of user-facing safeguards and responsive worker execution."""

from __future__ import annotations

import copy
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

from geopublisher import desktop  # noqa: E402
from geopublisher.config import Environment  # noqa: E402
from geopublisher.datasets import DatasetError  # noqa: E402
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


def two_raster_plan(window, *, confirmed=True):
    plan = sample_plan(window)
    second = copy.deepcopy(plan["resources"][0])
    second.update(resource_id="r0002", target_name="another_sample")
    plan["resources"].append(second)
    if confirmed:
        plan["schema_version"] = 2
        plan["import_intent"] = {
            "schema_version": 1,
            "raster_mode": "independent",
            "raster_count": 2,
            "resource_count": 2,
            "selections": [],
            "datasets": [],
        }
    return plan


def test_startup_does_not_create_working_files_or_scan_sources(window):
    assert not window._work_dir.exists()
    assert window.sources.count() == 0
    assert window.steps.currentIndex() == 0
    assert not window.execute_button.isEnabled()
    assert not window.independent_tiffs.isChecked()


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


def test_multiple_tiffs_can_be_selected_without_implicitly_confirming_publication(window, tmp_path):
    files = [tmp_path / "a.tif", tmp_path / "b.tiff", tmp_path / "c.qml"]
    for path in files:
        path.write_bytes(b"fixture")
    window.add_sources([str(files[0]), str(files[1]), str(files[0]), str(files[2])])
    assert window.sources.count() == 2
    assert not window.execute_button.isEnabled()
    assert not window.independent_tiffs.isChecked()


@pytest.mark.parametrize("change", ["add", "clear", "edit"])
def test_source_change_clears_raster_confirmation_and_reviewed_plan(window, tmp_path, change):
    source = tmp_path / "a.tif"
    source.write_bytes(b"fixture")
    window.add_sources([str(source)])
    window.independent_tiffs.setChecked(True)
    window.review_plan(two_raster_plan(window))
    assert window.execute_button.isEnabled()
    if change == "add":
        another = tmp_path / "b.tif"
        another.write_bytes(b"fixture")
        window.add_sources([str(another)])
    elif change == "clear":
        window._clear_sources()
    else:
        window.sources.item(0).setText(str(tmp_path / "changed.tif"))
    assert not window.independent_tiffs.isChecked()
    assert window._plan is None
    assert not window.execute_button.isEnabled()


def test_duplicate_source_does_not_clear_an_unchanged_confirmation(window, tmp_path):
    source = tmp_path / "a.tif"
    source.write_bytes(b"fixture")
    window.add_sources([str(source)])
    window.independent_tiffs.setChecked(True)
    plan = two_raster_plan(window)
    window.review_plan(plan)
    window.add_sources([str(source)])
    assert window.independent_tiffs.isChecked()
    assert window._plan is plan


def test_changing_raster_mode_invalidates_reviewed_plan(window):
    window.independent_tiffs.setChecked(True)
    window.review_plan(two_raster_plan(window))
    window.independent_tiffs.setChecked(False)
    assert window._plan is None
    assert not window.execute_button.isEnabled()


@pytest.mark.parametrize("confirmed", [False, True])
def test_background_plan_uses_only_explicit_raster_choice(window, tmp_path, app, monkeypatch, confirmed):
    files = [tmp_path / "a.tif", tmp_path / "b.tif"]
    for path in files:
        path.write_bytes(b"fixture")
    window.add_sources([str(path) for path in files])
    window.independent_tiffs.setChecked(confirmed)
    main_thread = threading.get_ident()
    recorded, warnings = {}, []
    result_plan = two_raster_plan(window)

    def build_plan(environment, sources, output, **options):
        assert threading.get_ident() != main_thread
        recorded.update(sources=sources, options=options)
        if options["raster_mode"] is None:
            raise DatasetError("RASTER_INTENT_REQUIRED", "发现 2 个 TIFF，请确认分别发布。")
        return result_plan

    monkeypatch.setattr(desktop, "build_plan", build_plan)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    window._start_plan()
    wait_until(app, lambda: window._thread is None)
    assert recorded["sources"] == files
    assert recorded["options"]["raster_mode"] == ("independent" if confirmed else None)
    assert window.execute_button.isEnabled() is confirmed
    if confirmed:
        assert "2 个 TIFF 分别发布为 2 个独立图层" in window.plan_summary.text()
        assert not warnings
    else:
        assert window._plan is None
        assert "RASTER_INTENT_REQUIRED" in warnings[0]


def test_restored_raster_intent_is_visible_and_not_overridden_by_form_defaults(window, monkeypatch):
    plan = two_raster_plan(window)
    original = copy.deepcopy(plan)
    messages = []
    monkeypatch.setattr(desktop, "load_plan", lambda _: plan)

    def decline(*args):
        messages.append(args[2])
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    window.restore_plan("synthetic-plan.json")
    assert not window.independent_tiffs.isChecked()
    assert window._plan is plan
    assert "2 个 TIFF 分别发布为 2 个独立图层" in window.plan_summary.text()
    window._confirm_run()
    assert "2 个 TIFF 分别发布为 2 个独立图层" in messages[0]
    assert plan == original
    assert window._thread is None


def test_legacy_multiple_raster_plan_cannot_run_without_recorded_intent(window, monkeypatch):
    plan = two_raster_plan(window, confirmed=False)
    original = copy.deepcopy(plan)
    window.review_plan(plan)
    assert "旧计划未记录" in window.plan_summary.text()
    assert "重新选择数据并生成计划" in window.activity.text()
    assert not window.execute_button.isEnabled()
    window._set_busy(False)
    assert not window.execute_button.isEnabled()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args: pytest.fail("Unconfirmed legacy plan asked to run.")
    )
    window._confirm_run()
    assert plan == original
    assert window._thread is None


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
        assert gate.wait(10)
        return plan

    window._start_worker(operation, window.review_plan)
    QTimer.singleShot(20, lambda: ticks.append("responsive"))
    try:
        wait_until(app, lambda: bool(ticks))
        assert window._thread is not None
        assert not window.inspect_button.isEnabled()
        assert not window.independent_tiffs.isEnabled()
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
