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
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from geopublisher import desktop  # noqa: E402
from geopublisher.config import Environment  # noqa: E402
from geopublisher.datasets import DatasetError  # noqa: E402
from geopublisher.desktop import MainWindow  # noqa: E402


def test_controlled_gdal_autodiscovery_only_accepts_complete_bundled_runtime(monkeypatch, tmp_path):
    """A portable build must not silently choose an unknown system GDAL."""
    exe = tmp_path / "GeoPublisher-Workbench.exe"
    monkeypatch.setattr(desktop.sys, "frozen", True, raising=False)
    monkeypatch.setattr(desktop.sys, "executable", str(exe))
    assert desktop.controlled_gdal_bin() == ""

    root = tmp_path / "runtime"
    library = root / "Library" if os.name == "nt" else root
    bin_dir = library / "bin"
    bin_dir.mkdir(parents=True)
    binaries = ("gdalinfo.exe", "ogrinfo.exe") if os.name == "nt" else ("gdalinfo", "ogrinfo")
    for name in binaries:
        (bin_dir / name).write_bytes(b"test")
    (library / "share" / "gdal").mkdir(parents=True)
    assert desktop.controlled_gdal_bin() == ""
    (library / "share" / "proj").mkdir(parents=True)
    (library / "share" / "proj" / "proj.db").write_bytes(b"test")
    assert desktop.controlled_gdal_bin() == str(bin_dir)


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
    # Pump queued GUI signals without entering QTest.qWait's nested Qt loop.
    # A Qt timer/event race there can itself block longer than the deadline.
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            break
        time.sleep(0.005)
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


def test_advanced_options_are_optional_and_preserve_defaults(window):
    assert not window.advanced_import_button.isChecked()
    assert window.advanced_import_options.isHidden()
    assert not window.advanced_settings_button.isChecked()
    assert window.advanced_settings_options.isHidden()
    assert window.encoding.currentData() is None
    assert window.strategy.currentData() == "fail"
    assert window.crs.text() == ""
    assert window.style_path.text() == ""
    window.advanced_import_button.click()
    assert window.advanced_import_button.isChecked()
    assert not window.advanced_import_options.isHidden()
    window.advanced_import_button.click()
    assert window.advanced_import_options.isHidden()
    window.advanced_settings_button.click()
    assert window.advanced_settings_button.isChecked()
    assert not window.advanced_settings_options.isHidden()
    assert window.session_password.echoMode() == window.session_password.EchoMode.Password


def test_readonly_connection_success_uses_only_probe_and_workspace(window, monkeypatch):
    calls = []

    class FakeReadOnlyGeoServer:
        def __init__(self, base_url, username, password, *, timeout, verify):
            assert base_url == "http://127.0.0.1:8080/geoserver"
            assert username == "operator"
            assert password == "temporary-secret"
            assert verify is True
            calls.append("connect")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            calls.append("close")

        def probe(self):
            calls.append("probe")
            return {"geoserver_version": "2.24.4"}

        def get_workspace(self, workspace):
            calls.append("workspace")
            assert workspace == "test"
            return {"name": workspace}

    monkeypatch.setattr("geopublisher.geoserver.GeoServerClient", FakeReadOnlyGeoServer)
    # The UI thread mechanics are already covered by existing worker tests.
    monkeypatch.setattr(
        window, "_start_worker",
        lambda operation, completed: completed(operation(lambda _event: None)),
    )
    window.session_username.setText("operator")
    window.session_password.setText("temporary-secret")
    window._check_connection()
    assert calls == ["connect", "probe", "workspace", "close"]
    assert "连接成功" in window.connection_status.text()
    assert "2.24.4" in window.connection_status.text()
    assert "temporary-secret" not in window.connection_status.text()
    assert not window._connection_testing
    assert not (window._work_dir / "geopublisher.sqlite").exists()


def test_connection_check_still_works_without_gdal_and_reports_missing_workspace(window, monkeypatch):
    class NoWorkspace:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def probe(self):
            return {"geoserver_version": "2.24.4"}

        def get_workspace(self, workspace):
            return None

    window.gdal_bin.clear()
    with pytest.raises(Exception):
        window.environment()
    assert window.environment(connection_only=True).gdal_bin == "READ_ONLY_CONNECTION_CHECK"
    monkeypatch.setattr("geopublisher.geoserver.GeoServerClient", NoWorkspace)
    monkeypatch.setattr(
        window, "_start_worker",
        lambda operation, completed: completed(operation(lambda _event: None)),
    )
    monkeypatch.setenv("GEOPUB_USERNAME", "test-operator")
    monkeypatch.setenv("GEOPUB_PASSWORD", "test-secret")
    window._check_connection()
    assert "工作空间 test 不存在" in window.connection_status.text()
    assert "请管理员先" in window.connection_status.text()


def test_connection_input_errors_are_actionable_and_never_trigger_network(window, monkeypatch):
    def unexpected_client(*_args, **_kwargs):
        pytest.fail("Incomplete credentials must not initiate a network connection")

    monkeypatch.setattr("geopublisher.geoserver.GeoServerClient", unexpected_client)
    window.session_username.setText("one-sided")
    window._check_connection()
    assert window._thread is None
    assert "同时填写" in window.connection_status.text()
    window.session_username.clear()
    window.base_url.setText("http://external.invalid/geoserver")
    window._check_connection()
    assert window._thread is None
    assert "设置尚未完整" in window.connection_status.text()


def test_connection_failure_does_not_destroy_existing_reviewed_plan(window, monkeypatch):
    from geopublisher.geoserver import GeoServerError

    class FailingReadOnlyClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def probe(self):
            raise GeoServerError("CONNECTION_ERROR", "GeoServer cannot be reached.")

    monkeypatch.setattr("geopublisher.geoserver.GeoServerClient", FailingReadOnlyClient)

    def execute_and_report_failure(operation, completed):
        try:
            completed(operation(lambda _event: None))
        except GeoServerError as exc:
            window._worker_failed({"code": exc.code, "message": str(exc)})

    monkeypatch.setattr(window, "_start_worker", execute_and_report_failure)
    monkeypatch.setenv("GEOPUB_USERNAME", "test-operator")
    monkeypatch.setenv("GEOPUB_PASSWORD", "test-secret")
    plan = sample_plan(window)
    window.review_plan(plan)
    window._check_connection()
    assert window._plan is plan
    assert window.execute_button.isEnabled()
    assert "连接未通过" in window.connection_status.text()
    assert "CONNECTION_ERROR" in window.connection_status.text()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("SUCCEEDED", "尚未登记"),
        ("REVIEW_REQUIRED", "人工核对"),
        ("MANUAL_ACTION_REQUIRED", "不要重新上传"),
        ("SKIPPED", "不会更新"),
        ("PARTIAL_SUCCESS", "部分图层"),
        ("FAILED", "发布未完成"),
        ("CANCELLED", "安全检查点"),
    ],
)
def test_result_guidance_explains_next_action_in_plain_chinese(status, expected):
    assert expected in desktop.result_next_action(status)


def test_review_summary_uses_human_readable_strategy_and_result(window):
    plan = sample_plan(window)
    plan["strategy"] = "skip"
    window.review_plan(plan)
    assert "跳过同名图层（不更新）" in window.plan_summary.text()
    assert "栅格 TIFF" == window.resources_table.item(0, 1).text()
    assert "检查通过" in window.review_guidance.text()
    plan["ready"] = False
    plan["resources"][0]["blocking_errors"] = [{"code": "TEST", "message": "检查源文件"}]
    window.review_plan(plan)
    assert "阻断" in window.review_guidance.text()
    assert not window.execute_button.isEnabled()


def test_in_app_help_describes_three_steps_and_safe_behavior(window, monkeypatch):
    dialogs = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda _parent, title, message, *_args: dialogs.append((title, message)),
    )
    window.help_button.click()
    assert len(dialogs) == 1
    title, message = dialogs[0]
    assert "使用帮助" in title
    assert "1." in message and "2." in message and "3." in message
    assert "测试连接（只读）" in message
    assert "不会自动写入" in message
    assert "Mosaic/Pyramid" in message


def test_basic_task_workflow_keeps_advanced_settings_hidden_by_default(window):
    # Avoid native offscreen paint timing in Linux CI; real Windows screenshots
    # verify actual rendering and Chinese font support separately.
    assert not window.advanced_import_button.isChecked()
    assert window.advanced_import_options.isHidden()
    assert window.inspect_button.isEnabled()
    assert window.help_button.isEnabled()
    window._navigate(1)
    assert window.pages.currentIndex() == 1
    assert not window.advanced_settings_button.isChecked()
    assert window.advanced_settings_options.isHidden()
    assert window.connection_test_button.isEnabled()
    window._navigate(0)
    assert window.steps.currentIndex() == 0


def test_server_connected_without_gdal_explains_remaining_local_requirement(window):
    window.gdal_bin.clear()
    window._show_connection_result({
        "version": "2.24.4", "workspace_exists": True, "workspace": "test",
    })
    assert "连接成功" in window.connection_status.text()
    assert "还需选择完整的受控 GDAL" in window.connection_status.text()


def test_convert_gdb_cli_parameters_are_explicit_and_offline():
    from geopublisher.cli import parser

    args = parser().parse_args([
        "convert-gdb", "D:/local/test.gdb", "--output", "D:/out/new",
        "--gdal-bin", "D:/runtime/Library/bin", "--preview",
    ])
    assert args.command == "convert-gdb"
    assert args.preview is True
    assert not args.include_annotation_bounds
    assert args.layer is None


def test_gdb_preview_summary_explains_field_risks_without_automatic_export():
    info = {
        "feature_count": 105,
        "layers": [
            {"name": "A", "feature_count": 100, "crs": "EPSG:4508", "annotation": False,
             "field_map": [{"renamed": True, "omitted": False}], "warnings": []},
            {"name": "A_NOTE", "feature_count": 5, "crs": "EPSG:4508", "annotation": True,
             "field_map": [{"renamed": False, "omitted": True}], "warnings": []},
            {"name": "EMPTY", "feature_count": 0, "crs": "EPSG:4508", "annotation": False,
             "field_map": [], "warnings": []},
        ],
    }
    text = desktop.gdb_preview_summary(info)
    assert "3 个要素类" in text
    assert "105 条要素" in text
    assert "默认计划导出 1 个" in text
    assert "可能需要改短名 1 处" in text
    assert "可能省略 1 处" in text
    assert "默认计划导出 2 个" in desktop.gdb_preview_summary(info, include_annotations=True)


def test_gdb_ui_inspects_before_conversion_and_never_uses_server(
    window, tmp_path, monkeypatch
):
    from geopublisher import datasets, gdb_export

    source = tmp_path / "synthetic.gdb"
    source.mkdir()
    window.output_root.setText(str(tmp_path))
    monkeypatch.setattr(
        desktop.QFileDialog, "getExistingDirectory",
        lambda *_args, **_kwargs: str(source),
    )
    confirm = []
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args: (confirm.append("asked"), QMessageBox.StandardButton.No)[1],
    )
    seen = []

    class LocalRuntime:
        def __init__(self, path, **kwargs):
            seen.append(("GDAL_ONLY", str(path), kwargs["expected_version"]))

    monkeypatch.setattr(datasets, "GdalRuntime", LocalRuntime)

    def inspect(runtime, gdb):
        assert isinstance(runtime, LocalRuntime) and gdb == source
        seen.append(("READ_ONLY_INSPECTION", gdb.name))
        return {"feature_count": 3, "layers": [
            {"name": "TEST", "feature_count": 3, "crs": "EPSG:4326",
             "geometry": "Point", "annotation": False, "field_map": [], "warnings": []}
        ]}

    monkeypatch.setattr(gdb_export, "inspect_gdb", inspect)
    monkeypatch.setattr(window, "_select_gdb_layers", lambda info, annotate: ["TEST"])

    def convert(runtime, gdb, output, *, include_annotations, only_layers):
        assert isinstance(runtime, LocalRuntime)
        assert gdb == source and not include_annotations
        assert only_layers == ["TEST"]
        assert output.parent == tmp_path
        output.mkdir()
        seen.append(("CONVERT_GDB_ONLY", source.name))
        return {
            "destination": str(output),
            "summary": {"EXPORTED_REVIEW_REQUIRED": 3},
        }

    monkeypatch.setattr(gdb_export, "convert_gdb", convert)
    monkeypatch.setattr(
        window, "_start_worker",
        lambda operation, completed: completed(operation(lambda _event: None)),
    )
    window._choose_gdb_and_export()
    assert seen[0][0] == "GDAL_ONLY"
    assert seen[1] == ("READ_ONLY_INSPECTION", "synthetic.gdb")
    assert seen[2] == ("CONVERT_GDB_ONLY", "synthetic.gdb")
    assert len(confirm) == 1
    assert "本地转换完成" in window.activity.text()
    assert window._thread is None


def test_gdb_ui_no_selection_does_not_create_output_or_launch_conversion(
    window, tmp_path, monkeypatch
):
    from geopublisher import datasets, gdb_export

    source = tmp_path / "input.gdb"
    source.mkdir()
    window.output_root.setText(str(tmp_path))
    monkeypatch.setattr(
        desktop.QFileDialog, "getExistingDirectory",
        lambda *_args, **_kwargs: str(source),
    )
    monkeypatch.setattr(datasets, "GdalRuntime", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        gdb_export, "inspect_gdb",
        lambda *_args: {"layers": [], "feature_count": 0},
    )
    monkeypatch.setattr(window, "_select_gdb_layers", lambda *_args: [])
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    monkeypatch.setattr(
        window, "_start_worker",
        lambda operation, completed: completed(operation(lambda _event: None)),
    )
    monkeypatch.setattr(
        gdb_export, "convert_gdb",
        lambda *_args, **_kwargs: pytest.fail("Must not convert without selecting layers"),
    )
    window._choose_gdb_and_export()
    assert "没有勾选" in window.activity.text()
    assert not list(tmp_path.glob("gdb-export-*"))


def test_gdb_layer_selection_prechecks_regular_layers_and_safely_skips_annotations(
    window, monkeypatch
):
    from PySide6.QtWidgets import QDialog

    monkeypatch.setattr(QDialog, "exec", lambda _dialog: QDialog.DialogCode.Accepted)
    source_info = {
        "feature_count": 13,
        "layers": [
            {"name": "FAULT", "feature_count": 10, "crs": "EPSG:4508",
             "geometry": "MultiLineString", "annotation": False,
             "field_map": [], "warnings": []},
            {"name": "TEXT_A", "feature_count": 3, "crs": "EPSG:4508",
             "geometry": "MultiPolygon", "annotation": True,
             "field_map": [], "warnings": []},
            {"name": "EMPTY", "feature_count": 0, "crs": "EPSG:4508",
             "geometry": "Point", "annotation": False,
             "field_map": [], "warnings": []},
        ],
    }
    assert window._select_gdb_layers(source_info, False) == ["FAULT"]
    assert window._select_gdb_layers(source_info, True) == ["FAULT", "TEXT_A"]
    assert "\n" in desktop.gdb_preview_summary(source_info)

def test_style_remap_opt_in_exposed_in_cli_and_hidden_by_default(window):
    from geopublisher.cli import parser

    assert not window.remap_sld_fields.isChecked()
    assert window.advanced_import_options.isHidden()
    window.advanced_import_button.click()
    assert not window.advanced_import_options.isHidden()
    window.remap_sld_fields.click()
    assert window.remap_sld_fields.isChecked()
    parsed = parser().parse_args([
        "plan", "D:/synthetic/fault.shp", "--environment", "D:/synthetic/env.json",
        "--output", "D:/synthetic/fresh-plan", "--sld-directory", "D:/synthetic/styles",
        "--remap-sld-fields",
    ])
    assert parsed.remap_sld_fields is True
    assert parsed.sld_directory.name == "styles"
    without_opt = parser().parse_args([
        "plan", "D:/synthetic/fault.shp", "--environment", "D:/synthetic/env.json",
        "--output", "D:/synthetic/fresh-plan", "--sld-directory", "D:/synthetic/styles",
    ])
    assert without_opt.remap_sld_fields is False


def test_repeated_short_background_workers_clear_thread_state_without_stale_callbacks(window, app):
    """Stress real Qt thread transitions; no GeoServer or filesystem access."""
    completed: list[int] = []
    worker_loop_levels: list[int] = []
    for iteration in range(16):
        def work(on_event, index=iteration):
            worker_loop_levels.append(desktop.QThread.currentThread().loopLevel())
            on_event({"stage": "synthetic", "text": "后台任务完成"})
            return {"sequence": index}

        window._start_worker(work, lambda result: completed.append(result["sequence"]))
        wait_until(app, lambda: window._thread is None, timeout=6)
        assert completed == list(range(iteration + 1))
        assert window._worker is None
        assert window._worker_success is None
        assert window.inspect_button.isEnabled()
        assert window.settings_nav.isEnabled()
    assert len(worker_loop_levels) == 16
    assert all(level >= 1 for level in worker_loop_levels), (
        "A Qt worker ran before its event loop started."
    )
    assert not window._work_dir.exists()


def test_repeated_background_worker_failure_does_not_leave_busy_state(window, app, monkeypatch):
    """A failed worker must surface a safe message and finish its QThread."""
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: warnings.append("seen"))
    for iteration in range(6):
        def work(_event):
            raise DatasetError("SYNTHETIC_WORKER_ERROR", "本地数学检查未完成")

        window._start_worker(work, window.review_plan)
        wait_until(app, lambda: window._thread is None, timeout=6)
        assert warnings == ["seen"] * (iteration + 1)
        assert window._worker is None
        assert window._worker_success is None
        assert "SYNTHETIC_WORKER_ERROR" in window.activity.text()
        assert window.inspect_button.isEnabled()
        assert window.settings_nav.isEnabled()
    assert not window._work_dir.exists()
