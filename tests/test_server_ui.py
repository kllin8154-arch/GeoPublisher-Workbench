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
