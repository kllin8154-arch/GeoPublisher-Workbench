"""Offline human-reviewed logical map-sheet grouping for GDB/MXD batches.

This is *not* automatic geometry segmentation. A feature class may span
many sheets, and a single sheet may contain many GeoServer layers.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .config import ConfigError
from .plans import digest_file, load_plan

_SHEET_FIELDS = {
    "sheetid", "sheetno", "sheetnumber", "mapno", "mapnumber",
    "mapid", "tfbh", "tfh", "图幅号", "图幅编号", "图幅代码",
    "图号", "幅号", "分幅编号", "图幅编码",
}
_NOTE = "图幅编号须核对实际数据范围；候选字段不等于真实图幅，单层多幅须先拆分。"
_FIXED_KEYS = ("resource_id", "target_name", "source_filename", "source_layer", "group_code",
               "suggested_fields")

def _deny(message: str) -> None:
    raise ConfigError("SHEET_REVIEW_INVALID", message)


def _checked_file(path: Path | str, *, name: str | None = None) -> Path:
    if str(path).startswith(("\\\\", "//")):
        _deny("图幅关系只能读取本地文件，不能使用共享盘。")
    source = Path(path).expanduser().absolute()
    if (
        (name is not None and source.name != name) or
        source.is_symlink() or any(part.is_symlink() for part in source.parents)
        or not source.is_file() or source.stat().st_size > (8 if name == "plan.json" else 2) * 1024 * 1024
    ):
        _deny("图幅关系文件不存在、不符合大小限制或包含符号链接。")
    return source


def _suggest(fields: object) -> list[str]:
    names = []
    if isinstance(fields, list):
        for field in fields:
            name = field.get("name") if isinstance(field, dict) else field
            if isinstance(name, str) and len(name) <= 80:
                normalized = re.sub(r"[\s_\-]+", "", name).casefold()
                if normalized in _SHEET_FIELDS and name not in names:
                    names.append(name)
    return names


def sheet_review_template(plan_file: Path | str) -> dict:
    plan_path = _checked_file(plan_file, name="plan.json")
    plan = load_plan(plan_path)
    if Path(plan["output_dir"]).resolve() != plan_path.parent.resolve():
        _deny("审核任务与原始 plan.json 目录不一致。")
    entries = []
    for resource in plan["resources"]:
        batch = resource.get("batch_source")
        if not batch:
            continue
        if resource["kind"] != "shapefile":
            _deny("图幅审核只接受 GDB/MXD 转换后的 SHP 图层。")
        entries.append({
            "resource_id": resource["resource_id"],
            "target_name": resource["target_name"],
            "source_filename": batch["source_filename"],
            "source_layer": batch["source_layer"],
            "group_code": batch["group_code"],
            "suggested_fields": _suggest(resource["inspection"].get("fields", [])),
            "status": "PENDING",
            "sheet_code": "",
            "sheet_title": "",
        })
    if not entries:
        _deny("当前计划不包含有本地转换来源证明的 GDB/MXD 图层。")
    return {
        "schema_version": 1,
        "task_id": plan["task_id"],
        "plan_sha256": digest_file(plan_path),
        "note": _NOTE,
        "entries": entries,
    }


def validate_sheet_review(
    plan_file: Path | str, review_file: Path | str, *, require_complete: bool = True,
) -> dict:
    expected = sheet_review_template(plan_file)
    source = _checked_file(review_file)
    try:
        review = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(review, dict) or set(review) != set(expected):
            raise ValueError()
        if any(review[k] != expected[k] for k in ("schema_version", "task_id", "plan_sha256", "note")):
            raise ValueError()
        rows = review["entries"]
        if not isinstance(rows, list) or len(rows) != len(expected["entries"]):
            raise ValueError()
        sheets: dict[tuple[str, str], str] = {}
        for item, original in zip(rows, expected["entries"], strict=True):
            if not isinstance(item, dict) or set(item) != set(original):
                raise ValueError()
            if any(item[k] != original[k] for k in _FIXED_KEYS):
                raise ValueError()
            if item["status"] not in {"PENDING", "NEEDS_SPLIT", "CONFIRMED_SINGLE"}:
                raise ValueError()
            code, title = item["sheet_code"], item["sheet_title"]
            if item["status"] == "CONFIRMED_SINGLE":
                if (not isinstance(code, str) or not 1 <= len(code) <= 80
                        or not re.fullmatch(r"[\w.:-]+", code, re.UNICODE)
                        or not isinstance(title, str) or not 1 <= len(title.strip()) <= 130
                        or any(ord(c) < 32 for c in title)):
                    raise ValueError()
                identity = item["group_code"], code
                if identity in sheets and sheets[identity] != title:
                    raise ValueError()
                sheets[identity] = title
            elif code != "" or title != "":
                raise ValueError()
        if require_complete and any(row["status"] != "CONFIRMED_SINGLE" for row in rows):
            _deny("存在待核对图层或需要按字段拆分的图层；不能以图层数量冒充真实图幅。")
        if len(sheets) > 185:
            _deny("本任务确认的图幅分组超过 185 个，请拆成多个任务。")
    except (UnicodeError, OSError, ValueError, TypeError, KeyError) as exc:
        raise ConfigError(
            "SHEET_REVIEW_INVALID",
            "图幅审核清单与不可变计划不一致，或编号/状态有冲突；不会自动创建图幅分组。",
        ) from exc
    return {
        "entries": rows,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "sheet_count": len(sheets),
        "pending": sum(row["status"] == "PENDING" for row in rows),
        "needs_split": sum(row["status"] == "NEEDS_SPLIT" for row in rows),
    }


def write_sheet_review(
    plan_file: Path | str, output: Path | str, *, entries: list[dict] | None = None,
) -> dict:
    """Create a new local review file, never overwrite or self-approve entries."""
    template = sheet_review_template(plan_file)
    if entries is not None:
        template["entries"] = entries
    destination = Path(output).expanduser().absolute()
    if (str(output).startswith(("\\\\", "//")) or destination.exists()
            or destination.is_symlink() or any(p.is_symlink() for p in destination.parents)
            or not destination.parent.is_dir()):
        _deny("输出必须为已有本机目录中的全新 JSON 文件，不允许覆盖或共享盘。")
    # Validate all immutable identity fields and user annotations *before* write.
    # Actual review status is still manual; default PENDING never implies approval.
    if entries is not None:
        original = sheet_review_template(plan_file)["entries"]
        if len(entries) != len(original):
            _deny("图幅数目与计划不一致。")
        for row, expected in zip(entries, original, strict=True):
            if not isinstance(row, dict) or set(row) != set(expected):
                _deny("图幅审阅条目字段不匹配。")
            if any(row[k] != expected[k] for k in _FIXED_KEYS):
                _deny("不能修改来源文件、原图层、候选字段等不可变信息。")
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(template, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        checked = validate_sheet_review(plan_file, destination, require_complete=False)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {
        "file": str(destination),
        "sheet_count": checked["sheet_count"],
        "pending": checked["pending"],
        "needs_split": checked["needs_split"],
        "sha256": checked["sha256"],
    }


def sheet_group_code(batch_group: str, sheet_code: str) -> str:
    return "draft_sheet_" + hashlib.sha256(
        (batch_group + "\0" + sheet_code).encode("utf-8")
    ).hexdigest()[:16]
