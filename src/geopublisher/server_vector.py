"""Package immutable Shapefile artifacts for server-first GeoServer publication.

Uses the existing verified plan, tar/SFTP staging, and SHA256 Linux extraction.
No business data ever leaves the authorized local or private server environment.
"""
from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import stat
import tarfile
import zipfile
from pathlib import Path

from .plans import digest_file, load_plan, verify_artifacts
from .server_deploy import LinuxTarget, ReleaseArchive, ServerDeployError, _local_regular, preflight

_SHP_SUFFIXES = frozenset({".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix"})
_REQUIRED = frozenset({".shp", ".shx", ".dbf", ".prj"})
_MAX_FILES = 12000
_MAX_BYTES = 80 * 1024**3
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_RESOURCE = re.compile(r"r[0-9]{4,6}\Z")


def _stop(code: str, message: str) -> None:
    raise ServerDeployError(code, message)


def _members(resource: dict) -> list[tuple[zipfile.ZipInfo, str]]:
    """The upload ZIP is the plan's immutable data artifact, not original GDB."""
    native = resource["artifact"].get("native_name")
    if not isinstance(native, str) or not _NAME.fullmatch(native):
        _stop("VECTOR_NATIVE_NAME_INVALID", "SHP 图层原生文件名不适用于服务器独立存储。")
    packed = Path(resource["artifact"]["path"])
    if not packed.is_file() or packed.is_symlink():
        _stop("VECTOR_ARTIFACT_MISSING", "计划中的 SHP ZIP 不存在或不是普通文件。")
    result = []
    seen = set()
    with zipfile.ZipFile(packed) as archive:
        if len(archive.infolist()) > 8:
            _stop("VECTOR_ZIP_COMPLEX", "SHP ZIP 含多套或未知配套文件，不允许上送服务器。")
        for info in archive.infolist():
            if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                _stop("VECTOR_ZIP_UNSAFE", "SHP ZIP 不允许目录或符号链接。")
            name = info.filename
            path = Path(name)
            if ("/" in name or "\\" in name or not name.isascii()
                    or name != f"{native}{path.suffix}" or
                    path.suffix.lower() not in _SHP_SUFFIXES or
                    path.suffix.lower() in seen):
                _stop("VECTOR_ZIP_UNSAFE", "SHP ZIP 内文件名或扩展名不符合单图层安全规则。")
            seen.add(path.suffix.lower())
            result.append((info, name))
        if not _REQUIRED.issubset(seen):
            _stop("VECTOR_COMPONENT_MISSING", "ZIP 缺少 .shp/.shx/.dbf/.prj，不能作为服务器图层。")
    return result


def bundle_vector_plan(plan_file: Path | str, destination: Path | str) -> dict:
    """Create a new local .tar and index from fully verified SHP plan ZIPs.

    The output folder must not exist, to prevent mixing builds or rewriting a
    previously approved archive.
    """
    file = _local_regular(plan_file, suffix=".json")
    plan = load_plan(file)
    if file.name != "plan.json" or not plan.get("ready"):
        _stop("VECTOR_PLAN_NOT_READY", "请选择预检通过且未改动的 plan.json。")
    verify_artifacts(plan)
    resources = plan["resources"]
    if not resources or len(resources) > 1000 or any(r["kind"] != "shapefile" for r in resources):
        _stop("VECTOR_SHAPEFILES_ONLY", "服务器 SHP 模式仅接受 1–1000 个 SHP 资源；TIFF 另走原栅格发布流程。")
    dest = Path(destination).expanduser().absolute()
    if (str(dest).startswith(("\\\\", "//")) or dest.exists() or dest.is_symlink()
            or not dest.parent.is_dir() or dest.parent.is_symlink()
            or dest.is_relative_to(file.parent) or file.is_relative_to(dest)):
        _stop("VECTOR_BUNDLE_OUTPUT_UNSAFE", "服务器发布包必须写到原始任务之外的全新本地目录。")
    release = "gpv_" + plan["task_id"].replace("-", "")[:20]
    archive_path = dest / f"{release}.tar"
    records, sums = [], []
    # Validate each ZIP before creating any output.
    for resource in resources:
        rid = resource["resource_id"]
        if not _RESOURCE.fullmatch(rid):
            _stop("VECTOR_RESOURCE_ID_INVALID", "发布计划包含不合法的资源编号。")
        entries = _members(resource)
        records.append((resource, entries))
    dest.mkdir(parents=False, exist_ok=False)
    rows = []
    try:
        with tarfile.open(archive_path, mode="x") as archive:
            readme = (
                "# GeoPublisher server-side vector release\n"
                "Verified Shapefile components only. No scripts, credentials or original GDB.\n"
                "Do not serve this folder until the SHA256SUMS verify step is complete.\n"
            ).encode("utf-8")
            info = tarfile.TarInfo(f"{release}/README.md")
            info.size = len(readme)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(readme))
            total_unpacked = 0
            for resource, entries in records:
                rid = resource["resource_id"]
                packed = Path(resource["artifact"]["path"])
                native = resource["artifact"]["native_name"]
                parts = []
                with zipfile.ZipFile(packed) as zipped:
                    for item, filename in entries:
                        total_unpacked += item.file_size
                        if item.file_size < 0 or total_unpacked > _MAX_BYTES:
                            _stop("VECTOR_RELEASE_TOO_LARGE", "服务器发布包超过已允许的 80GB 解包上限。")
                        destination_name = f"{rid}/{filename}"
                        digest = hashlib.sha256()
                        with zipped.open(item) as member:
                            for block in iter(lambda: member.read(1024 * 1024), b""):
                                digest.update(block)
                        record = tarfile.TarInfo(f"{release}/data/{destination_name}")
                        record.size = item.file_size
                        record.mode = 0o640
                        with zipped.open(item) as member:
                            archive.addfile(record, member)
                        sums.append(f"{digest.hexdigest()}  {destination_name}")
                        parts.append({"filename": filename, "sha256": digest.hexdigest()})
                rows.append({
                    "resource_id": rid, "store_name": resource["store_name"],
                    "target_name": resource["target_name"],
                    "relative_shp": f"data/{rid}/{native}.shp",
                    "components": parts,
                    "artifact_sha256": resource["artifact"]["sha256"],
                })
            if len(sums) > _MAX_FILES:
                _stop("VECTOR_RELEASE_TOO_MANY_FILES", "服务器发布归档超过文件数量限制。")
            text = ("\n".join(sums) + "\n").encode("ascii")
            record = tarfile.TarInfo(f"{release}/data/SHA256SUMS.txt")
            record.mode = 0o640
            record.size = len(text)
            archive.addfile(record, io.BytesIO(text))
        verified = ReleaseArchive.inspect(archive_path)
        manifest = {
            "schema_version": 1, "operation": "SERVER_VECTOR_RELEASE",
            "release": release, "task_id": plan["task_id"],
            "plan_sha256": digest_file(file), "plan_hash": plan["plan_hash"],
            "archive_sha256": verified.sha256,
            "archive_size": verified.size, "manifest_count": verified.manifest_count,
            "resources": rows,
            "geoserver_published": False, "database_registered": False,
        }
        marker = dest / "vector-release.json"
        with marker.open("x", encoding="utf-8") as output:
            json.dump(manifest, output, ensure_ascii=False, indent=2)
            output.write("\n")
        return {
            "status": "LOCAL_VECTOR_RELEASE_READY",
            "archive": str(archive_path), "release_manifest": str(marker),
            "sha256": verified.sha256, "resources": len(rows),
            "target_server_uploaded": False, "database_modified": False,
        }
    except BaseException:
        # Explicitly leave unfinished directory, without a success manifest.
        raise


def verify_vector_release(
    plan_file: Path | str, archive_path: Path | str, manifest_file: Path | str,
    target: LinuxTarget, session,
) -> dict[str, str]:
    """Validate a previously SFTP-staged & extracted release before REST writes."""
    plan_file = _local_regular(plan_file, suffix=".json")
    archive = ReleaseArchive.inspect(archive_path)
    manifest_file = _local_regular(manifest_file, suffix=".json")
    plan = load_plan(plan_file)
    verify_artifacts(plan)
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        assert isinstance(data, dict)
        assert data["schema_version"] == 1
        assert data["operation"] == "SERVER_VECTOR_RELEASE"
        assert data["task_id"] == plan["task_id"]
        assert data["plan_hash"] == plan["plan_hash"]
        assert data["plan_sha256"] == digest_file(plan_file)
        assert data["release"] == archive.name
        assert data["archive_sha256"] == archive.sha256
        assert data["archive_size"] == archive.size
        assert data["manifest_count"] == archive.manifest_count
        assert isinstance(data["resources"], list)
        assert len(data["resources"]) == len(plan["resources"])
    except (KeyError, AssertionError, TypeError, ValueError, UnicodeError, OSError) as exc:
        raise ServerDeployError("VECTOR_RELEASE_MISMATCH", "服务器 SHP 发布包与不可变任务计划不一致。") from exc
    preflight(target, session)
    directory = posixpath.join(target.root, archive.name)
    tar = posixpath.join(target.root, archive.path.name)
    if session.kind(tar) != "file" or session.kind(directory) != "dir":
        _stop("VECTOR_NOT_EXTRACTED", "目标服务器发布包未上传并安全解包。")
    server_sha = session.run("digest", tar, timeout=3600).split()[0].lower()
    if server_sha != archive.sha256:
        _stop("VECTOR_REMOTE_ARCHIVE_MISMATCH", "服务器 tar 的 SHA256 与审核发布包不一致。")
    checked = session.run("verify", posixpath.join(directory, "data"), timeout=3600)
    if checked != f"VERIFIED_FILES={archive.manifest_count}":
        _stop("VECTOR_FILES_NOT_VERIFIED", "服务器 SHP 配套组件未通过 SHA256 全量校验。")
    bindings = {}
    for planned, item in zip(plan["resources"], data["resources"], strict=True):
        rid = planned["resource_id"]
        native = planned["artifact"]["native_name"]
        expected = f"data/{rid}/{native}.shp"
        if (item.get("resource_id") != rid or item.get("relative_shp") != expected
                or item.get("store_name") != planned["store_name"]
                or item.get("target_name") != planned["target_name"]
                or item.get("artifact_sha256") != planned["artifact"]["sha256"]):
            _stop("VECTOR_RESOURCE_MISMATCH", "服务器文件与发布计划中的图层对应关系不一致。")
        suffixes = {Path(v["filename"]).suffix.lower() for v in item.get("components", [])
                    if isinstance(v, dict) and isinstance(v.get("filename"), str)}
        if not _REQUIRED.issubset(suffixes):
            _stop("VECTOR_COMPONENT_MISSING", "发布的 SHP 组件不完整。")
        for component in item["components"]:
            name = component["filename"]
            if (name != f"{native}{Path(name).suffix}"
                    or Path(name).suffix.lower() not in _SHP_SUFFIXES
                    or not re.fullmatch(r"[a-f0-9]{64}", component.get("sha256", ""))):
                _stop("VECTOR_COMPONENT_INVALID", "SHP 组件清单名称或摘要非法。")
            absolute = posixpath.join(directory, "data", rid, name)
            if session.kind(absolute) != "file":
                _stop("VECTOR_REMOTE_FILE_UNSAFE", "服务器目录缺少 SHP 普通文件或存在符号链接。")
            digest = session.run("digest", absolute, timeout=3600).split()[0].lower()
            if digest != component["sha256"]:
                _stop("VECTOR_REMOTE_COMPONENT_CHANGED", "服务器 SHP 文件与发布包原始 SHA256 不匹配。")
        bindings[rid] = posixpath.join(directory, expected)
    return bindings
