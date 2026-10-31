"""Conservative, read-only import classification before individual publication.

Known Mosaic/Pyramid structures cannot be converted to individual resources by
an ``independent`` choice. Missing markers do not prove that rasters are
independent: the user must explicitly choose that interpretation for a batch.
Only marker keys are inspected; configuration values never leave this module.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .datasets import _IGNORED_DIRECTORIES, DatasetError, discover_sources

_MAX_MARKER_BYTES = 64 * 1024
_RESERVED_MARKERS = {"pyramid.properties", "indexer.properties", "indexer.xml"}
_STRUCTURE_CODES = {
    "UNSUPPORTED_MOSAIC",
    "UNSUPPORTED_PYRAMID",
    "RASTER_STRUCTURE_REVIEW_REQUIRED",
}


def _review(marker: Path) -> DatasetError:
    return DatasetError(
        "RASTER_STRUCTURE_REVIEW_REQUIRED",
        f"发现栅格结构标记 {marker.name}，但无法完整确认其内容；请先核对该数据组。当前不能按独立图层发布。",
    )


def _read_marker(path: Path) -> tuple[bytes, bool]:
    """Read a bounded plain file, without following a marker symlink."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError("Marker is not a plain file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        after = os.fstat(stream.fileno())
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise OSError("Marker changed while opening")
        content = stream.read(_MAX_MARKER_BYTES + 1)
    return content[:_MAX_MARKER_BYTES], len(content) <= _MAX_MARKER_BYTES


def _unescape_key(value: str) -> str:
    result = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            result.append(character)
            index += 1
            continue
        index += 1
        if index == len(value):
            raise ValueError("Incomplete key escape")
        character = value[index]
        if character == "u":
            digits = value[index + 1 : index + 5]
            if len(digits) != 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                raise ValueError("Invalid unicode key escape")
            result.append(chr(int(digits, 16)))
            index += 5
        else:
            result.append({"t": "\t", "r": "\r", "n": "\n", "f": "\f"}.get(character, character))
            index += 1
    return "".join(result).casefold()


def _property_keys(content: bytes) -> tuple[set[str], bool]:
    """Recognize Java property keys, never retain or return their values."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Java .properties traditionally uses ISO-8859-1 plus unicode escapes.
        text = content.decode("latin-1")
    keys: set[str] = set()
    valid = "\0" not in text
    pending = ""
    continuation = False
    for physical in text.splitlines():
        line = pending + physical.lstrip(" \t\f") if continuation else physical.lstrip(" \t\f")
        if not continuation and (not line or line.startswith(("#", "!"))):
            continue
        trailing = len(line) - len(line.rstrip("\\"))
        if trailing % 2:
            pending = line[:-1]
            continuation = True
            continue
        pending = ""
        continuation = False
        escaped = False
        end = len(line)
        for index, character in enumerate(line):
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character in "=: \t\f":
                end = index
                break
        try:
            keys.add(_unescape_key(line[:end]))
        except ValueError:
            valid = False
    return keys, valid and not continuation


def _check_marker(path: Path) -> None:
    name = path.name.casefold()
    if name == "datastore.properties":
        # A generic datastore file may contain credentials and does not by
        # itself establish a Mosaic. Do not parse or emit its values.
        return
    reserved = name in _RESERVED_MARKERS
    try:
        content, complete = _read_marker(path)
    except OSError as exc:
        if reserved:
            raise _review(path) from exc
        # An unrelated unreadable .properties at e.g. a drive root must not
        # block every import below it. Arbitrary unreadable names are outside
        # reliable marker detection; reserved names always fail closed.
        return
    if reserved and not complete:
        raise _review(path)
    if name == "indexer.xml":
        try:
            root = ElementTree.fromstring(content)
        except (ElementTree.ParseError, DefusedXmlException, ValueError) as exc:
            raise _review(path) from exc
        if root.tag.rsplit("}", 1)[-1].casefold() != "indexer":
            raise _review(path)
        raise DatasetError(
            "UNSUPPORTED_MOSAIC",
            f"发现 ImageMosaic 结构标记 {path.name}；当前不支持拼接服务及其辅助索引的独立发布。",
        )
    keys, valid = _property_keys(content)
    if name == "pyramid.properties":
        if not valid or "levelsdirs" not in keys or not ({"levels", "levelsnum"} & keys):
            raise _review(path)
        raise DatasetError(
            "UNSUPPORTED_PYRAMID",
            f"发现 ImagePyramid 结构标记 {path.name}；当前不支持将金字塔层级或其索引拆成独立图层。",
        )
    if name == "indexer.properties":
        indexer_keys = {
            "schema",
            "propertycollectors",
            "useexistingschema",
            "typename",
            "timedomainattribute",
            "elevationattribute",
            "canbeempty",
        }
        if not valid or not keys.intersection(indexer_keys):
            raise _review(path)
        raise DatasetError(
            "UNSUPPORTED_MOSAIC",
            f"发现 ImageMosaic 结构标记 {path.name}；当前不支持拼接服务及其辅助索引的独立发布。",
        )
    if {"levels", "locationattribute"}.issubset(keys) and keys.intersection({"levelsnum", "suggestedspi"}):
        raise DatasetError(
            "UNSUPPORTED_MOSAIC",
            f"发现 ImageMosaic 配置键组合（{path.name}）；当前不支持拼接服务及其辅助索引的独立发布。",
        )


def _check_directory(directory: Path, visited: set[str]) -> None:
    key = os.path.normcase(str(directory))
    if key in visited:
        return
    visited.add(key)
    try:
        markers = [
            item
            for item in directory.iterdir()
            if item.suffix.casefold() == ".properties" or item.name.casefold() == "indexer.xml"
        ]
    except OSError as exc:
        raise DatasetError(
            "RASTER_STRUCTURE_REVIEW_REQUIRED",
            "无法检查所选数据的目录结构标记；请确认输入目录及其父目录可读后重新预检。",
        ) from exc
    # A root pyramid descriptor takes precedence over its per-level mosaics.
    markers.sort(key=lambda p: (p.name.casefold() != "pyramid.properties", p.name.casefold()))
    for marker in markers:
        _check_marker(marker)


def _check_context(path: Path, *, directory: bool, visited: set[str]) -> None:
    parent = path if directory else path.parent
    for ancestor in (*reversed(parent.parents), parent):
        _check_directory(ancestor, visited)
    if directory:

        def walk_error(error: OSError) -> None:
            raise DatasetError(
                "RASTER_STRUCTURE_REVIEW_REQUIRED", "所选目录包含无法读取的子目录；请核对后重新预检。"
            ) from error

        for current, directories, _ in os.walk(path, followlinks=False, onerror=walk_error):
            current_path = Path(current)
            _check_directory(current_path, visited)
            directories[:] = sorted(
                name
                for name in directories
                if not name.startswith(".")
                and name.casefold() not in _IGNORED_DIRECTORIES
                and not (current_path / name).is_symlink()
            )


def _relative_origin(path: Path, selection: dict) -> str | None:
    selected = Path(selection["path"])
    if selection["kind"] == "file":
        return path.name if path == selected else None
    try:
        relative = path.relative_to(selected)
    except ValueError:
        return None
    if any(part.startswith(".") for part in relative.parts) or any(
        part.casefold() in _IGNORED_DIRECTORIES for part in relative.parts[:-1]
    ):
        return None
    return relative.as_posix()


def prepare_imports(sources: list[Path | str], *, raster_mode: str | None = None) -> dict:
    """Freeze original selections and require explicit independent raster intent."""
    if raster_mode is not None and (not isinstance(raster_mode, str) or raster_mode != "independent"):
        raise DatasetError("INVALID_RASTER_MODE", "栅格发布方式仅支持明确选择 independent（分别发布）。")
    if not isinstance(sources, list) or not sources:
        raise DatasetError("NO_DATASETS", "请先选择 SHP、GeoTIFF 文件或目录。")
    selections = []
    selected_paths: set[str] = set()
    visited: set[str] = set()
    for raw in sources:
        if not isinstance(raw, (str, Path)):
            raise DatasetError("INVALID_IMPORT_SELECTION", "输入必须是本地文件或目录路径。")
        path = Path(raw).absolute()
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise DatasetError("SYMLINK_NOT_ALLOWED", "符号链接目录或文件不受支持。")
        if path.is_file():
            kind = "file"
        elif path.is_dir():
            kind = "directory"
        else:
            raise DatasetError("SOURCE_NOT_FOUND", "选择的输入路径不存在。")
        path = path.resolve()
        key = os.path.normcase(str(path))
        if key in selected_paths:
            continue
        selected_paths.add(key)
        selections.append({"path": str(path), "kind": kind})
        _check_context(path, directory=kind == "directory", visited=visited)
    paths = discover_sources([Path(item["path"]) for item in selections])
    raster_count = sum(path.suffix.casefold() in {".tif", ".tiff"} for path in paths)
    if raster_count > 1 and raster_mode != "independent":
        raise DatasetError(
            "RASTER_INTENT_REQUIRED",
            f"发现 {raster_count} 个 TIFF；请明确确认按 {raster_count} 个独立图层分别发布。若应组成 Mosaic/Pyramid 服务，当前版本不支持。",
        )
    datasets = []
    for path in paths:
        origins = []
        for index, selection in enumerate(selections):
            relative = _relative_origin(path, selection)
            if relative is not None:
                origins.append({"selection": index, "relative_path": relative})
        datasets.append(
            {
                "path": str(path),
                "kind": "shapefile" if path.suffix.casefold() == ".shp" else "geotiff",
                "origins": origins,
            }
        )
    return {
        "schema_version": 1,
        "raster_mode": "none" if not raster_count else (raster_mode or "single"),
        "raster_count": raster_count,
        "resource_count": len(paths),
        "selections": selections,
        "datasets": datasets,
    }


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError()
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError()
    return path


def validate_import_intent(intent: dict, resources: list[dict]) -> None:
    """Validate frozen provenance without reading the original filesystem."""
    try:
        if not isinstance(intent, dict) or set(intent) != {
            "schema_version",
            "raster_mode",
            "raster_count",
            "resource_count",
            "selections",
            "datasets",
        }:
            raise ValueError()
        if type(intent["schema_version"]) is not int or intent["schema_version"] != 1:
            raise ValueError()
        if type(intent["raster_count"]) is not int or type(intent["resource_count"]) is not int:
            raise ValueError()
        mode = intent["raster_mode"]
        if not isinstance(mode, str) or mode not in {"none", "single", "independent"}:
            raise ValueError()
        selections, entries = intent["selections"], intent["datasets"]
        if not isinstance(selections, list) or not selections or not isinstance(entries, list) or not entries:
            raise ValueError()
        if not isinstance(resources, list) or len(resources) != len(entries):
            raise ValueError()
        selected_keys = set()
        for selection in selections:
            if not isinstance(selection, dict) or set(selection) != {"path", "kind"}:
                raise ValueError()
            key = os.path.normcase(str(_absolute_path(selection["path"])))
            if key in selected_keys or selection["kind"] not in {"file", "directory"}:
                raise ValueError()
            selected_keys.add(key)
        seen = set()
        raster_count = 0
        for entry, resource in zip(entries, resources, strict=True):
            if not isinstance(entry, dict) or set(entry) != {"path", "kind", "origins"}:
                raise ValueError()
            path = _absolute_path(entry["path"])
            key = os.path.normcase(str(path))
            kind = "shapefile" if path.suffix.casefold() == ".shp" else "geotiff"
            if (
                path.suffix.casefold() not in {".shp", ".tif", ".tiff"}
                or key in seen
                or entry["kind"] != kind
            ):
                raise ValueError()
            seen.add(key)
            raster_count += kind == "geotiff"
            if resource["kind"] != kind or resource["inspection"]["source"] != str(path):
                raise ValueError()
            origins = entry["origins"]
            if not isinstance(origins, list) or not origins:
                raise ValueError()
            for origin in origins:
                if not isinstance(origin, dict) or set(origin) != {"selection", "relative_path"}:
                    raise ValueError()
                relative = origin["relative_path"]
                if type(origin["selection"]) is not int or not isinstance(relative, str) or "\\" in relative:
                    raise ValueError()
                relative_path = PurePosixPath(relative)
                if (
                    relative_path.is_absolute()
                    or ".." in relative_path.parts
                    or relative_path.as_posix() != relative
                ):
                    raise ValueError()
            expected = []
            for index, selection in enumerate(selections):
                relative = _relative_origin(path, selection)
                if relative is not None:
                    expected.append({"selection": index, "relative_path": relative})
            if origins != expected:
                raise ValueError()
        if intent["resource_count"] != len(entries) or intent["raster_count"] != raster_count:
            raise ValueError()
        if (mode == "none" and raster_count != 0) or (mode == "single" and raster_count != 1):
            raise ValueError()
        if mode == "independent" and raster_count < 1:
            raise ValueError()
        if [entry["path"] for entry in entries] != sorted(
            (entry["path"] for entry in entries), key=str.casefold
        ):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DatasetError(
            "INVALID_IMPORT_INTENT", "计划中的导入来源、发布方式或资源数量不一致；请重新生成计划。"
        ) from exc


def verify_import_intent(intent: dict) -> None:
    """Recheck context and membership, never expand an already reviewed plan."""
    try:
        resources = [
            {"kind": item["kind"], "inspection": {"source": item["path"]}} for item in intent["datasets"]
        ]
    except (KeyError, TypeError) as exc:
        raise DatasetError("INVALID_IMPORT_INTENT", "计划中的导入来源无效；请重新生成计划。") from exc
    validate_import_intent(intent, resources)
    try:
        current = prepare_imports(
            [Path(item["path"]) for item in intent["selections"]],
            raster_mode="independent" if intent["raster_mode"] == "independent" else None,
        )
    except DatasetError as exc:
        if exc.code in _STRUCTURE_CODES:
            raise
        raise DatasetError(
            "IMPORT_SELECTION_CHANGED", "原导入目录、文件或发布意图已不再符合冻结计划；请重新预检。"
        ) from exc
    if current != intent:
        raise DatasetError(
            "IMPORT_SELECTION_CHANGED",
            "导入目录成员或文件/目录选择已经改变；原计划不会自动增加或减少图层。",
        )
