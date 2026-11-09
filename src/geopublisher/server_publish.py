"""Safely register *already staged* Linux GeoTIFF/ImageMosaic via GeoServer REST.

Staging/extraction and business database SQL are separate phases. Publishing
requires a fully reviewed, explicit plan, fresh store/layer names, a matching
server-side tar SHA256 and data checksums, and manual confirmation. Before each
REST PUT an fsynced local journal intent is written. No automatic retry/delete.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from .config import validate_name
from .geoserver import GeoServerClient
from .server_deploy import (
    LinuxTarget,
    ReleaseArchive,
    ServerDeployError,
    _local_regular,
    preflight,
)
from .server_forward import open_service
from .verification import verify_image

_IMAGE_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True)
class RasterLayer:
    store: str
    name: str
    kind: str
    source: str
    bbox: tuple[float, float, float, float]
    granules: int | None = None


@dataclass(frozen=True)
class RasterPublishPlan:
    release: str
    workspace: str
    geoserver_version: str
    layers: tuple[RasterLayer, ...]

    @classmethod
    def load(cls, file: Path | str, archive: ReleaseArchive) -> RasterPublishPlan:
        f = _local_regular(file, suffix=".json")
        if f.stat().st_size > 100_000:
            raise ServerDeployError("BAD_RASTER_PLAN", "发布清单文件过大。")
        try:
            raw = json.loads(f.read_text("utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ServerDeployError("BAD_RASTER_PLAN", "不能读取 GeoServer 发布清单。") from exc
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version", "release", "workspace", "geoserver_version", "layers",
        }:
            raise ServerDeployError("BAD_RASTER_PLAN", "GeoServer 发布清单字段不完整或存在未知字段。")
        if raw["schema_version"] != 1 or raw["release"] != archive.name:
            raise ServerDeployError("RELEASE_MISMATCH", "发布清单与本机 tar 发布包版本不一致。")
        workspace = validate_name(raw["workspace"], "GeoServer 工作空间")
        version = raw["geoserver_version"]
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3}){1,2}", version):
            raise ServerDeployError("BAD_GEOSERVER_VERSION", "必须明确目标 GeoServer 精确版本。")
        if not isinstance(raw["layers"], list) or not 1 <= len(raw["layers"]) <= 32:
            raise ServerDeployError("BAD_RASTER_PLAN", "本批需有 1–32 个受控栅格图层。")
        layers = []
        seen_stores, seen_layers = set(), set()
        for entry in raw["layers"]:
            if not isinstance(entry, dict) or set(entry) != {
                "store", "layer", "kind", "source", "bbox", "granules",
            }:
                raise ServerDeployError("BAD_RASTER_LAYER", "每个图层需要明确存储、名称、类型、来源、范围和 granules。")
            store = validate_name(entry["store"], "GeoServer 存储")
            name = validate_name(entry["layer"], "GeoServer 图层")
            kind = entry["kind"]
            if kind not in ("geotiff", "imagemosaic"):
                raise ServerDeployError("BAD_RASTER_KIND", "仅可登记单幅 GeoTIFF 或已整理的 ImageMosaic。")
            if store in seen_stores or name in seen_layers:
                raise ServerDeployError("DUPLICATE_RASTER_STORE", "同一批任务不可复用存储名或图层名。")
            seen_stores.add(store)
            seen_layers.add(name)
            source = entry["source"]
            if not isinstance(source, str) or "\\" in source or not source.startswith("data/"):
                raise ServerDeployError("UNSAFE_RASTER_PATH", "仅接受归档内 data/ 目录下的相对路径。")
            pieces = source.split("/")
            if any(part in ("", ".", "..") for part in pieces) or any(ord(c) < 32 for c in source):
                raise ServerDeployError("UNSAFE_RASTER_PATH", "Raster 路径存在穿越或控制字符。")
            embedded = archive.name + "/" + source
            if kind == "geotiff":
                if not source.lower().endswith((".tif", ".tiff")) or embedded not in archive.content_names:
                    raise ServerDeployError("MISSING_RASTER_SOURCE", "GeoTIFF 必须明确对应归档内单幅影像。")
            else:
                if not any(
                    f.startswith(embedded + "/") and f.lower().endswith((".tif", ".tiff"))
                    for f in archive.content_names
                ):
                    raise ServerDeployError("MISSING_MOSAIC_SOURCE", "ImageMosaic 目录缺少 TIFF 分幅。")
            bbox = entry["bbox"]
            if (
                not isinstance(bbox, list) or len(bbox) != 4
                or any(type(coord) not in (int, float) or not -1800 <= coord <= 1800 for coord in bbox)
                or not (bbox[0] < bbox[2] and bbox[1] < bbox[3])
            ):
                raise ServerDeployError("BAD_RASTER_BBOX", "WMS 验收范围必须明确为 EPSG:4326 中可用的四个坐标。")
            granules = entry["granules"]
            if granules is not None and (
                type(granules) is not int or not 1 <= granules <= 10000 or kind != "imagemosaic"
            ):
                raise ServerDeployError("BAD_GRANULE_COUNT", "只有 ImageMosaic 可指定预期 granules 数量。")
            layers.append(RasterLayer(
                store=store, name=name, kind=kind, source=source,
                bbox=tuple(float(coord) for coord in bbox), granules=granules,
            ))
        return cls(archive.name, workspace, version, tuple(layers))


class _Audit:
    def __init__(self, directory: Path):
        if str(directory).startswith(("\\\\", "//")) or directory.is_symlink() or any(
            part.is_symlink() for part in directory.parents
        ) or directory.exists():
            raise ServerDeployError("UNSAFE_AUDIT_PATH", "审计目录必须是全新的本机路径，不接受 UNC 或软链接。")
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.file = (directory / "server_raster_audit.jsonl").open("x", encoding="utf-8")

    def log(self, code: str, **data) -> None:
        self.file.write(json.dumps({"event": code, **data}, ensure_ascii=False, sort_keys=True) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())

    def close(self):
        self.file.close()


def _validate_server_copy(target: LinuxTarget, archive: ReleaseArchive, session) -> str:
    preflight(target, session)
    directory = posixpath.join(target.root, archive.name)
    path = posixpath.join(target.root, archive.path.name)
    if session.kind(directory) != "dir" or session.kind(path) != "file":
        raise ServerDeployError("RELEASE_NOT_STAGED", "服务器发布目录不是普通目录或 tar 非普通文件；拒绝跟随符号链接。")
    remote_sha = session.run("digest", path, timeout=3600).split()[0].lower()
    if remote_sha != archive.sha256:
        raise ServerDeployError("SERVER_RELEASE_CHANGED", "服务器 tar SHA256 与本机发布包不一致。")
    checked = session.run("verify", posixpath.join(directory, "data"), timeout=3600)
    if checked != f"VERIFIED_FILES={archive.manifest_count}":
        raise ServerDeployError("RASTER_NOT_VERIFIED", "Linux TIFF 清单未完整通过或文件数量不一致。")
    return directory


def _granules(client: GeoServerClient, workspace: str, store: str, layer: str) -> int:
    response = client._request(
        "GET",
        f"/rest/workspaces/{workspace}/coveragestores/{store}/coverages/{layer}/index/granules.json",
        operation="read ImageMosaic granule count",
    )
    try:
        obj = json.loads(response.content)
        count = obj.get("totalFeatures", obj.get("numberMatched"))
        if count is None:
            features = obj.get("features")
            if not isinstance(features, list):
                raise ValueError()
            count = len(features)
        if type(count) is not int or count < 1:
            raise ValueError()
        return count
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise ServerDeployError("BAD_MOSAIC_GRANULES", "服务器返回了无法核对的 ImageMosaic 分幅计数。") from exc


def deploy_rasters(
    target: LinuxTarget,
    archive: ReleaseArchive,
    plan: RasterPublishPlan,
    session,
    *,
    geoserver_username: str,
    geoserver_password: str,
    output: Path | str,
    publish: bool = False,
    client_factory=GeoServerClient,
) -> dict:
    """Preflight or explicitly create *new* raster layers; no DB writes or auto retry.

    A returning WMS PNG is machine evidence only. Human cartographic approval
    and SQL business registration always remain separate.
    """
    if not isinstance(geoserver_username, str) or not geoserver_username or not geoserver_password:
        raise ServerDeployError("GEOSERVER_AUTH_REQUIRED", "必须输入独立的 GeoServer REST 用户名和密码。")
    if plan.release != archive.name:
        raise ServerDeployError("RELEASE_MISMATCH", "发布计划与 tar 不一致。")
    directory = _validate_server_copy(target, archive, session)
    for item in plan.layers:
        path = posixpath.join(directory, item.source)
        expected_kind = "dir" if item.kind == "imagemosaic" else "file"
        if not path.startswith(directory + "/") or session.kind(path) != expected_kind:
            raise ServerDeployError("RASTER_SOURCE_MISSING", "服务器影像数据类型异常或为符号链接，停止发布。")
    output = Path(output).expanduser().absolute()
    with open_service(session, service="geoserver") as port:
        with client_factory(
            port.geoserver_url, geoserver_username, geoserver_password, timeout=240,
        ) as server:
            probe = server.probe()
            if probe["geoserver_version"] != plan.geoserver_version:
                raise ServerDeployError(
                    "GEOSERVER_VERSION_MISMATCH",
                    "目标 GeoServer 运行版本与发布清单不一致，需检查 JDK17、插件及版本依赖。",
                )
            if server.get_workspace(plan.workspace) is None:
                raise ServerDeployError("WORKSPACE_NOT_FOUND", "指定 GeoServer 工作空间不存在。")
            before = []
            for item in plan.layers:
                store = server.get_store(plan.workspace, item.store, "geotiff")
                layer = server.get_layer(plan.workspace, item.name)
                if publish and (store is not None or layer is not None):
                    raise ServerDeployError(
                        "RASTER_ALREADY_REGISTERED",
                        "同名 store/layer 已存在（或部分发布），禁止覆盖；请先读取服务状态与任务记录。",
                    )
                before.append({
                    "store": item.store, "layer": item.name, "kind": item.kind,
                    "store_exists": store is not None, "layer_exists": layer is not None,
                })
            if not publish:
                return {
                    "status": "GEOSERVER_READONLY_PREFLIGHT",
                    "server_geoserver_version": probe["geoserver_version"],
                    "workspace": plan.workspace, "preflight": before,
                    "server_modified": False, "database_accessed": False,
                }

            audit = _Audit(output)
            results = []
            try:
                audit.log("PREFLIGHT_PASSED", workspace=plan.workspace, release=plan.release,
                          release_sha256=archive.sha256, expected_version=plan.geoserver_version)
                for item in plan.layers:
                    source_path = posixpath.join(directory, item.source)
                    audit.log("REST_CREATE_INTENT", store=item.store, layer=item.name, kind=item.kind,
                              source_relative=item.source)
                    try:
                        server.register_external_raster(
                            plan.workspace, item.store, item.name, item.kind, source_path
                        )
                        store = server.get_store(plan.workspace, item.store, "geotiff")
                        layer = server.get_layer(plan.workspace, item.name)
                        if store is None or layer is None:
                            raise ServerDeployError(
                                "REST_REGISTRATION_NOT_CONFIRMED",
                                "REST 写入已返回，但 store/layer 仍无法确认；必须人工核对。",
                                outcome_unknown=True,
                            )
                    except Exception as exc:
                        audit.log("REST_CREATE_UNKNOWN", store=item.store, layer=item.name)
                        raise ServerDeployError(
                            "REST_RECONCILE_REQUIRED",
                            "创建图层中断或结果不可确认。请核对服务器实际存储/图层及审计文件，不能自动重试。",
                            outcome_unknown=True,
                        ) from exc
                    audit.log("REST_CREATE_CONFIRMED", store=item.store, layer=item.name)
                    if item.kind == "imagemosaic" and item.granules is not None:
                        observed = _granules(server, plan.workspace, item.store, item.name)
                        audit.log("MOSAIC_GRANULES", layer=item.name,
                                  expected=item.granules, observed=observed)
                        if observed != item.granules:
                            raise ServerDeployError(
                                "MOSAIC_GRANULE_COUNT_MISMATCH",
                                "ImageMosaic granules 数量与清单不一致，停止后续发布，需核对。",
                            )
                    response = server.get_map(
                        plan.workspace, item.name,
                        bbox=item.bbox, crs="EPSG:4326", width=512, height=512,
                    )
                    evidence = verify_image(
                        response.content, response.headers.get("content-type", ""),
                        expected_size=(512, 512),
                    )
                    preview = audit.directory / f"{item.name}_wms.png"
                    with preview.open("xb") as stream:
                        stream.write(response.content)
                    entry = {
                        "store": item.store, "layer": item.name,
                        "wms_status": evidence["status"], "wms_sha256": evidence["sha256"],
                        "wms_png": str(preview), "visual_reviewed": False,
                    }
                    results.append(entry)
                    audit.log("WMS_MACHINE_CHECKED", layer=item.name,
                              status=evidence["status"], sha256=evidence["sha256"])
                status = "PUBLISHED_NEEDS_HUMAN_WMS_REVIEW"
                audit.log("FINISHED", status=status, database_accessed=False)
                report = {
                    "status": status, "workspace": plan.workspace, "layers": results,
                    "source_archive_sha256": archive.sha256,
                    "audit_directory": str(audit.directory),
                    "visual_reviewed": False, "database_accessed": False,
                    "warning": "GeoServer 新图层已创建，但尚未人工确认样式/位置/接缝，也未登记业务数据库。",
                }
                (audit.directory / "server_raster_report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8"
                )
                return report
            finally:
                audit.close()
