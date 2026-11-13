"""Prepare an offline *unreviewed* business catalog draft from an immutable publish plan.

Do not guess a thematic parent or scale from a filename. Publishing reports are
only prefill hints: a machine-valid WMS preview is not visual acceptance, and
schema, lineage, parentage and business service identities require human review.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .catalog import CatalogError
from .plans import load_plan

PENDING_GROUP = "draft_unclassified"


def _missing(message: str) -> None:
    raise CatalogError("INVALID_CATALOG_DRAFT", message)


def _default_template(workspace: str) -> dict:
    # Synthetic column names, never an assertion about a production database.
    return {
        "schema_version": 1,
        "target_schema": "review_schema",
        "schema_verified": False,
        "tables": {"service": "review_services", "node": "review_nodes"},
        "columns": {
            "service": {
                "key": "name", "title": "namec", "kind": "maptype",
                "platform": "platform", "url": "url", "layer": "layer",
                "crs": "crs", "format": "format", "memo": "memo",
                "organization": "ssdw",
            },
            "node": {
                "sys": "sys", "key": "code", "title": "namec", "parent": "glcode",
                "kind": "type", "display_order": "disporder", "visible": "visible",
                "zoom_start": "zooms", "zoom_end": "zoome",
                "service_reference": "svr_name", "layer_index": "lay_idx",
                "memo": "memo", "organization": "ssdw",
            },
        },
        "defaults": {
            "sys": "0", "wms_url": f"geoserver/{workspace}/wms",
            "workspace": workspace, "platform": "geoserver", "organization": 0,
        },
        "existing_parents": [],
        "groups": [],
        "layers": [],
    }


def _local_report(plan: dict, report_path: Path | None) -> tuple[dict[str, dict], str]:
    """Read only a matching local report; never treat report text as authorization."""
    if report_path is None or not report_path.is_file():
        return {}, "MISSING"
    if str(report_path).startswith(("\\\\", "//")):
        _missing("报告不能位于 UNC 或远程共享路径。")
    root = Path(plan["output_dir"]).resolve()
    if report_path.is_symlink() or not report_path.resolve().is_relative_to(root):
        _missing("报告必须是同一发布任务目录中的普通本地文件。")
    if report_path.stat().st_size > 8 * 1024 * 1024:
        _missing("任务报告异常大，不予读取。")
    try:
        content = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        _missing("任务报告不能解析，不能推断远端状态。")
    if not isinstance(content, dict) or (
        content.get("task_id") != plan["task_id"]
        or content.get("workspace") != plan["environment"]["workspace"]
        or Path(str(content.get("output_dir", ""))).resolve() != root
    ):
        _missing("报告与已校验的发布计划不一致。")
    resources = content.get("resources")
    if not isinstance(resources, list) or len(resources) > 1000:
        _missing("报告图层清单缺失或数量异常。")
    matched: dict[str, dict] = {}
    for record in resources:
        if not isinstance(record, dict) or not isinstance(record.get("resource_id"), str):
            _missing("报告包含无效图层条目。")
        key = record["resource_id"]
        if key in matched:
            _missing("报告包含重复图层 ID。")
        matched[key] = record
    return matched, "PRESENT_MATCHING_TASK"


def _source_preview(plan: dict, recorded: dict, resource: dict) -> str | None:
    if recorded.get("status") != "SUCCEEDED":
        return None
    layer = plan["environment"]["workspace"] + ":" + resource["target_name"]
    if recorded.get("qualified_name") != layer or recorded.get("target_name") != resource["target_name"]:
        return None
    wms = recorded.get("wms")
    evidence = wms.get("evidence") if isinstance(wms, dict) else None
    if not isinstance(evidence, dict) or evidence.get("status") != "PASSED":
        return None
    preview = recorded.get("preview_path")
    if not isinstance(preview, str) or not preview or len(preview) > 4096:
        return None
    path = Path(preview)
    root = Path(plan["output_dir"]).resolve()
    if (
        not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path.suffix.lower() != ".png"
        or path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
        or not path.resolve().is_relative_to(root) or not path.is_file()
    ):
        return None
    return str(path)


def generate_catalog_draft(
    plan_file: Path | str,
    output: Path | str,
    *,
    template_file: Path | str | None = None,
    report_file: Path | str | None = None,
) -> dict:
    """Create a local editable tree profile and review report, but never SQL writes."""
    if str(plan_file).startswith(("\\\\", "//")):
        _missing("发布计划必须位于本地磁盘，不能使用 UNC 共享路径。")
    plan_path = Path(plan_file).expanduser().absolute()
    if (
        str(plan_path).startswith(("\\\\", "//"))
        or not plan_path.is_file() or plan_path.name != "plan.json"
        or plan_path.is_symlink() or any(parent.is_symlink() for parent in plan_path.parents)
    ):
        _missing("请选择原始任务目录中的 plan.json，不允许符号链接或其他文件。")
    plan = load_plan(plan_path)  # Checks the stored fingerprint and basic plan schema.
    if Path(plan["output_dir"]).resolve() != plan_path.parent.resolve():
        _missing("plan.json 不在其声明的任务目录中。")
    if len(plan["resources"]) > 1000:
        _missing("一次最多生成 1000 个待分类图层。")
    workspace = plan["environment"]["workspace"]
    if not isinstance(workspace, str) or not workspace.isascii() or not workspace.replace("_", "").isalnum():
        _missing("工作空间名称不能安全写入相对 WMS 模板路径。")

    template = _default_template(workspace)
    if template_file is not None:
        if str(template_file).startswith(("\\\\", "//")):
            _missing("业务表映射模板不能位于 UNC 共享路径。")
        path = Path(template_file).expanduser().absolute()
        if str(path).startswith(("\\\\", "//")) or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            _missing("业务表映射模板不得使用 UNC 或符号链接。")
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            _missing("业务表映射模板不存在或过大。")
        try:
            supplied = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(supplied, dict):
                raise ValueError()
            for item in ("target_schema", "tables", "columns", "defaults"):
                if item not in supplied:
                    raise ValueError(item)
            for item in ("target_schema", "tables", "columns"):
                template[item] = copy.deepcopy(supplied[item])
            # Use reviewed schema names as prefill only; intentionally clear verification.
            template["defaults"].update({
                key: copy.deepcopy(supplied["defaults"][key])
                for key in ("sys", "platform", "organization")
                if key in supplied["defaults"]
            })
        except (OSError, ValueError, KeyError, TypeError):
            _missing("业务字段模板必须包含合法的目标模式、表、列及默认配置。")
    template["schema_verified"] = False
    template["defaults"]["workspace"] = workspace
    template["defaults"]["wms_url"] = f"geoserver/{workspace}/wms"

    if report_file is not None and str(report_file).startswith(("\\\\", "//")):
        _missing("任务报告不能位于 UNC 共享路径。")
    report_path = Path(report_file).expanduser().absolute() if report_file else plan_path.parent / "report.json"
    reported, report_status = _local_report(plan, report_path)
    template["groups"] = [
        {
            "code": PENDING_GROUP,
            "title": "待分类：请移至真实专题及比例尺目录并删除本分组",
            "parent": None, "display_order": 0, "layer_index": 0,
            "classification_reviewed": False,
        }
    ]
    review: list[dict[str, Any]] = []
    seen: set[str] = set()
    batches: dict[str, dict] = {}
    for resource in plan["resources"]:
        origin = resource.get("conversion_origin")
        if not origin:
            continue
        code = origin["batch_id"]
        prior = batches.get(code)
        if prior and (prior["source_name"], prior["source_format"], prior["manifest"]) != (
            origin["source_name"], origin["source_format"], origin["manifest"]
        ):
            _missing("同一批次的 GDB/MXD 来源信息不一致。")
        batches[code] = origin
    if len(batches) > 199:
        _missing("单次最多整理 199 个 GDB/MXD 来源文件的父节点，请拆分批次。")
    for code, origin in sorted(batches.items()):
        template["groups"].append({
            "code": code, "title": origin["source_name"], "parent": PENDING_GROUP,
            "display_order": len(template["groups"]) * 10, "layer_index": len(template["groups"]),
            "classification_reviewed": False,
        })
    for i, resource in enumerate(plan["resources"], start=1):
        code = resource["target_name"]
        if code == PENDING_GROUP or code in batches or code in seen:
            _missing("多个发布图层名称重复或与保留的未分类目录名称冲突。")
        seen.add(code)
        kind = resource["kind"]
        if kind not in {"shapefile", "geotiff"}:
            _missing("当前发布计划不是受支持的 SHP/单 GeoTIFF 资源。")
        recorded = reported.get(resource["resource_id"], {})
        preview = _source_preview(plan, recorded, resource)
        origin = resource.get("conversion_origin")
        wms = {"status": "PENDING", "visual_reviewed": False}
        if preview:
            wms["png"] = preview
        template["layers"].append({
            "code": code, "title": code,
            "parent": origin["batch_id"] if origin else PENDING_GROUP,
            "service_key": code,
            "qualified_name": f"{workspace}:{code}",
            "source_format": origin["source_format"] if origin else (
                "shp" if kind == "shapefile" else "geotiff"
            ),
            "publish_kind": kind, "crs": resource["inspection"]["crs"],
            "display_order": i * 10, "layer_index": i,
            "zoom_start": 1, "zoom_end": 29,
            "classification_reviewed": False,
            **({"conversion_reviewed": False} if origin else {}),
            "wms": wms,
        })
        review.append({
            "resource_id": resource["resource_id"],
            "layer": f"{workspace}:{code}",
            "kind": kind,
            "recorded_status": recorded.get("status", "NO_REPORT"),
            "preview_prefilled": preview is not None,
            "source_file": origin["source_name"] if origin else None,
            "source_layer": origin["source_layer"] if origin else None,
            "parent_node_code": origin["batch_id"] if origin else PENDING_GROUP,
            "required_manual_reviews": [
                "专题、比例尺、父节点、业务服务名称",
                "源数据是否来自 MXD/GDB，转换报告与 SLD 样式",
                "实际 EPSG、WMS PNG 真实性及人工视觉验收",
                "业务表字段、主键序列与现有节点重名情况",
            ],
        })
    if str(output).startswith(("\\\\", "//")):
        _missing("不能把目录审阅包写入 UNC 或远程共享路径。")
    output_path = Path(output).expanduser().absolute()
    if (
        str(output_path).startswith(("\\\\", "//"))
        or output_path.is_symlink() or any(parent.is_symlink() for parent in output_path.parents)
        or output_path.exists() or output_path.is_relative_to(plan_path.parent)
        or output_path == plan_path.parent
    ):
        _missing("输出必须是任务数据目录之外的全新本机目录。")
    output_path.mkdir(parents=True, exist_ok=False)
    file = output_path / "business_tree_draft.json"
    file.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    overview = {
        "status": "DRAFT_NOT_REVIEWED",
        "registered": False,
        "database_accessed": False,
        "geoserver_accessed": False,
        "original_task_id": plan["task_id"],
        "matching_report": report_status,
        "layer_count": len(review),
        "source_parent_count": len(batches),
        "layers": review,
        "notes": [
            "自动提取的是发布计划中的 SHP/GeoTIFF 图层，不代表已从服务器确认发布成功。",
            "通过本机转换清单核验的 MXD/GDB 图幅已按源文件生成待审核父节点；普通 SHP 来源不自动猜测。",
            "父子目录最终登记到现场业务数据库的节点表；本地 JSON 只是待审核 SQL 输入，不代表已入库。",
            "需将文件父节点接入真实专题/比例尺父目录，删除 draft_unclassified，并核实字段与 WMS。",
            "schema_verified=false、classification_reviewed=false、wms=PENDING，不能直接登记数据库。",
        ],
    }
    (output_path / "draft_review.json").write_text(
        json.dumps(overview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"status": overview["status"], "registered": False,
            "output": str(output_path), "draft": str(file),
            "layer_count": len(review), "source_parent_count": len(batches),
            "report": str(output_path / "draft_review.json")}
