"""Local provenance for SHPs exported from a whole GDB or MXD.

This report is a *selection and lineage* hint. It does not prove style or WMS
acceptance and cannot authorize database writes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .datasets import DatasetError
from .plans import digest_file

_REPORTS = {"conversion-report.json": "gdb", "mxd-export-manifest.json": "mxd"}
_GOOD = {"gdb": {"EXPORTED", "EXPORTED_REVIEW_REQUIRED"},
         "mxd": {"EXPORTED", "SHP_EXPORTED_LYR_FAILED"}}
_COMPONENTS = {".shp", ".shx", ".dbf", ".prj", ".cpg"}


def _fail(code: str, message: str) -> None:
    raise DatasetError(code, message)


def _local(path: Path, *, is_dir: bool = False) -> Path:
    path = Path(path).expanduser().absolute()
    if str(path).startswith(("\\\\", "//")) or any(
        item.is_symlink() for item in (path, *path.parents)
    ):
        _fail("BATCH_UNSAFE_PATH", "只允许本机普通文件和目录，禁止网络共享及符号链接。")
    if not (path.is_dir() if is_dir else path.is_file()):
        _fail("BATCH_FILE_MISSING", "转换结果或报告文件不存在。")
    return path.resolve()


def _label(value: object) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 160) or any(
        ord(c) < 32 or ord(c) == 127 for c in value
    ):
        _fail("BATCH_INVALID_REPORT", "转换清单的图层名称无效。")
    return value


def _filename(value: object, suffix: str | None = None) -> str:
    name = _label(value)
    if (
        name in {".", ".."} or any(c in '<>:"/\\|?*' for c in name)
        or name.rstrip(" .") != name or Path(name).name != name
        or (suffix and not name.casefold().endswith(suffix))
    ):
        _fail("BATCH_INVALID_REPORT", "转换清单包含非法文件名。")
    return name


def _manifest(directory: Path) -> tuple[Path, str]:
    candidates = [(directory / name, kind) for name, kind in _REPORTS.items()
                  if (directory / name).exists()]
    if len(candidates) != 1:
        _fail("BATCH_MANIFEST_REQUIRED", "必须选中一个有唯一 GDB/MXD 转换清单的结果文件夹。")
    report, kind = candidates[0]
    _local(report)
    if report.stat().st_size > 8 * 1024 * 1024:
        _fail("BATCH_INVALID_REPORT", "转换清单过大，请重新导出。")
    return report, kind


def load_conversion_batch(directory: Path | str) -> dict:
    """Enumerate exactly the successful SHPs named by a local conversion report."""
    folder = _local(Path(directory), is_dir=True)
    manifest, kind = _manifest(folder)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError()
        rows = data["layers"] if kind == "gdb" else data["items"]
        if not isinstance(rows, list) or not (1 <= len(rows) <= 1000):
            raise ValueError()
        if kind == "gdb":
            if data.get("operation") != "GDB_TO_SHP_LOCAL_ONLY" or data.get("source_unchanged") is not True:
                raise ValueError()
            if Path(data["destination"]).resolve() != folder:
                raise ValueError()
            source_name = _filename(data["source_display_name"], ".gdb")
        else:
            if data.get("converter") != "ArcMap-10x-local" or data.get("uploaded") is not False:
                raise ValueError()
            source_name = _filename(data["source_filename"], ".mxd")
    except (KeyError, TypeError, ValueError, OSError, UnicodeError) as exc:
        raise DatasetError("BATCH_INVALID_REPORT", "转换清单内容无效，不能推断整批图幅。") from exc

    batch_id = "src_" + hashlib.sha256(str(folder).casefold().encode("utf-8")).hexdigest()[:16]
    report_sha = digest_file(manifest)
    selected, excluded, seen = [], [], set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            _fail("BATCH_INVALID_REPORT", "转换清单包含非法图层记录。")
        name = _label(row.get("source_layer"))
        status = row.get("status")
        if status not in _GOOD[kind]:
            excluded.append({"source_layer": name, "status": str(status or "UNKNOWN")[:80]})
            continue
        if kind == "gdb":
            base = _filename(row.get("output_basename"))
            shp_name = _filename(base + ".shp", ".shp")
            files = row.get("output_files")
            if not isinstance(files, list) or len(files) != len(_COMPONENTS):
                _fail("BATCH_INVALID_REPORT", "GDB 成功图层缺少组件校验记录。")
            suffixes = set()
            for record in files:
                if not isinstance(record, dict):
                    _fail("BATCH_INVALID_REPORT", "GDB 文件校验记录无效。")
                component = _filename(record.get("name"))
                file = _local(folder / component)
                sha = record.get("sha256")
                suffix = file.suffix.casefold()
                if (
                    file.stem.casefold() != base.casefold() or suffix not in _COMPONENTS
                    or suffix in suffixes or not isinstance(sha, str) or len(sha) != 64
                    or any(c not in "0123456789abcdef" for c in sha)
                ):
                    _fail("BATCH_INVALID_REPORT", "GDB 导出文件组件与记录不一致。")
                suffixes.add(suffix)
                if digest_file(file) != sha:
                    _fail("BATCH_OUTPUT_CHANGED", "GDB 已导出的文件被修改，不能直接批量发布。")
            if suffixes != _COMPONENTS:
                _fail("BATCH_INVALID_REPORT", "GDB 成功图层缺少 SHP 必备组件。")
        else:
            shp_name = _filename(row.get("shp"), ".shp")
        shp = _local(folder / shp_name)
        if shp.name.casefold() in seen:
            _fail("BATCH_DUPLICATE_LAYER", "一个转换批次中存在重复 SHP 名称。")
        seen.add(shp.name.casefold())
        selected.append({
            "batch_id": batch_id,
            "source_format": kind, "source_name": source_name,
            "source_layer": name, "source_frame": _label(row["data_frame"]) if kind == "mxd" else "",
            "shp_path": str(shp), "manifest": str(manifest),
            "manifest_sha256": report_sha,
            "conversion_status": status,
        })
    return {"source_format": kind, "source_name": source_name,
            "manifest": str(manifest), "layers": selected, "excluded": excluded}


def conversion_origins(paths: list[Path]) -> dict[str, dict]:
    """Inspect manifests beside selected SHPs without expanding the selection."""
    found: dict[str, dict] = {}
    cache: dict[str, dict] = {}
    for path in paths:
        path = Path(path).resolve()
        if path.suffix.casefold() != ".shp":
            continue
        folder = path.parent
        if not any((folder / name).exists() for name in _REPORTS):
            continue
        if str(folder) not in cache:
            cache[str(folder)] = load_conversion_batch(folder)
        batch = cache[str(folder)]
        entry = next((r for r in batch["layers"] if r["shp_path"] == str(path)), None)
        if entry is None:
            _fail("BATCH_UNRECORDED_SHP", "文件夹内含未记载为成功成果的 SHP，拒绝混入整批发布。")
        found[str(path)] = {k: entry[k] for k in entry if k != "shp_path"}
    return found


def verify_conversion_origins(resources: list[dict]) -> None:
    """Revalidate report membership/hash before creating any remote objects."""
    origins = {
        resource["inspection"]["source"]: resource["conversion_origin"]
        for resource in resources if "conversion_origin" in resource
    }
    if not origins:
        return
    current = conversion_origins([Path(path) for path in origins])
    if current != origins:
        _fail("BATCH_LINEAGE_CHANGED", "转换清单或源图幅归属已改变，必须重新预检生成发布任务。")
