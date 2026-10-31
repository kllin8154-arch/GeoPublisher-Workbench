"""Sequential, evidence-driven publishing with explicit recovery boundaries."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable

from .config import ConfigError, Environment
from .geoserver import GeoServerClient, GeoServerError
from .journal import Journal
from .plans import fingerprint, save_json, validate_plan, verify_artifacts
from .verification import verify_image


class RecoveryRequired(RuntimeError):
    code = "MANUAL_ACTION_REQUIRED"


class Cancelled(RuntimeError):
    code = "CANCELLED"


def _safe_error(exc: Exception) -> tuple[str, str]:
    code = getattr(exc, "code", "UNEXPECTED_ERROR")
    if isinstance(exc, (GeoServerError, ConfigError, RecoveryRequired, Cancelled)):
        return code, str(exc)
    return code, "任务遇到本地错误，执行已停止；请查看阶段记录，确认后恢复。"


def _snapshot(value: dict) -> dict:
    """Exclude transport links from object comparison; keep all semantic fields."""

    def clean(item):
        if isinstance(item, dict):
            return {key: clean(value) for key, value in item.items() if key != "href"}
        if isinstance(item, list):
            return [clean(value) for value in item]
        return item

    result = clean(value)
    # GeoServer 2.24.4 may materialize this default after the first OWS request.
    # Normalize only the known semantic default, never suppress arbitrary changes.
    if "nativeName" in result and "store" in result:
        result.setdefault("advertised", True)
    if "resource" in result and "type" in result:
        result.setdefault("queryable", True)
        result.setdefault("opaque", False)
    return result


def _bbox(bounds: list[float]) -> list[float]:
    x0, y0, x1, y1 = map(float, bounds)
    dx, dy = max((x1 - x0) * 0.03, 0.0001), max((y1 - y0) * 0.03, 0.0001)
    return [max(-180.0, x0 - dx), max(-90.0, y0 - dy), min(180.0, x1 + dx), min(90.0, y1 + dy)]


class Publisher:
    def __init__(self, plan: dict, journal: Journal, client, on_event=None, cancel_event=None):
        self.plan, self.journal, self.client = plan, journal, client
        self.task_id = plan["task_id"]
        self.workspace = plan["environment"]["workspace"]
        self.on_event, self.cancel_event = on_event, cancel_event

    def event(self, code: str, message: str, resource_id=None, level="INFO"):
        self.journal.add_event(self.task_id, level, code, message, resource_id=resource_id)
        if self.on_event:
            self.on_event({"code": code, "text": message, "resource_id": resource_id, "level": level})

    def checkpoint(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled("已在安全检查点取消；已完成的服务保留，可再次打开任务恢复。")

    def current(self, resource: dict, kind: str):
        workspace, store, layer = self.workspace, resource["store_name"], resource["target_name"]
        if kind == "store":
            return self.client.get_store(workspace, store, resource["kind"])
        if kind == "resource":
            return self.client.get_resource(workspace, store, layer, resource["kind"])
        if kind == "layer":
            return self.client.get_layer(workspace, layer)
        if kind == "style":
            style = self.client.get_style(workspace, resource["style_name"])
            if style is not None:
                import hashlib

                style = dict(style)
                style["content_sha256"] = hashlib.sha256(
                    self.client.get_style_content(workspace, resource["style_name"])
                ).hexdigest()
            return style
        raise ValueError("Unknown object type")

    def object_name(self, resource: dict, kind: str):
        return resource[{"store": "store_name", "style": "style_name"}.get(kind, "target_name")]

    def owned(self, resource: dict, kind: str):
        return next(
            (
                obj
                for obj in self.journal.objects(self.task_id, resource["resource_id"])
                if obj["kind"] == kind
            ),
            None,
        )

    def assert_owned(self, resource: dict, kind: str) -> dict:
        record, current = self.owned(resource, kind), self.current(resource, kind)
        expected = fingerprint(_snapshot(record["details"]["snapshot"])) if record else None
        if not record or current is None or fingerprint(_snapshot(current)) != expected:
            raise RecoveryRequired(
                f"{resource['target_name']} 的 {kind} 缺失或与任务记录不一致，已停止写入。"
            )
        return current

    def record_current(self, resource: dict, kind: str, *, refresh=False):
        current = self.current(resource, kind)
        if current is None:
            raise RecoveryRequired("写请求完成，但未能读取目标对象，需核对服务端结果。")
        snapshot = _snapshot(current)
        kwargs = {
            "fingerprint": fingerprint(snapshot),
            "details": {"snapshot": snapshot, "store": resource["store_name"]},
        }
        previous = self.owned(resource, kind)
        if previous and refresh:
            if kind == "layer":
                before = dict(previous["details"]["snapshot"])
                after = dict(snapshot)
                # GeoServer materializes these defaults when updating a layer.
                for item in (before, after):
                    item.pop("defaultStyle", None)
                    item.pop("dateModified", None)
                    item.setdefault("queryable", True)
                    item.setdefault("opaque", False)
                if before != after:
                    raise RecoveryRequired("绑定样式时发现其他图层属性发生变化，已停止自动认领和恢复。")
            self.journal.refresh_object(
                self.task_id,
                resource["resource_id"],
                kind,
                self.object_name(resource, kind),
                expected_fingerprint=previous["fingerprint"],
                **kwargs,
            )
        elif not previous:
            self.journal.record_object(
                self.task_id, resource["resource_id"], kind, self.object_name(resource, kind), **kwargs
            )
        elif previous["fingerprint"] != kwargs["fingerprint"]:
            raise RecoveryRequired("服务端对象已改变，不能将其自动认领为本任务对象。")

    def write(
        self,
        resource: dict,
        action: str,
        kinds: list[str],
        operation: Callable,
        *,
        expected=None,
        proof=None,
        refresh=False,
    ):
        """Every mutation is journalled before I/O; completed calls are never replayed."""
        self.checkpoint()
        rid = resource["resource_id"]
        previous = [op for op in self.journal.operations(self.task_id, rid) if op["action"] == action]
        if previous:
            last = previous[-1]
            if last["outcome"] in {"SUCCEEDED", "INTENT", "UNKNOWN"}:
                for kind in kinds:
                    self.assert_owned(resource, kind)
                if proof and not proof():
                    raise RecoveryRequired("服务端状态与本次操作的预期结果不一致，需人工核对。")
                if last["outcome"] != "SUCCEEDED":
                    self.journal.finish_operation(
                        last["operation_id"],
                        outcome="SUCCEEDED",
                        evidence={"reconciled_from_owned_snapshot": True},
                    )
                return
        self.journal.set_resource(self.task_id, rid, status="RUNNING", stage=action)
        self.event(action, f"{resource['target_name']}：{action}", rid)
        op_id = self.journal.begin_operation(
            self.task_id,
            rid,
            action,
            {
                "workspace": self.workspace,
                "layer": resource["target_name"],
                "store": resource["store_name"],
                "kinds": kinds,
            },
            expected,
        )
        try:
            evidence = operation()
            for kind in kinds:
                self.record_current(resource, kind, refresh=refresh)
            if proof and not proof():
                raise RecoveryRequired("操作后的目标状态未满足验证要求。")
        except BaseException as exc:
            # A successful HTTP request followed by a failed evidence read is also UNKNOWN.
            known_rejection = isinstance(exc, GeoServerError) and not exc.outcome_unknown
            self.journal.finish_operation(
                op_id,
                outcome="FAILED" if known_rejection else "UNKNOWN",
                evidence={"code": getattr(exc, "code", "INTERRUPTED")},
            )
            raise
        self.journal.finish_operation(op_id, outcome="SUCCEEDED", evidence=evidence or {"verified": True})

    def publish(self, resource: dict) -> dict:
        self.checkpoint()
        rid, layer = resource["resource_id"], resource["target_name"]
        existing = self.client.get_layer(self.workspace, layer)
        owned_objects = self.journal.objects(self.task_id, rid)
        if existing is not None and not self.owned(resource, "layer"):
            if owned_objects or self.journal.operations(self.task_id, rid):
                raise RecoveryRequired("发现同名图层但缺少本任务的创建证据；禁止自动接管或删除。")
            if self.plan["strategy"] == "skip":
                result = {
                    "resource_id": rid,
                    "target_name": layer,
                    "status": "SKIPPED",
                    "message": "沿用已存在的图层；本次输入未发布，也未验证已有内容。",
                }
                self.journal.set_resource(
                    self.task_id, rid, status="SKIPPED", stage="CONFLICT_CHECK", details=result
                )
                return result
            raise ConfigError("RESOURCE_CONFLICT", f"图层 {layer} 已存在，请改名或选择跳过。")

        for kind in ("store", "style"):
            if self.current(resource, kind) is not None and not self.owned(resource, kind):
                raise RecoveryRequired(f"目标 {kind} 已存在且没有本任务所有权记录；需人工核对。")

        self.write(
            resource,
            "UPLOAD",
            ["store"],
            lambda: self.client.upload_dataset(
                self.workspace,
                resource["store_name"],
                resource["kind"],
                Path(resource["artifact"]["path"]),
                encoding=resource["inspection"].get("encoding"),
            ),
            expected={"artifact_sha256": resource["artifact"]["sha256"]},
        )
        self.assert_owned(resource, "store")
        self.write(
            resource,
            "PUBLISH",
            ["resource", "layer"],
            lambda: self.client.publish_resource(
                self.workspace,
                resource["store_name"],
                layer,
                resource["kind"],
                native_name=(
                    resource["artifact"].get("native_name") or resource["inspection"].get("native_name")
                )
                if resource["kind"] == "shapefile"
                else None,
                crs=resource["inspection"].get("crs"),
            ),
        )
        self.write(
            resource,
            "INSTALL_STYLE",
            ["style"],
            lambda: self.client.upload_style(
                self.workspace, resource["style_name"], Path(resource["style"]["path"])
            ),
            expected={"style_sha256": resource["style"]["sha256"]},
        )
        self.assert_owned(resource, "layer")
        self.assert_owned(resource, "style")

        def style_bound():
            current = self.client.get_layer(self.workspace, layer)
            name = (current or {}).get("defaultStyle", {}).get("name", "")
            return name in {resource["style_name"], self.workspace + ":" + resource["style_name"]}

        self.write(
            resource,
            "BIND_STYLE",
            ["layer"],
            lambda: self.client.bind_style(self.workspace, layer, resource["style_name"]),
            expected={"default_style": resource["style_name"]},
            proof=style_bound,
            refresh=True,
        )
        self.checkpoint()
        self.journal.set_resource(self.task_id, rid, status="RUNNING", stage="VERIFY_WMS")
        self.event("VERIFY_WMS", f"{layer}：验证实际 WMS 地图", rid)
        bbox = _bbox(resource["inspection"]["wgs84_bbox"])
        response = self.client.get_map(
            self.workspace, layer, bbox=bbox, crs="EPSG:4326", version="1.1.1", width=512, height=512
        )
        evidence = verify_image(
            response.content, response.headers.get("content-type", ""), expected_size=(512, 512)
        )
        preview = Path(self.plan["output_dir"]) / rid / "wms-preview.png"
        # verify_image rejects XML/error responses before any preview is saved.
        preview.write_bytes(response.content)
        status = "SUCCEEDED" if evidence["status"] == "PASSED" else "REVIEW_REQUIRED"
        wfs = None
        if self.plan.get("enable_wfs") and resource["kind"] == "shapefile":
            data = self.client.get_features(self.workspace, layer, limit=5)
            features = data.get("features", [])
            wfs = {
                "sample_count": len(features),
                "type": data.get("type"),
                "field_names": list(features[0].get("properties", {})) if features else [],
            }
            if not features and resource["inspection"].get("feature_count", 0):
                status = "REVIEW_REQUIRED"
        result = {
            "resource_id": rid,
            "target_name": layer,
            "qualified_name": self.workspace + ":" + layer,
            "status": status,
            "preview_path": str(preview),
            "wms": {
                "endpoint": self.plan["environment"]["base_url"] + "/wms",
                "bbox": bbox,
                "version": "1.1.1",
                "crs": "EPSG:4326",
                "evidence": evidence,
            },
            "wfs": wfs,
            "catalog_status": "NOT_REGISTERED",
        }
        self.journal.set_resource(self.task_id, rid, status=status, stage="VERIFIED", details=result)
        self.event(status, f"{layer}：{status}", rid)
        return result

    def report(self, status: str, results: list[dict]) -> dict:
        output = Path(self.plan["output_dir"])
        done = {r["resource_id"] for r in results}
        results = results + [
            {
                "resource_id": r["resource_id"],
                "target_name": r["target_name"],
                "status": "NOT_STARTED",
                "message": "任务已停止，此资源尚未执行。",
            }
            for r in self.plan["resources"]
            if r["resource_id"] not in done
        ]
        report_path = output / "report.md"
        report = {
            "task_id": self.task_id,
            "status": status,
            "environment_key": self.plan["environment"]["environment_key"],
            "workspace": self.workspace,
            "resources": results,
            "output_dir": str(output),
            "report_path": str(report_path),
            "catalog_status": "NOT_REGISTERED",
            "catalog_message": "仅导出图层绑定清单。配置经核验的目录字段映射后，可单独生成 SQL；尚未写入业务数据库。",
            "recovery": "恢复先核对任务所有权和对象指纹；未知结果或外部变化转人工处理。源数据与既有服务不做原位替换。",
        }
        save_json(output / "report.json", report)
        bindings = [
            {
                "qualified_name": r["qualified_name"],
                "wms_url": r["wms"]["endpoint"],
                "crs": r["wms"]["crs"],
                "title": r["target_name"],
                "task_id": self.task_id,
            }
            for r in results
            if r.get("status") == "SUCCEEDED"
        ]
        save_json(
            output / "catalog-bindings.json", {"schema_version": 1, "registered": False, "layers": bindings}
        )
        lines = [
            "# GeoPublisher 发布报告",
            "",
            f"任务：`{self.task_id}`",
            f"状态：**{status}**",
            f"环境：`{report['environment_key']}` / `{self.workspace}`",
            "",
            "| 图层 | 状态 | 说明 |",
            "|---|---|---|",
        ]
        for result in results:
            message = (
                str(result.get("message", "直接 WMS 验收；业务目录尚未登记"))
                .replace("|", "\\|")
                .replace("\n", " ")
            )
            lines.append(f"| {result['target_name']} | {result['status']} | {message} |")
        lines.extend(
            [
                "",
                "## 业务目录",
                "",
                report["catalog_message"],
                "",
                "## 恢复与清理",
                "",
                report["recovery"],
                "",
                "本报告不证明样式像素级一致、WMTS 缓存更新或业务页面已可见。",
            ]
        )
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report


def run_plan(
    plan: dict, journal_path=None, *, credentials=None, on_event=None, cancel_event=None, client=None
) -> dict:
    """Execute or resume a confirmed plan. A caller must obtain task-level consent."""
    plan = copy.deepcopy(plan)
    claimed_hash = plan.pop("plan_hash", None)
    if fingerprint(plan) != claimed_hash:
        raise ConfigError("PLAN_CHANGED", "执行计划已改变；请重新预检。")
    plan["plan_hash"] = claimed_hash
    validate_plan(plan)
    if not plan.get("ready"):
        raise ConfigError("PREFLIGHT_BLOCKED", "预检存在阻断问题，不能开始发布。")
    verify_artifacts(plan)
    environment = Environment(**plan["environment"])
    output = Path(plan["output_dir"])
    # A shared parent journal coordinates separate task directories on this machine.
    journal_path = Path(journal_path) if journal_path else output.parent / "geopublisher.sqlite"
    close_client = client is None
    if client is None:
        if credentials is None:
            client = environment.client()
        else:
            client = GeoServerClient(
                environment.base_url.rstrip("/"),
                credentials[0],
                credentials[1],
                timeout=environment.timeout,
                verify=environment.ca_bundle or True,
            )
    try:
        with Journal(journal_path) as journal:
            journal.create_task(plan["task_id"], plan)
            journal.recover_locks(plan["task_id"])
            publisher = Publisher(plan, journal, client, on_event, cancel_event)
            results = []
            started = False
            try:
                journal.acquire_lock(plan["task_id"], "task:" + plan["task_id"])
                for resource in plan["resources"]:
                    key = fingerprint(
                        [environment.base_url.rstrip("/"), environment.workspace, resource["target_name"]]
                    )
                    journal.acquire_lock(plan["task_id"], "layer:" + key)
                if journal.get_task(plan["task_id"])["status"] == "ROLLED_BACK":
                    raise ConfigError("TASK_ROLLED_BACK", "此任务已清理；重新发布必须生成新计划。")
                journal.set_task_status(plan["task_id"], "RUNNING")
                started = True
                client.probe()
                if client.get_workspace(environment.workspace) is None:
                    raise ConfigError(
                        "WORKSPACE_REQUIRED", "目标工作空间不存在。请先由管理员创建，再运行本任务。"
                    )
                for resource in plan["resources"]:
                    try:
                        results.append(publisher.publish(resource))
                    except Exception as exc:
                        code, message = _safe_error(exc)
                        unresolved = any(
                            op["outcome"] in {"INTENT", "UNKNOWN"}
                            for op in journal.operations(plan["task_id"], resource["resource_id"])
                        )
                        state = (
                            "CANCELLED"
                            if isinstance(exc, Cancelled)
                            else "MANUAL_ACTION_REQUIRED"
                            if isinstance(exc, RecoveryRequired) or unresolved
                            else "FAILED"
                        )
                        result = {
                            "resource_id": resource["resource_id"],
                            "target_name": resource["target_name"],
                            "status": state,
                            "code": code,
                            "message": message,
                        }
                        journal.set_resource(
                            plan["task_id"],
                            resource["resource_id"],
                            status=state,
                            stage="STOPPED",
                            details=result,
                        )
                        publisher.event(code, message, resource["resource_id"], "ERROR")
                        results.append(result)
                        if state in {"CANCELLED", "MANUAL_ACTION_REQUIRED"}:
                            break
                statuses = {r["status"] for r in results}
                if "MANUAL_ACTION_REQUIRED" in statuses:
                    status = "MANUAL_ACTION_REQUIRED"
                elif "CANCELLED" in statuses:
                    status = "CANCELLED"
                elif "FAILED" in statuses:
                    status = "PARTIAL_SUCCESS" if "SUCCEEDED" in statuses else "FAILED"
                elif "REVIEW_REQUIRED" in statuses:
                    status = "REVIEW_REQUIRED"
                elif statuses == {"SKIPPED"}:
                    status = "SKIPPED"
                else:
                    status = "SUCCEEDED"
                journal.set_task_status(plan["task_id"], status)
                return publisher.report(status, results)
            except Exception as exc:
                if not started:
                    raise
                code, message = _safe_error(exc)
                status = "MANUAL_ACTION_REQUIRED" if isinstance(exc, RecoveryRequired) else "FAILED"
                journal.set_task_status(plan["task_id"], status)
                publisher.event(code, message, level="ERROR")
                report = publisher.report(status, results)
                report["code"], report["message"] = code, message
                save_json(output / "report.json", report)
                return report
            finally:
                journal.release_locks(plan["task_id"])
    finally:
        if close_client:
            client.close()


def rollback_plan(plan: dict, journal_path=None, *, credentials=None, client=None) -> dict:
    """Explicit cleanup of task-owned catalog objects; physical server files are retained."""
    environment = Environment(**plan["environment"])
    output = Path(plan["output_dir"])
    journal_path = Path(journal_path) if journal_path else output.parent / "geopublisher.sqlite"
    close_client = client is None
    if client is None:
        if credentials is None:
            client = environment.client()
        else:
            client = GeoServerClient(
                environment.base_url.rstrip("/"),
                *credentials,
                timeout=environment.timeout,
                verify=environment.ca_bundle or True,
            )
    try:
        with Journal(journal_path) as journal:
            recorded = journal.get_task(plan["task_id"])
            if recorded["plan"] != plan:
                raise ConfigError("PLAN_CHANGED", "清理计划与任务记录不一致。")
            journal.recover_locks(plan["task_id"])
            publisher = Publisher(plan, journal, client)
            removed = []
            cleanup_locked = False
            try:
                journal.acquire_lock(plan["task_id"], "task:" + plan["task_id"])
                cleanup_locked = True
                for resource in plan["resources"]:
                    journal.acquire_lock(
                        plan["task_id"],
                        "layer:"
                        + fingerprint(
                            [environment.base_url.rstrip("/"), environment.workspace, resource["target_name"]]
                        ),
                    )
                if any(op["outcome"] in {"UNKNOWN", "INTENT"} for op in journal.operations(plan["task_id"])):
                    raise RecoveryRequired("存在结果未知的写操作，不能自动清理。请先人工核对。")
                # Preflight every owned object before deleting the first one.
                for resource in plan["resources"]:
                    for obj in journal.objects(plan["task_id"], resource["resource_id"]):
                        publisher.assert_owned(resource, obj["kind"])
                journal.set_task_status(plan["task_id"], "ROLLING_BACK")
                for resource in reversed(plan["resources"]):
                    for kind in ("layer", "resource", "store", "style"):
                        obj = publisher.owned(resource, kind)
                        if not obj:
                            continue
                        publisher.assert_owned(resource, kind)
                        op_id = journal.begin_operation(
                            plan["task_id"],
                            resource["resource_id"],
                            "DELETE_" + kind.upper(),
                            {"kind": kind, "name": obj["name"]},
                            {"fingerprint": obj["fingerprint"]},
                        )
                        try:
                            if kind == "layer":
                                client.delete_layer(environment.workspace, resource["target_name"])
                            elif kind == "resource":
                                client.delete_resource(
                                    environment.workspace,
                                    resource["store_name"],
                                    resource["target_name"],
                                    resource["kind"],
                                )
                            elif kind == "store":
                                client.delete_store(
                                    environment.workspace, resource["store_name"], resource["kind"]
                                )
                            else:
                                client.delete_style(environment.workspace, resource["style_name"])
                            if publisher.current(resource, kind) is not None:
                                raise RecoveryRequired("删除后仍能读取目标对象。")
                        except BaseException as exc:
                            journal.finish_operation(
                                op_id,
                                outcome="UNKNOWN",
                                evidence={"code": getattr(exc, "code", "INTERRUPTED")},
                            )
                            journal.set_task_status(plan["task_id"], "MANUAL_ACTION_REQUIRED")
                            raise
                        journal.finish_operation(
                            op_id, outcome="SUCCEEDED", evidence={"confirmed_absent": True}
                        )
                        removed.append({"kind": kind, "name": obj["name"]})
                journal.set_task_status(plan["task_id"], "ROLLED_BACK")
                result = {
                    "status": "ROLLED_BACK",
                    "removed": removed,
                    "server_data_retained": True,
                    "message": "已删除本任务拥有的服务配置；服务器上传文件保留，未执行物理数据清除。",
                }
                save_json(output / "cleanup-report.json", result)
                return result
            except Exception as exc:
                if cleanup_locked:
                    code, message = _safe_error(exc)
                    journal.add_event(
                        plan["task_id"],
                        "ERROR",
                        "CLEANUP_BLOCKED",
                        message,
                        details={"code": code, "removed_count": len(removed)},
                    )
                    save_json(
                        output / "cleanup-report.json",
                        {
                            "status": "MANUAL_ACTION_REQUIRED",
                            "code": code,
                            "message": message,
                            "removed": removed,
                            "server_data_retained": True,
                        },
                    )
                raise
            finally:
                journal.release_locks(plan["task_id"])
    finally:
        if close_client:
            client.close()
