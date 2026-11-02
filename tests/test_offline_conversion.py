"""Safety and workflow tests for direct ArcMap/QGIS subprocess conversion."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from geopublisher import offline_conversion as workflow


def fake_qgis_runtime(tmp_path):
    root = tmp_path / "QGIS"
    exe = root / "bin" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"synthetic-python-not-executable")
    for suffix in ("apps/qgis/python", "apps/Python312", "apps/qgis/bin"):
        (root / suffix).mkdir(parents=True)
    return exe


def fake_arcmap_runtime(tmp_path):
    exe = tmp_path / "ArcGIS10.8" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"synthetic-python-not-executable")
    return exe


def test_qgis_environment_does_not_inherit_proxy_cloud_or_geopub_secrets(tmp_path):
    exe = fake_qgis_runtime(tmp_path)
    supplied = {
        "SystemRoot": str(tmp_path / "Windows"), "WINDIR": str(tmp_path / "Windows"),
        "TEMP": str(tmp_path / "temp"), "USERPROFILE": str(tmp_path / "user"),
        "APPDATA": str(tmp_path / "user" / "AppData"),
        "GEOPUB_USERNAME": "secret-user", "GEOPUB_PASSWORD": "secret-password",
        "HTTPS_PROXY": "http://user:password@proxy", "AWS_ACCESS_KEY_ID": "cloud-key",
        "GIT_TOKEN": "do-not-export",
    }
    path, env = workflow.local_python_environment("lyr", exe, supplied)
    assert path == exe
    assert env["PROJ_NETWORK"] == "OFF"
    assert env["QT_QPA_PLATFORM"] == "offscreen"
    assert env["QGIS_PREFIX_PATH"] == str(tmp_path / "QGIS" / "apps" / "qgis")
    assert all(secret not in str(env) for secret in (
        "secret-user", "secret-password", "cloud-key", "do-not-export", "user:password@proxy"
    ))
    assert not {"HTTPS_PROXY", "GEOPUB_PASSWORD", "AWS_ACCESS_KEY_ID", "GIT_TOKEN"} & env.keys()


def test_arcmap_environment_does_not_inherit_parent_path_or_credentials(tmp_path):
    exe = fake_arcmap_runtime(tmp_path)
    path, env = workflow.local_python_environment("mxd", exe, {
        "SystemRoot": str(tmp_path / "Windows"),
        "PATH": "D:/untrusted/run.exe", "GEOPUB_PASSWORD": "do-not-inherit",
        "TEMP": str(tmp_path / "temp"),
    })
    assert path == exe
    assert "D:/untrusted" not in env["PATH"]
    assert "do-not-inherit" not in str(env)
    assert env["PROJ_NETWORK"] == "OFF"


@pytest.mark.parametrize("kind", ["mxd", "lyr"])
def test_runner_creates_new_job_and_executes_only_bundled_script(tmp_path, monkeypatch, kind):
    source = tmp_path / ("input.mxd" if kind == "mxd" else "input-data")
    if kind == "mxd":
        source.write_bytes(b"synthetic-mxd")
    else:
        source.mkdir()
    exe = fake_arcmap_runtime(tmp_path) if kind == "mxd" else fake_qgis_runtime(tmp_path)
    monkeypatch.setattr(workflow, "local_python_environment",
                        lambda _kind, _exe: (exe, {"PATH": str(exe.parent), "PROJ_NETWORK": "OFF"}))
    calls = []

    class LocalProcess:
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
            assert kwargs["shell"] is False
            assert kwargs["env"]["PROJ_NETWORK"] == "OFF"
            assert Path(command[1]).is_file()
            assert command[4] == "--cancel-file"
            assert Path(command[5]).parent == Path(command[1]).parent
            destination = Path(command[3])
            destination.mkdir()
            row = {"source_layer": "math", "status": "EXPORTED"} if kind == "mxd" else {
                "shp": "math.shp", "lyr": "math.lyr", "status": "EXPORTED"
            }
            manifest = "mxd-export-manifest.json" if kind == "mxd" else "lyr-conversion-report.json"
            (destination / manifest).write_text(json.dumps({"items": [row]}), encoding="utf-8")
            self.returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(workflow.subprocess, "Popen", LocalProcess)
    progress = []
    result = workflow.run_offline_conversion(
        kind, source, exe, tmp_path / "results", on_event=progress.append
    )
    assert result["status"] == "SUCCEEDED"
    assert result["converted"] == result["total"] == 1
    assert Path(result["report"]).is_file()
    assert Path(result["job"], "conversion.log").is_file()
    assert calls[0][0][0] == str(exe)
    assert any("成功" in event["text"] for event in progress)


def test_failed_or_partial_helper_cannot_be_reported_as_success(tmp_path, monkeypatch):
    source = tmp_path / "input.mxd"
    source.write_bytes(b"synthetic")
    exe = fake_arcmap_runtime(tmp_path)
    monkeypatch.setattr(workflow, "local_python_environment",
                        lambda *_args: (exe, {"PROJ_NETWORK": "OFF"}))

    class FailedProcess:
        def __init__(self, command, **_kwargs):
            self.returncode = 1
            destination = Path(command[3])
            destination.mkdir()
            (destination / "mxd-export-manifest.json").write_text(
                json.dumps({"items": [
                    {"source_layer": "math", "status": "EXPORTED"},
                    {"source_layer": "other", "status": "FAILED"}
                ]}), encoding="utf-8"
            )

        def poll(self):
            return 1

    monkeypatch.setattr(workflow.subprocess, "Popen", FailedProcess)
    result = workflow.run_offline_conversion("mxd", source, exe, tmp_path / "outputs")
    assert result["status"] == "INCOMPLETE"
    assert result["converted"] == 1
    assert result["total"] == 2


def test_cancel_before_start_does_not_create_output(tmp_path, monkeypatch):
    source = tmp_path / "input.mxd"
    source.write_bytes(b"synthetic")
    exe = fake_arcmap_runtime(tmp_path)
    monkeypatch.setattr(workflow, "local_python_environment",
                        lambda *_args: (exe, {"PROJ_NETWORK": "OFF"}))
    cancel = threading.Event()
    cancel.set()
    target = tmp_path / "results"
    result = workflow.run_offline_conversion("mxd", source, exe, target, cancel_event=cancel)
    assert result["status"] == "CANCELLED"
    assert not target.exists()


def test_source_and_job_output_overlap_is_refused_before_start(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    with pytest.raises(workflow.OfflineConversionError, match="来源文件夹"):
        workflow.run_offline_conversion("lyr", src, "python.exe", src / "new-run")
    assert not (src / "new-run").exists()


def test_runtime_rejects_arbitrary_exe_and_incomplete_qgis(tmp_path):
    bad = tmp_path / "QGIS" / "bin" / "python.exe"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"fake")
    with pytest.raises(workflow.OfflineConversionError, match="缺少"):
        workflow.local_python_environment("lyr", bad)
    arbitrary = tmp_path / "my-runner.exe"
    arbitrary.write_bytes(b"fake")
    with pytest.raises(workflow.OfflineConversionError, match="python.exe"):
        workflow.local_python_environment("mxd", arbitrary)



def test_offline_cancel_during_worker_writes_checkpoint_without_forced_termination(tmp_path, monkeypatch):
    source = tmp_path / "input.mxd"
    source.write_bytes(b"synthetic")
    exe = fake_arcmap_runtime(tmp_path)
    monkeypatch.setattr(workflow, "local_python_environment",
                        lambda *_args: (exe, {"PROJ_NETWORK": "OFF"}))
    cancel = threading.Event()

    class CooperativeProcess:
        def __init__(self, command, **_kwargs):
            self.marker = Path(command[5])
            self.returncode = None
            self.checks = 0
            output = Path(command[3])
            output.mkdir()
            (output / "mxd-export-manifest.json").write_text(
                json.dumps({"items": [{"source_layer": "math", "status": "CANCELLED"}]}),
                encoding="utf-8",
            )

        def poll(self):
            self.checks += 1
            if self.checks == 1:
                cancel.set()
                return None
            assert self.marker.is_file()
            self.returncode = 1
            return 1

    monkeypatch.setattr(workflow.subprocess, "Popen", CooperativeProcess)
    result = workflow.run_offline_conversion(
        "mxd", source, exe, tmp_path / "jobs", cancel_event=cancel,
    )
    assert result["status"] == "CANCELLED"
    assert (Path(result["job"]) / "cancel.request").is_file()
