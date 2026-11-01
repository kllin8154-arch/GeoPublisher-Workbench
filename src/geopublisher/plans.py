"""Immutable, reviewable plans. Preparing a plan performs no network writes."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, Environment, validate_name


def digest_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def save_json(path: Path | str, value: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _target_name(source: Path) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.stem).strip("_.-")
    if not candidate or not re.match("[A-Za-z_]", candidate):
        candidate = "layer_" + fingerprint(source.name)[:10]
    return candidate[:72]


def build_plan(
    environment: Environment,
    sources: list[Path | str],
    output_dir: Path | str,
    *,
    strategy: str = "fail",
    encoding: str | None = None,
    declared_crs: str | None = None,
    sld: Path | str | None = None,
    target_name: str | None = None,
    enable_wfs: bool = False,
    raster_mode: str | None = None,
    task_id: str | None = None,
) -> dict:
    from .datasets import GdalRuntime, package_dataset
    from .imports import prepare_imports, verify_import_intent
    from .styles import validate_sld, write_default_sld

    if strategy not in {"fail", "skip", "new-version"}:
        raise ConfigError(
            "UNSUPPORTED_STRATEGY", "首版仅支持 fail、skip、new-version；已有数据原位替换尚未开放。"
        )
    if task_id is None:
        task_id = str(uuid.uuid4())
    try:
        valid_id = str(uuid.UUID(task_id)) == task_id
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    if not valid_id:
        raise ConfigError("INVALID_TASK_ID", "任务标识必须是 UUID。")
    output = Path(output_dir).resolve()
    if output.exists():
        raise ConfigError("PLAN_EXISTS", "任务目录已经存在；请使用新目录，或恢复现有任务。")
    import_intent = prepare_imports(sources, raster_mode=raster_mode)
    if any(
        selection["kind"] == "directory" and output.is_relative_to(Path(selection["path"]))
        for selection in import_intent["selections"]
    ):
        raise ConfigError(
            "OUTPUT_INSIDE_SOURCE", "任务目录不能位于导入目录内；请选择独立的输出位置。"
        )
    paths = [Path(item["path"]) for item in import_intent["datasets"]]
    if target_name and len(paths) != 1:
        raise ConfigError("AMBIGUOUS_NAME", "指定单个图层名时只能导入一个数据集。")
    runtime = GdalRuntime(
        environment.gdal_bin,
        expected_version=environment.gdal_version,
        credential_env_names=(environment.username_env, environment.password_env),
    )
    capabilities = runtime.probe()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise ConfigError("PLAN_EXISTS", "任务目录已被另一操作占用；请使用新目录。") from exc
    resources = []
    used_names: set[str] = set()
    for index, source in enumerate(paths):
        resource_id = f"r{index + 1:04d}"
        target = validate_name(target_name or _target_name(source), "图层名")
        if strategy == "new-version":
            target = target[:72] + "__v" + task_id.replace("-", "")[:12]
        if target in used_names:
            raise ConfigError("DUPLICATE_TARGET", "多个输入映射到同名图层，请拆分任务或调整文件名。")
        used_names.add(target)
        info = runtime.inspect(source, encoding=encoding, declared_crs=declared_crs)
        info["source"] = str(source)
        resource = {
            "resource_id": resource_id,
            "target_name": target,
            "store_name": target[:64] + "__gp_" + task_id.replace("-", "")[:12] + "_" + resource_id,
            "style_name": target[:64] + "__gp_" + task_id.replace("-", "")[:12] + "_" + resource_id,
            "kind": info["kind"],
            "inspection": info,
            "blocking_errors": list(info.get("blocking_errors", [])),
            "warnings": list(info.get("warnings", [])),
        }
        if not isinstance(info.get("crs"), str) or not re.fullmatch(r"EPSG:[0-9]{1,8}", info["crs"], re.I):
            resource["blocking_errors"].append(
                {
                    "code": "UNSUPPORTED_CRS",
                    "message": "首版发布仅支持可验证的 EPSG 坐标系；自定义 WKT 需单独适配，未执行上传。",
                }
            )
        resource_dir = output / resource_id
        resource_dir.mkdir(exist_ok=True)
        if not resource["blocking_errors"]:
            artifact = package_dataset(info, resource_dir)
            resource["artifact"] = artifact
            style_file = resource_dir / "style.sld"
            if sld:
                source_style = Path(sld).resolve()
                if source_style.stat().st_size > 5 * 1024 * 1024:
                    raise ConfigError("SLD_TOO_LARGE", "SLD 超过首版允许的 5 MiB 上限。")
                with source_style.open("rb") as stream:
                    style_bytes = stream.read(5 * 1024 * 1024 + 1)
                if len(style_bytes) > 5 * 1024 * 1024:
                    raise ConfigError("SLD_TOO_LARGE", "SLD 超过首版允许的 5 MiB 上限。")
                with style_file.open("xb") as stream:
                    stream.write(style_bytes)
            else:
                write_default_sld(
                    style_file,
                    name=resource["style_name"],
                    geometry_type=info.get("geometry_type"),
                    band_count=info.get("band_count"),
                )
            style_result = validate_sld(
                style_file, fields=info.get("fields") if info["kind"] == "shapefile" else None
            )
            resource["style_validation"] = style_result
            resource["style"] = {"path": str(style_file), "sha256": digest_file(style_file)}
            resource["blocking_errors"].extend(style_result.get("blocking_errors", []))
            resource["warnings"].extend(style_result.get("warnings", []))
            bbox = info.get("wgs84_bbox")
            if (
                not bbox
                or len(bbox) != 4
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox)
            ):
                resource["blocking_errors"].append(
                    {"code": "VERIFICATION_EXTENT", "message": "无法获得可靠的 WGS84 验收范围。"}
                )
        resources.append(resource)
    plan = {
        "schema_version": 2,
        "task_id": task_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": environment.to_dict(),
        "strategy": strategy,
        "enable_wfs": bool(enable_wfs),
        "output_dir": str(output),
        "capabilities": capabilities,
        "import_intent": import_intent,
        "resources": resources,
        "ready": not any(r["blocking_errors"] for r in resources),
        "catalog_mode": "export-only",
    }
    validate_plan(plan)
    verify_import_intent(import_intent)
    plan["plan_hash"] = fingerprint(plan)
    save_json(output / "plan.json", plan)
    return plan


def load_plan(path: Path | str) -> dict:
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise ValueError("Plan must be a JSON object")
        recorded_hash = plan.pop("plan_hash")
        actual_hash = fingerprint(plan)
        plan["plan_hash"] = recorded_hash
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError("INVALID_PLAN", "计划文件无效。请重新生成计划。") from exc
    if recorded_hash != actual_hash or plan.get("schema_version") not in (1, 2):
        raise ConfigError("PLAN_CHANGED", "计划已改变或版本不兼容；请重新预检并确认。")
    validate_plan(plan)
    return plan


def validate_plan(plan: dict) -> None:
    from .imports import validate_import_intent

    try:
        if (
            not isinstance(plan, dict)
            or type(plan["schema_version"]) is not int
            or plan["schema_version"] not in (1, 2)
        ):
            raise ValueError()
        if str(uuid.UUID(plan["task_id"])) != plan["task_id"]:
            raise ValueError()
        if type(plan["ready"]) is not bool or type(plan["enable_wfs"]) is not bool:
            raise ValueError()
        if plan["strategy"] not in {"fail", "skip", "new-version"}:
            raise ValueError()
        Environment(**plan["environment"])
        output = Path(plan["output_dir"])
        if not output.is_absolute() or not isinstance(plan["resources"], list) or not plan["resources"]:
            raise ValueError()
        seen_ids, seen_names = set(), set()
        for resource in plan["resources"]:
            rid = resource["resource_id"]
            if not re.fullmatch(r"r[0-9]{4,8}", rid) or rid in seen_ids:
                raise ValueError()
            seen_ids.add(rid)
            for key in ("target_name", "store_name", "style_name"):
                validate_name(resource[key])
            if resource["target_name"] in seen_names or resource["kind"] not in {"shapefile", "geotiff"}:
                raise ValueError()
            seen_names.add(resource["target_name"])
            if not isinstance(resource["blocking_errors"], list):
                raise ValueError()
            if plan["ready"]:
                if resource["blocking_errors"]:
                    raise ValueError()
                for key in ("artifact", "style"):
                    item = resource[key]
                    if (
                        not isinstance(item["path"], str)
                        or not Path(item["path"]).is_absolute()
                        or not Path(item["path"]).resolve().is_relative_to(output.resolve())
                    ):
                        raise ValueError()
                    if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                        raise ValueError()
                if (
                    not isinstance(resource["artifact"]["source_hashes"], list)
                    or not resource["artifact"]["source_hashes"]
                ):
                    raise ValueError()
                for source in resource["artifact"]["source_hashes"]:
                    if not Path(source["path"]).is_absolute() or not re.fullmatch(
                        r"[0-9a-f]{64}", source["sha256"]
                    ):
                        raise ValueError()
                if plan["schema_version"] == 2 and resource["inspection"]["source"] not in {
                    source["path"] for source in resource["artifact"]["source_hashes"]
                }:
                    raise ValueError()
                bbox = resource["inspection"]["wgs84_bbox"]
                if (
                    not isinstance(bbox, list)
                    or len(bbox) != 4
                    or not all(type(v) in (int, float) and math.isfinite(v) for v in bbox)
                ):
                    raise ValueError()
                if not (-180 <= bbox[0] <= bbox[2] <= 180 and -90 <= bbox[1] <= bbox[3] <= 90):
                    raise ValueError()
        if plan["schema_version"] == 2:
            validate_import_intent(plan["import_intent"], plan["resources"])
            if any(
                selection["kind"] == "directory"
                and output.resolve().is_relative_to(Path(selection["path"]))
                for selection in plan["import_intent"]["selections"]
            ):
                raise ValueError()
        elif "import_intent" in plan:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(
            "INVALID_PLAN", "发布计划字段不完整、类型不正确或超出支持范围；请重新预检。"
        ) from exc


def verify_artifacts(plan: dict) -> None:
    from .datasets import DatasetError
    from .imports import prepare_imports, validate_import_intent, verify_import_intent

    # Reading/cleanup of legacy plans must remain possible. Only execution
    # requires import consent and current source context, before any client exists.
    try:
        if plan["schema_version"] == 2:
            verify_import_intent(plan["import_intent"])
        else:
            if sum(r["kind"] == "geotiff" for r in plan["resources"]) > 1:
                raise ConfigError(
                    "LEGACY_RASTER_INTENT_REQUIRED",
                    "旧版多 TIFF 计划未记录分别发布的确认，不能继续发布；请重新生成计划。"
                    "已有任务的配置清理仍使用原计划。",
                )
            sources = [resource["inspection"].get("source") for resource in plan["resources"]]
            if not all(isinstance(source, str) and Path(source).is_absolute() for source in sources):
                raise ConfigError("INVALID_PLAN", "旧计划缺少可核对的源文件路径；请重新生成计划。")
            validate_import_intent(prepare_imports(sources), plan["resources"])
    except DatasetError as exc:
        raise ConfigError(exc.code, str(exc)) from exc
    for resource in plan["resources"]:
        for artifact in (resource.get("artifact"), resource.get("style")):
            if (
                not artifact
                or not Path(artifact["path"]).is_file()
                or digest_file(artifact["path"]) != artifact["sha256"]
            ):
                raise ConfigError("ARTIFACT_CHANGED", "预检后的发布文件已经缺失或改变；必须重新生成计划。")
        for source in resource["artifact"].get("source_hashes", []):
            if not Path(source["path"]).is_file() or digest_file(source["path"]) != source["sha256"]:
                raise ConfigError("SOURCE_CHANGED", "源数据在预检后已经改变；必须重新生成计划。")
