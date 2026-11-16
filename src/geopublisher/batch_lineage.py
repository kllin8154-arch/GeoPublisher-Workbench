"""Freeze GDB/MXD conversion lineage so one source becomes one catalog parent.

The report stays on the local machine. Its source file is included in the
immutable plan's existing source-hash checks; no GeoServer or DB access.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import ConfigError


def conversion_batch(source: Path, *, source_sha256: str) -> dict | None:
    """Resolve exactly one successful conversion row for a generated SHP."""
    if source.suffix.casefold() != ".shp":
        return None
    options = (
        ("conversion-report.json", "gdb", "GDB"),
        ("mxd-export-manifest.json", "mxd", "MXD"),
        ("sheet-split-manifest.json", "split", "SPLIT"),
    )
    present = [(source.parent / file, kind, format_name)
               for file, kind, format_name in options
               if (source.parent / file).exists() or (source.parent / file).is_symlink()]
    if not present:
        return None
    if len(present) != 1:
        raise ConfigError("BATCH_MANIFEST_CONFLICT", "同一批导出目录存在多个格式来源报告，请分开导入。")
    marker, kind, format_name = present[0]
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 8 * 1024 * 1024:
        raise ConfigError("BATCH_MANIFEST_INVALID", "批量转换报告不是安全的普通本地文件。")
    try:
        raw = marker.read_bytes()
        report = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(report, dict):
            raise ValueError()
        if kind == "split":
            if (
                report.get("operation") != "SHP_SHEET_SPLIT_LOCAL_ONLY"
                or report.get("status") != "COMPLETE_VERIFIED"
                or report.get("destination") != str(source.parent)
            ):
                raise ValueError()
            rows = report["groups"]
            matches = [row for row in rows if isinstance(row, dict) and row.get("shp") == source.name]
            if len(matches) != 1 or matches[0].get("status") != "EXPORTED_VERIFIED":
                raise ValueError()
            files = matches[0]["output_files"]
            if not isinstance(files, list) or not any(
                isinstance(item, dict) and item.get("name") == source.name
                and item.get("sha256") == source_sha256 for item in files
            ):
                raise ValueError()
            from .sheet_split import _hashes, _safe_path

            original = _safe_path(report["source"], file=True)
            if (
                original.parent == source.parent
                or (original.parent / "sheet-split-manifest.json").exists()
                or _hashes(original) != report["source_hashes"]
                or report["source_sha256"] != report["source_hashes"][0]["sha256"]
            ):
                raise ValueError()
            original_batch = conversion_batch(original, source_sha256=report["source_sha256"])
            if original_batch != report.get("source_batch"):
                raise ValueError()
            # Standalone SHP splits can publish but do not claim a GDB/MXD parent.
            if original_batch is None:
                return None
            code = matches[0]["sheet_code"]
            if not isinstance(code, str) or not 1 <= len(code) <= 64:
                raise ValueError()
            return {
                "format": original_batch["format"],
                "source_filename": original_batch["source_filename"],
                "source_layer": original_batch["source_layer"][:185] + " / 图幅 " + code,
                "group_code": original_batch["group_code"],
                "manifest_path": str(marker),
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            }
        if kind == "gdb":
            if (report.get("operation") != "GDB_TO_SHP_LOCAL_ONLY"
                    or report.get("destination") != str(source.parent)):
                raise ValueError()
            filename = report["source_display_name"]
            rows = report["layers"]
            matches = [row for row in rows
                       if isinstance(row, dict) and row.get("output_basename") == source.stem]
            if len(matches) != 1 or matches[0].get("status") not in {
                "EXPORTED", "EXPORTED_REVIEW_REQUIRED",
            }:
                raise ValueError()
            files = matches[0]["output_files"]
            if not isinstance(files, list) or not any(
                isinstance(item, dict) and item.get("name") == source.name
                and item.get("sha256") == source_sha256 for item in files
            ):
                raise ValueError()
            layer_name = matches[0]["source_layer"]
        else:
            if (report.get("source_format") != "MXD"
                    or report.get("converter") != "ArcMap-10x-local"
                    or report.get("files_relative_to_output") is not True):
                raise ValueError()
            filename = report["source_filename"]
            rows = report["items"]
            matches = [row for row in rows
                       if isinstance(row, dict) and row.get("shp") == source.name]
            if (len(matches) != 1 or matches[0].get("status") != "EXPORTED"
                    or matches[0].get("lyr") != source.stem + ".lyr"
                    or not (source.parent / matches[0]["lyr"]).is_file()):
                raise ValueError()
            layer_name = matches[0]["source_layer"]
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError()
        if (not isinstance(filename, str) or not 1 <= len(filename) <= 128
                or not filename.casefold().endswith("." + kind)
                or any(char in filename for char in ("/", "\\"))
                or any(ord(char) < 32 for char in filename)):
            raise ValueError()
        if (not isinstance(layer_name, str) or not 1 <= len(layer_name) <= 256
                or any(ord(char) < 32 for char in layer_name)):
            raise ValueError()
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise ConfigError(
            "BATCH_MANIFEST_INVALID",
            "此 SHP 与本地 GDB/MXD 转换清单不一致；请核对导出结果，禁止默默跳过其他图幅。",
        ) from exc
    return {
        "format": format_name,
        "source_filename": filename,
        "source_layer": layer_name,
        "group_code": "draft_batch_" + hashlib.sha256(str(marker).encode("utf-8")).hexdigest()[:16],
        "manifest_path": str(marker),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }


def verified_batch_sources(directory: Path | str) -> list[Path]:
    """Select only complete, successful SHPs named in a local GDB/MXD report.

    Used by the desktop import step. It never uploads data or trusts arbitrary
    SHP files left by a failed conversion. The publication plan re-verifies
    every selected file and freezes the report hash.
    """
    root = Path(directory).expanduser().absolute()
    if (str(root).startswith(("\\\\", "//")) or root.is_symlink()
            or any(parent.is_symlink() for parent in root.parents) or not root.is_dir()):
        raise ConfigError("BATCH_DIRECTORY_INVALID", "请选择本机已有的批量转换结果文件夹。")
    reports = [(root / name, kind) for name, kind in (
        ("conversion-report.json", "gdb"), ("mxd-export-manifest.json", "mxd"),
        ("sheet-split-manifest.json", "split"),
    ) if (root / name).exists() or (root / name).is_symlink()]
    if len(reports) != 1:
        raise ConfigError("BATCH_MANIFEST_INVALID", "转换文件夹必须包含一份有效的 GDB 或 MXD 清单。")
    marker, kind = reports[0]
    if kind == "split":
        from .sheet_split import verified_split_sources

        return verified_split_sources(root)
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 8 * 1024 * 1024:
        raise ConfigError("BATCH_MANIFEST_INVALID", "转换清单无效或过大。")
    try:
        report = json.loads(marker.read_text(encoding="utf-8-sig"))
        rows = report["layers"] if kind == "gdb" else report["items"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise ValueError()
        sources, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError()
            status = row.get("status")
            success = (status in {"EXPORTED", "EXPORTED_REVIEW_REQUIRED"}
                       if kind == "gdb" else status == "EXPORTED")
            if not success:
                continue
            name = ((row["output_basename"] + ".shp") if kind == "gdb" else row["shp"])
            if (not isinstance(name, str) or not 1 <= len(name) <= 180
                    or not name.casefold().endswith(".shp")
                    or any(c in '<>:"/\\|?*' for c in name)
                    or any(ord(c) < 32 for c in name)
                    or name.rstrip(" .") != name or name.casefold() in seen):
                raise ValueError()
            shp = root / name
            if shp.is_symlink() or not shp.is_file():
                raise ValueError()
            sha = hashlib.sha256()
            with shp.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    sha.update(block)
            if conversion_batch(shp, source_sha256=sha.hexdigest()) is None:
                raise ValueError()
            sources.append(shp)
            seen.add(name.casefold())
        return sources
    except (KeyError, TypeError, ValueError, OSError, UnicodeError) as exc:
        raise ConfigError(
            "BATCH_MANIFEST_INVALID",
            "成功图层与导出清单不一致，请先检查本地转换报告，未加入任何图层。",
        ) from exc
