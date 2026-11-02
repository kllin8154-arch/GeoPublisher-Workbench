"""Run bundled ArcMap/QGIS helpers only via explicitly chosen local Python executables.

Never shell out to a .bat/.cmd, inherit credentials/proxies, or contact GeoServer.
Conversions create a new private job directory; no source file is modified.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from .arcgis_bridge import arcmap_helper_bytes, qgis_helper_bytes


class OfflineConversionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

def suggested_runtime(kind: str) -> str:
    """Convenience hints only, never search PATH or execute an unknown binary."""
    if os.name != "nt":
        return ""
    candidates = (
        (r"C:\Python27\ArcGIS10.8\python.exe", r"D:\Python27\ArcGIS10.8\python.exe")
        if kind == "mxd" else
        (r"D:\QGIS\bin\python.exe", r"C:\OSGeo4W\bin\python.exe")
    )
    return next((candidate for candidate in candidates if Path(candidate).is_file()), "")

def _safe_local_path(path: Path, *, must_exist: bool = True) -> Path:
    value = path.expanduser().absolute()
    if os.name == "nt" and (str(value).startswith("\\\\") or str(value).startswith("//")):
        raise OfflineConversionError("OFFLINE_UNC_PATH", "只允许本地文件，拒绝 UNC/网络共享路径。")
    if value.is_symlink() or any(parent.is_symlink() for parent in value.parents):
        raise OfflineConversionError("OFFLINE_SYMLINK", "输入、运行时或输出路径不能包含符号链接。")
    if must_exist and not value.exists():
        raise OfflineConversionError("OFFLINE_MISSING", "选择的本地文件或目录不存在。")
    return value

def _system_environment(parent: Mapping[str, str]) -> dict[str, str]:
    allowed = ("SystemRoot", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA",
               "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH")
    names = {name.casefold(): name for name in parent}
    result = {key: parent[names[key.casefold()]] for key in allowed if key.casefold() in names}
    if os.name == "nt" and not result.get("SystemRoot"):
        raise OfflineConversionError("OFFLINE_SYSTEM_ROOT", "Windows 系统目录未配置，无法运行本地 GIS 软件。")
    return result

def local_python_environment(kind: str, executable: Path | str,
                             parent: Mapping[str, str] | None = None) -> tuple[Path, dict[str, str]]:
    """Validate a chosen Python and construct minimal OS/GIS-specific environment."""
    if kind not in ("mxd", "lyr"):
        raise OfflineConversionError("OFFLINE_KIND", "只支持 MXD 或同名 SHP/LYR 离线转换。")
    python_exe = _safe_local_path(Path(executable))
    if python_exe.name.casefold() not in {"python.exe", "python3.exe"} or not python_exe.is_file():
        raise OfflineConversionError("OFFLINE_PYTHON", "必须选择 ArcGIS/QGIS 安装目录内的 python.exe。")
    parent_env = os.environ if parent is None else parent
    env = _system_environment(parent_env)
    system_root = Path(env.get("SystemRoot", r"C:\Windows"))
    paths = [str(system_root / "System32"), str(system_root),
             str(system_root / "System32" / "Wbem")]
    env.update({"PROJ_NETWORK": "OFF", "GDAL_PAM_ENABLED": "NO",
                "SHAPE_RESTORE_SHX": "NO", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": "",
                "PYTHONNOUSERSITE": "1"})
    if kind == "lyr":
        root = python_exe.parent.parent
        if python_exe.parent.name.casefold() != "bin":
            raise OfflineConversionError("OFFLINE_QGIS_PATH", "请选择 QGIS 安装目录 bin/python.exe。")
        required = [root / "apps" / "qgis" / "python",
                    root / "apps" / "Python312",
                    root / "apps" / "qgis" / "bin"]
        if not all(path.is_dir() for path in required):
            raise OfflineConversionError("OFFLINE_QGIS_RUNTIME", "所选 QGIS 缺少应用或 Python 3.12 运行时。")
        paths = [str(root / "apps" / "qgis" / "bin"), str(root / "apps" / "qt6" / "bin"),
                 str(root / "apps" / "Python312" / "Scripts"), str(root / "bin"), *paths]
        env.update({
            "PYTHONHOME": str(root / "apps" / "Python312"),
            "PYTHONPATH": str(root / "apps" / "qgis" / "python"),
            "QGIS_PREFIX_PATH": str(root / "apps" / "qgis"),
            "QT_PLUGIN_PATH": os.pathsep.join([
                str(root / "apps" / "qgis" / "qtplugins"), str(root / "apps" / "qt6" / "plugins")]),
            "GDAL_DATA": str(root / "apps" / "gdal" / "share" / "gdal"),
            "GDAL_DRIVER_PATH": str(root / "apps" / "gdal" / "lib" / "gdalplugins"),
            "PROJ_DATA": str(root / "share" / "proj"),
            "OSGEO4W_ROOT": str(root),
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONUTF8": "1",
        })
    else:
        if "arcgis" not in str(python_exe.parent).casefold():
            raise OfflineConversionError("OFFLINE_ARCPY_PATH", "请选择 ArcGIS Desktop 10.x 的 Python 2.7 运行时。")
        paths = [str(python_exe.parent), *paths]
    env["PATH"] = os.pathsep.join(paths)
    return python_exe, env

def run_offline_conversion(kind: str, source: Path | str, executable: Path | str,
                           output_root: Path | str, *, on_event: Callable | None = None,
                           cancel_event=None) -> dict:
    """A synchronous worker-thread operation. Cancel cooperatively between layers."""
    if kind not in ("mxd", "lyr"):
        raise OfflineConversionError("OFFLINE_KIND", "转换类型错误。")
    src = _safe_local_path(Path(source))
    if kind == "mxd" and (not src.is_file() or src.suffix.casefold() != ".mxd"):
        raise OfflineConversionError("OFFLINE_MXD", "请选择本地有效的 .mxd 文件。")
    if kind == "lyr" and not src.is_dir():
        raise OfflineConversionError("OFFLINE_LYR_DIR", "请选择包含同名 SHP/LYR 的本地文件夹。")
    root = _safe_local_path(Path(output_root), must_exist=False)
    # For LYR folders prevent helper output inside the source scan tree.
    # For a single MXD, a fresh job subfolder beside the source is safe.
    if kind == "lyr" and (root == src or root.is_relative_to(src)):
        raise OfflineConversionError("OFFLINE_OUTPUT_OVERLAP", "任务输出根目录不能位于来源文件夹内部。")
    python_exe, env = local_python_environment(kind, executable)
    if cancel_event is not None and cancel_event.is_set():
        return {"status": "CANCELLED", "kind": kind, "total": 0, "converted": 0}
    root.mkdir(parents=True, exist_ok=True)
    job = root / (f"offline-{kind}-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    job.mkdir(exist_ok=False)
    helper = job / ("arcmap_mxd_to_shp.py" if kind == "mxd" else "qgis_lyr_to_sld.py")
    helper.write_bytes(arcmap_helper_bytes() if kind == "mxd" else qgis_helper_bytes())
    destination = job / "output"
    cancel_flag = job / "cancel.request"
    log_path = job / "conversion.log"
    command = [str(python_exe), str(helper), str(src), str(destination),
               "--cancel-file", str(cancel_flag)]
    notify = on_event or (lambda _event: None)
    notify({"text": f"开始本地 {'MXD' if kind == 'mxd' else 'LYR'} 转换；原始文件保持不变。"})
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    started = time.monotonic()
    last_lines = 0
    log_offset = 0
    pending_line = b""
    with log_path.open("wb") as log:
        try:
            process = subprocess.Popen(
                command, cwd=str(job), env=env, shell=False,
                stdout=log, stderr=subprocess.STDOUT, creationflags=creationflags,
            )
        except OSError:
            raise OfflineConversionError("OFFLINE_LAUNCH", "无法启动所选 Python；请检查本地运行时。") from None
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set() and not cancel_flag.exists():
                cancel_flag.write_text("cancel", encoding="ascii")
                notify({"text": "已请求取消；转换将在下一图层检查点安全停止。"})
            # A bounded, local log is retained for troubleshooting; only trusted
            # status tokens reach the UI, never raw external process output.
            with log_path.open("rb") as reader:
                reader.seek(log_offset)
                data = reader.read(16384)
                log_offset += len(data)
            if data:
                complete = (pending_line + data).split(b"\n")
                pending_line = complete.pop()[-1024:]
                for line in complete:
                    if line.strip().startswith(
                        (b"STYLE_EXPORTED=", b"STYLE_FAILED=", b"STYLE_CANCELLED=")
                    ):
                        last_lines += 1
                if last_lines:
                    notify({"text": f"已处理 {last_lines} 个图层。"})
            time.sleep(0.15)
        return_code = process.returncode
    report_path = destination / ("mxd-export-manifest.json" if kind == "mxd" else "lyr-conversion-report.json")
    items: list[dict] = []
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            items = report.get("items", [])
            if not isinstance(items, list):
                raise ValueError("items")
        except (OSError, ValueError):
            raise OfflineConversionError("OFFLINE_REPORT_INVALID", "转换报告损坏；请检查本地任务目录。") from None
    success_rows = [row for row in items if row.get("status") == "EXPORTED"]
    canceled = cancel_flag.exists() or any(row.get("status") == "CANCELLED" for row in items)
    all_ok = return_code == 0 and bool(items) and len(success_rows) == len(items)
    result = {
        "kind": kind, "status": "SUCCEEDED" if all_ok else "CANCELLED" if canceled else "INCOMPLETE",
        "source": str(src), "output": str(destination), "job": str(job),
        "report": str(report_path) if report_path.is_file() else None,
        "converted": len(success_rows), "total": len(items),
        "exit_code": return_code, "seconds": round(time.monotonic() - started, 2),
    }
    notify({"text": f"本地转换结束：成功 {len(success_rows)} / {len(items)}；状态 {result['status']}。"})
    return result
