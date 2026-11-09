"""Offscreen SSH publish controls: no real network/credentials/files touched."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

from geopublisher.desktop import MainWindow  # noqa: E402
from geopublisher.server_deploy import LinuxTarget, ServerDeployError  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path):
    w = MainWindow(work_dir=tmp_path)
    yield w
    until = time.monotonic() + 5
    while w._thread is not None and time.monotonic() < until:
        qapp.processEvents()
        time.sleep(.01)
    assert w._thread is None
    w.close()
    qapp.processEvents()


def _wait(qapp, window):
    until = time.monotonic() + 5
    while window._thread is not None and time.monotonic() < until:
        qapp.processEvents()
        time.sleep(.01)
    qapp.processEvents()
    assert window._thread is None


def _settings(operation, **kwargs):
    return {
        "operation": operation,
        "target": LinuxTarget(mode="local", root="/srv/geopublisher/synthetic"),
        "password": None, "passphrase": None,
        "archive": "", "manifest": "",
        "plan_file": "",
        "geoserver_user": "", "geoserver_password": "",
        **kwargs,
    }


@contextmanager
def _fake_session(*_args, **_kw):
    class Session:
        def run(self, operation, *args):
            assert operation == "os"
            return "Linux"
    yield Session()


def test_ui_has_server_button_with_opt_in_transfer_cancel(window):
    assert "Linux" in window.server_deploy_button.text()
    assert window.server_deploy_button.isEnabled()
    assert not window.server_cancel_button.isVisible()
    assert not window.server_cancel_button.isEnabled()


def test_readonly_server_doctor_does_not_mutate_plan(window, qapp, monkeypatch):
    import geopublisher.server_deploy as deploy
    original_plan = {"task_id": "synthetic-keep-original"}
    window._plan = original_plan
    monkeypatch.setattr(window, "_server_deploy_dialog", lambda: _settings("doctor"))
    monkeypatch.setattr(deploy, "open_target", _fake_session)
    monkeypatch.setattr(deploy, "preflight", lambda *_a: {
        "status": "READ_ONLY_PREFLIGHT_OK", "target": {"host": "localhost"},
    })
    info = []
    monkeypatch.setattr(QMessageBox, "information", lambda *_a: info.append("done"))
    window._server_deploy()
    _wait(qapp, window)
    assert info == ["done"]
    assert window._plan is original_plan
    assert not window._server_deploy_running


def test_ssh_plan_requires_two_confirmations_and_preserves_existing_task(
    window, qapp, monkeypatch
):
    import geopublisher.server_deploy as deploy
    import geopublisher.server_plan as plan_module
    settings = _settings("publish_plan", plan_file="D:/synthetic/plan.json",
                         geoserver_user="test", geoserver_password="do-not-save")
    monkeypatch.setattr(window, "_server_deploy_dialog", lambda: settings)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *_a: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QInputDialog, "getText",
                        lambda *_a: ("PUBLISH_PLAN_SSH", True))
    seen = []
    monkeypatch.setattr(deploy, "open_target", _fake_session)
    def fake_publish(target, source, session, **kwargs):
        seen.append((source, kwargs["username"], kwargs["password"]))
        return {"status": "REVIEW_REQUIRED"}
    monkeypatch.setattr(plan_module, "publish_plan_via_tunnel", fake_publish)
    monkeypatch.setattr(QMessageBox, "information", lambda *_a: None)
    window._plan = {"task_id": "original-draft"}
    window._server_deploy()
    _wait(qapp, window)
    assert seen == [("D:/synthetic/plan.json", "test", "do-not-save")]
    assert window._plan["task_id"] == "original-draft"
    assert not window._server_deploy_running


def test_server_failure_does_not_erase_working_plan(window, qapp, monkeypatch):
    import geopublisher.server_deploy as deploy
    window._plan = {"task_id": "keep-on-error"}
    monkeypatch.setattr(window, "_server_deploy_dialog", lambda: _settings("doctor"))
    def fail_connection(*_a, **_kw):
        raise ServerDeployError("HOST_UNAVAILABLE", "Test-only missing SSH connection")
    monkeypatch.setattr(deploy, "open_target", fail_connection)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_a: warnings.append("reported"))
    window._server_deploy()
    _wait(qapp, window)
    assert warnings == ["reported"]
    assert window._plan["task_id"] == "keep-on-error"
    assert not window._server_deploy_running


def test_decline_ssh_write_means_no_server_connection(window, monkeypatch):
    settings = _settings("publish_plan", plan_file="D:/synthetic/plan.json",
                         geoserver_user="test", geoserver_password="do-not-save")
    monkeypatch.setattr(window, "_server_deploy_dialog", lambda: settings)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *_a: QMessageBox.StandardButton.No)
    window._server_deploy()
    assert window._thread is None
    assert not window._server_deploy_running



def test_database_catalog_doctor_runs_in_worker_and_leaves_business_sql_untouched(
    window, qapp, monkeypatch
):
    import geopublisher.catalog_live as live
    import geopublisher.server_deploy as deploy

    original = {"task_id": "do-not-modify"}
    window._plan = original
    settings = _settings(
        "db_doctor", catalog_file="D:/synthetic/review.json",
        database="demo_db", database_user="readonly",
        database_password="ephemeral-db-secret",
    )
    monkeypatch.setattr(window, "_server_deploy_dialog", lambda: settings)
    monkeypatch.setattr(deploy, "open_target", _fake_session)
    calls = []
    def fake_probe(target, session, file, **kwargs):
        calls.append((file, kwargs["database"], kwargs["username"], kwargs["password"]))
        return {
            "status": "READ_ONLY_EVIDENCE_READY_NOT_APPROVED",
            "review": "D:/synthetic/output/report.json",
            "database_modified": False,
        }
    monkeypatch.setattr(live, "probe_catalog", fake_probe)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: calls.append("review-ready"))
    window._server_deploy()
    _wait(qapp, window)
    assert calls == [
        ("D:/synthetic/review.json", "demo_db", "readonly", "ephemeral-db-secret"),
        "review-ready",
    ]
    assert window._plan is original
    assert not window._server_deploy_running


def test_missing_database_password_fails_before_connection(window, monkeypatch):
    import geopublisher.server_deploy as deploy

    settings = _settings(
        "db_doctor", catalog_file="D:/synthetic/review.json",
        database="demo_db", database_user="readonly",
    )
    monkeypatch.setattr(window, "_server_deploy_dialog", lambda: settings)
    monkeypatch.setattr(deploy, "open_target",
                        lambda *_args, **_kw: pytest.fail("Cannot connect before DB credential"))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warned.append("missing"))
    window._server_deploy()
    assert warned == ["missing"]
    assert window._thread is None


def test_server_dialog_shows_only_selected_step_input_rows(window, monkeypatch):
    from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout

    inspected = []
    def fake_dialog_exec(dialog):
        controls = [
            obj for obj in dialog.findChildren(QComboBox)
            if obj.findData("db_doctor") >= 0
        ]
        assert len(controls) == 1
        selection = controls[0]
        forms = [x for x in dialog.findChildren(QFormLayout) if x.rowCount() >= 15]
        assert len(forms) == 1
        form = forms[0]
        def is_row_visible(label):
            for idx in range(form.rowCount()):
                item = form.itemAt(idx, QFormLayout.ItemRole.LabelRole)
                if item is not None and item.widget() is not None:
                    if item.widget().text() == label:
                        return form.isRowVisible(idx)
            raise AssertionError(f"Missing form label {label}")
        assert is_row_visible("Linux 发布包目录")
        assert not is_row_visible("数据库密码")
        selection.setCurrentIndex(selection.findData("db_doctor"))
        assert is_row_visible("数据库密码")
        assert is_row_visible("待核查业务目录 JSON")
        assert not is_row_visible("GeoServer REST 密码")
        assert not is_row_visible("本地发布归档")
        selection.setCurrentIndex(selection.findData("publish_plan"))
        assert is_row_visible("已有发布任务")
        assert not is_row_visible("数据库密码")
        inspected.append(True)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", fake_dialog_exec)
    assert window._server_deploy_dialog() is None
    assert inspected == [True]



def test_database_conflicts_surface_as_warning_with_report_path(window, monkeypatch):
    from geopublisher import desktop

    warning = []
    monkeypatch.setattr(desktop.QMessageBox, "warning",
                        lambda _self, title, body: warning.append((title, body)))
    monkeypatch.setattr(desktop.QMessageBox, "information",
                        lambda *_args: pytest.fail("Blocking database conflicts must not look like success"))
    window._server_deploy_completed({
        "status": "BLOCKED_PENDING_HUMAN_REVIEW",
        "review": "D:/synthetic/review/database.json",
        "blocking_reasons": ["Proposed node code already exists"],
    })
    assert len(warning) == 1
    assert "database.json" in warning[0][1]
    assert "Proposed node code already exists" in warning[0][1]
