"""Read-only GDAL inspection and immutable, task-local dataset packaging.

GDAL is always selected explicitly; importing this module never inspects PATH or
initializes a native GIS library in the application process.
"""

from __future__ import annotations

import codecs
import errno
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any


class DatasetError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_SOURCE_SUFFIXES = {".shp", ".tif", ".tiff"}
_SHP_SUFFIXES = (".shp", ".shx", ".dbf", ".prj", ".cpg")
_IGNORED_DIRECTORIES = {"artifacts", "dist", "build", "runtimes", "node_modules"}


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _plain_file(path: Path) -> Path:
    # Refuse symlink components before resolve(), including a symlinked parent.
    absolute = path.absolute()
    if any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise DatasetError("SYMLINK_NOT_ALLOWED", "符号链接输入不受支持，请选择实际文件。")
    if not absolute.is_file():
        raise DatasetError("SOURCE_NOT_FOUND", "输入文件不存在或不是普通文件。")
    return absolute.resolve()


def discover_sources(paths: list[Path]) -> list[Path]:
    """Discover each SHP/TIFF separately without following symbolic links.

    Unrelated files *inside* an imported directory are ignored. An explicitly
    selected unsupported file is rejected, avoiding a silently empty task.
    """
    found: dict[str, Path] = {}
    for raw in paths:
        path = Path(raw).absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise DatasetError("SYMLINK_NOT_ALLOWED", "符号链接目录或文件不受支持。")
        if path.is_file():
            if path.suffix.lower() not in _SOURCE_SUFFIXES:
                raise DatasetError("UNSUPPORTED_FORMAT", "首版仅支持 SHP 和独立 GeoTIFF 文件。")
            resolved = path.resolve()
            found[os.path.normcase(str(resolved))] = resolved
        elif path.is_dir():
            for current, directories, filenames in os.walk(path, followlinks=False):
                current_path = Path(current)
                directories[:] = sorted(
                    d
                    for d in directories
                    if not d.startswith(".")
                    and d.lower() not in _IGNORED_DIRECTORIES
                    and not (current_path / d).is_symlink()
                )
                for name in sorted(filenames):
                    candidate = current_path / name
                    if name.startswith(".") or candidate.is_symlink():
                        continue
                    if candidate.suffix.lower() in _SOURCE_SUFFIXES:
                        resolved = candidate.resolve()
                        found[os.path.normcase(str(resolved))] = resolved
        else:
            raise DatasetError("SOURCE_NOT_FOUND", "选择的输入路径不存在。")
    if not found:
        raise DatasetError("NO_DATASETS", "未找到可发布的 SHP 或 GeoTIFF 文件。")
    return sorted(found.values(), key=lambda p: str(p).casefold())


def _components(path: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for candidate in path.parent.iterdir():
        if candidate.stem.casefold() != path.stem.casefold():
            continue
        suffix = candidate.suffix.lower()
        if suffix not in _SHP_SUFFIXES:
            continue
        if suffix in result:
            raise DatasetError("AMBIGUOUS_COMPONENT", "存在仅大小写不同的同名 SHP 组件。")
        result[suffix] = _plain_file(candidate)
    return result


def _encoding(value: str) -> str:
    value = value.strip().strip('"').removeprefix("\ufeff")
    aliases = {"65001": "UTF-8", "936": "GBK", "CP936": "GBK", "UTF8": "UTF-8"}
    value = aliases.get(value.upper(), value)
    try:
        canonical = codecs.lookup(value).name
    except (LookupError, ValueError):
        raise DatasetError("UNKNOWN_ENCODING", "编码不受支持，请明确填写 GBK、UTF-8 等有效编码。") from None
    return {"utf-8": "UTF-8", "gbk": "GBK"}.get(canonical, value)


def _bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        result = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in result) or result[0] > result[2] or result[1] > result[3]:
        return None
    return result


def _coordinate_bbox(coordinates: Any) -> list[float] | None:
    points: list[tuple[float, float]] = []

    def visit(value: Any) -> None:
        if not isinstance(value, (list, tuple)):
            return
        if len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
            points.append((float(value[0]), float(value[1])))
        else:
            for child in value:
                visit(child)

    visit(coordinates)
    if not points:
        return None
    return _bbox(
        [
            min(p[0] for p in points),
            min(p[1] for p in points),
            max(p[0] for p in points),
            max(p[1] for p in points),
        ]
    )


def _crs(coordinate_system: Any) -> str | None:
    if not isinstance(coordinate_system, dict):
        return None
    authority = coordinate_system.get("projjson", {}).get("id", {})
    if str(authority.get("authority", "")).upper() == "EPSG" and str(authority.get("code", "")).isdigit():
        return f"EPSG:{authority['code']}"
    wkt = coordinate_system.get("wkt")
    if not isinstance(wkt, str) or not wkt.strip():
        return None
    # Only a top-level authority at the end of WKT is authoritative here.
    match = re.search(r'(?:ID|AUTHORITY)\[\s*"EPSG"\s*,\s*"?(\d+)"?\s*\]\s*\]\s*$', wkt)
    return f"EPSG:{match.group(1)}" if match else wkt.strip()


class GdalRuntime:
    """A selected and optionally version-pinned external GDAL command runtime."""

    COMMANDS = ("ogrinfo", "gdalinfo", "gdaltransform", "gdalsrsinfo")

    def __init__(self, bin_dir: Path | str | None, *, expected_version: str | None = None):
        if bin_dir is None:
            packaged = Path(__file__).resolve().parent / "runtime" / "bin"
            if not packaged.is_dir():
                raise DatasetError("GDAL_NOT_CONFIGURED", "尚未配置受控 GDAL 运行时；不会自动切换系统 PATH。")
            bin_dir = packaged
        self.bin_dir = Path(bin_dir).expanduser().resolve()
        if not self.bin_dir.is_dir():
            raise DatasetError("GDAL_NOT_FOUND", "指定的 GDAL 运行时目录不存在。")
        self.expected_version = expected_version
        self.timeout = 180.0
        self._capability: dict[str, Any] | None = None

    def _executable(self, name: str) -> str:
        if name not in self.COMMANDS:
            raise DatasetError("GDAL_COMMAND_NOT_ALLOWED", "此 GDAL 命令不在预检白名单。")
        executable = self.bin_dir / (name + (".exe" if os.name == "nt" else ""))
        if not executable.is_file():
            raise DatasetError("GDAL_COMMAND_MISSING", f"受控运行时缺少 {name}。")
        return str(executable)

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        # Never inherit another GIS installation's data/plugins/configuration.
        for key in tuple(env):
            if key.startswith(("GDAL_", "PROJ_", "CPL_", "OGR_", "SHAPE_", "QGIS_")):
                del env[key]
        prefix = self.bin_dir.parent
        env.update(
            {
                "PROJ_NETWORK": "OFF",
                "GDAL_PAM_ENABLED": "NO",
                "SHAPE_RESTORE_SHX": "NO",
                "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": "",
                "GDAL_DISABLE_READDIR_ON_OPEN": "FALSE",
                "GDAL_CONFIG_FILE": os.devnull,
                "PATH": str(self.bin_dir) + os.pathsep + env.get("PATH", ""),
            }
        )
        # conda Unix prefix and conda Windows Library layout are both supported.
        for candidate in (prefix / "share" / "gdal", prefix / "Library" / "share" / "gdal"):
            if candidate.is_dir():
                env["GDAL_DATA"] = str(candidate)
                break
        for candidate in (prefix / "share" / "proj", prefix / "Library" / "share" / "proj"):
            if candidate.is_dir():
                env["PROJ_DATA"] = str(candidate)
                break
        return env

    def _run(self, command: str, arguments: list[str], *, input_text: str | None = None) -> str:
        try:
            result = subprocess.run(
                [self._executable(command), *arguments],
                input=input_text,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=self._environment(),
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise DatasetError("GDAL_TIMEOUT", f"{command} 检查超时，未修改源文件。") from None
        except OSError:
            raise DatasetError("GDAL_START_FAILED", f"{command} 无法启动，请检查受控运行时。") from None
        if result.returncode != 0:
            # Raw native stderr can expose arbitrary metadata/paths; keep it out
            # of user task reports and exception strings.
            raise DatasetError("GDAL_READ_FAILED", f"{command} 读取失败，请核对文件与运行时兼容性。")
        return result.stdout.strip()

    def _json(self, command: str, arguments: list[str]) -> dict:
        try:
            data = json.loads(self._run(command, arguments))
        except json.JSONDecodeError:
            raise DatasetError("GDAL_INVALID_RESPONSE", "GDAL 未返回可解析的结构化检查结果。") from None
        if not isinstance(data, dict):
            raise DatasetError("GDAL_INVALID_RESPONSE", "GDAL 返回了错误的数据结构。")
        return data

    def probe(self) -> dict:
        versions: set[str] = set()
        for name in self.COMMANDS:
            output = self._run(name, ["--version"])
            match = re.search(r"GDAL\s+(\d+\.\d+\.\d+)", output)
            if not match:
                raise DatasetError("GDAL_VERSION_UNKNOWN", f"无法识别 {name} 版本。")
            versions.add(match.group(1))
        if len(versions) != 1:
            raise DatasetError("GDAL_VERSION_MISMATCH", "GDAL 命令版本不一致，请使用同一套运行时。")
        version = next(iter(versions))
        if tuple(map(int, version.split("."))) < (3, 7, 0):
            raise DatasetError("GDAL_TOO_OLD", "结构化矢量预检需要 GDAL 3.7 或更新版本。")
        if self.expected_version and version != self.expected_version:
            raise DatasetError("GDAL_VERSION_MISMATCH", "当前 GDAL 与环境档案锁定版本不一致。")
        vector = self._run("ogrinfo", ["--formats"])
        raster = self._run("gdalinfo", ["--formats"])
        drivers = {
            "ESRI Shapefile": "ESRI Shapefile" in vector,
            "GTiff": bool(re.search(r"\bGTiff\b", raster)),
            "COG": bool(re.search(r"\bCOG\b", raster)),
            "VRT": bool(re.search(r"\bVRT\b", raster)),
        }
        if not drivers["ESRI Shapefile"] or not drivers["GTiff"]:
            raise DatasetError("GDAL_DRIVER_MISSING", "受控运行时缺少 SHP 或 GeoTIFF 驱动。")
        self._capability = {
            "status": "AVAILABLE",
            "version": version,
            "bin_dir": str(self.bin_dir),
            "drivers": drivers,
            "network": "OFF",
        }
        return dict(self._capability)

    def _wgs84_bbox(self, bbox: list[float] | None, crs: str | None) -> list[float] | None:
        if bbox is None or crs is None:
            return None
        if crs.upper() == "EPSG:4326":
            result = bbox
        else:
            # Densify all edges: projected rectangle corners alone can miss
            # extrema after a nonlinear coordinate transformation.
            xmin, ymin, xmax, ymax = bbox
            points = []
            for index in range(21):
                factor = index / 20
                x, y = xmin + factor * (xmax - xmin), ymin + factor * (ymax - ymin)
                points.extend(((x, ymin), (x, ymax), (xmin, y), (xmax, y)))
            output = self._run(
                "gdaltransform",
                ["-s_srs", crs, "-t_srs", "EPSG:4326"],
                input_text="\n".join(f"{x:.17g} {y:.17g}" for x, y in points) + "\n",
            )
            try:
                result = _coordinate_bbox(
                    [[float(v) for v in line.split()[:2]] for line in output.splitlines() if line.strip()]
                )
            except ValueError:
                return None
        if result is None or not (
            -180 <= result[0] <= result[2] <= 180 and -90 <= result[1] <= result[3] <= 90
        ):
            return None
        return list(result)

    def inspect(
        self, path: Path | str, *, encoding: str | None = None, declared_crs: str | None = None
    ) -> dict:
        source = _plain_file(Path(path))
        if source.suffix.lower() not in _SOURCE_SUFFIXES:
            raise DatasetError("UNSUPPORTED_FORMAT", "首版仅支持 SHP 和独立 GeoTIFF。")
        capability = self._capability or self.probe()
        kind = "shapefile" if source.suffix.lower() == ".shp" else "geotiff"
        info: dict[str, Any] = {
            "kind": kind,
            "source": str(source),
            "components": [],
            "crs": None,
            "native_bbox": None,
            "wgs84_bbox": None,
            "feature_count": None,
            "geometry_type": None,
            "fields": [],
            "band_count": None,
            "warnings": [],
            "blocking_errors": [],
            "gdal_version": capability["version"],
            "native_name": source.stem,
            "encoding": None,
        }
        if source.stat().st_size == 0:
            info["blocking_errors"].append(_issue("EMPTY_SOURCE", "输入文件为空。"))
            return info
        with source.open("rb") as stream:
            signature = stream.read(4)
        expected_signatures = (
            {b"\x00\x00\x27\x0a"} if kind == "shapefile" else {b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"}
        )
        if signature not in expected_signatures:
            info["blocking_errors"].append(_issue("INVALID_FILE_SIGNATURE", "文件头与声明格式不匹配。"))
            return info
        if declared_crs:
            declared_crs = declared_crs.strip()
            if not re.fullmatch(r"(?i)EPSG:\d+", declared_crs):
                info["blocking_errors"].append(
                    _issue("DECLARED_CRS_INVALID", "首版显式声明 CRS 请使用 EPSG 编号。")
                )
                return info
            declared_crs = declared_crs.upper()
        if kind == "shapefile":
            self._inspect_shapefile(info, source, encoding, declared_crs)
        else:
            self._inspect_geotiff(info, source, declared_crs)
        if info["native_bbox"] is None:
            info["blocking_errors"].append(
                _issue("EXTENT_UNAVAILABLE", "无法取得有效范围，不能生成可靠的地图验收请求。")
            )
        if info["crs"] and info["wgs84_bbox"] is None and info["native_bbox"] is not None:
            try:
                info["wgs84_bbox"] = self._wgs84_bbox(info["native_bbox"], info["crs"])
            except DatasetError:
                info["warnings"].append(
                    _issue("WGS84_TRANSFORM_UNAVAILABLE", "离线运行时无法转换验收范围；可使用原生 CRS 验收。")
                )
        if info["wgs84_bbox"] is None and info["native_bbox"] is not None:
            info["warnings"].append(
                _issue("WGS84_EXTENT_UNAVAILABLE", "未取得可靠的 EPSG:4326 范围，请使用原生 CRS。")
            )
        return info

    def _inspect_shapefile(
        self, info: dict, source: Path, encoding: str | None, declared_crs: str | None
    ) -> None:
        components = _components(source)
        info["components"] = [str(components[suffix]) for suffix in _SHP_SUFFIXES if suffix in components]
        for suffix in (".shp", ".shx", ".dbf"):
            if suffix not in components or components[suffix].stat().st_size == 0:
                info["blocking_errors"].append(_issue("MISSING_COMPONENT", f"缺少或为空的必要组件：{suffix}"))
        if info["blocking_errors"]:
            return
        if encoding is None and ".cpg" in components:
            try:
                encoding = components[".cpg"].read_text(encoding="utf-8-sig").strip()
            except (UnicodeError, OSError):
                info["blocking_errors"].append(
                    _issue("INVALID_CPG", "CPG 编码文件无法读取，请显式确认编码。")
                )
        if encoding:
            try:
                info["encoding"] = _encoding(encoding)
            except DatasetError as exc:
                info["blocking_errors"].append(_issue(exc.code, str(exc)))
        else:
            info["blocking_errors"].append(_issue("ENCODING_REQUIRED", "缺少有效 CPG，请显式确认 SHP 编码。"))
        arguments = ["-ro", "-json", "-so", "-al"]
        if info["encoding"]:
            arguments += ["-oo", f"ENCODING={info['encoding']}"]
        arguments += [str(source)]
        metadata = self._json("ogrinfo", arguments)
        if metadata.get("driverShortName") not in (None, "ESRI Shapefile"):
            info["blocking_errors"].append(_issue("DRIVER_MISMATCH", "文件内容不是 Shapefile。"))
        layers = metadata.get("layers", [])
        if len(layers) != 1:
            info["blocking_errors"].append(
                _issue("LAYER_COUNT_UNSUPPORTED", "一个 SHP 资源必须对应一个矢量图层。")
            )
            return
        layer = layers[0]
        geometries = layer.get("geometryFields", [])
        geometry = geometries[0] if len(geometries) == 1 else {}
        info["native_name"] = layer.get("name", source.stem)
        info["native_bbox"] = _bbox(geometry.get("extent"))
        info["crs"] = _crs(geometry.get("coordinateSystem"))
        info["geometry_type"] = geometry.get("type") or geometry.get("geometryType")
        info["feature_count"] = layer.get("featureCount")
        info["fields"] = [
            {key: field[key] for key in ("name", "type", "width", "precision", "nullable") if key in field}
            for field in layer.get("fields", [])
        ]
        if len(geometries) != 1:
            info["blocking_errors"].append(_issue("GEOMETRY_UNSUPPORTED", "图层几何字段缺失或数量不受支持。"))
        if info["feature_count"] == 0:
            info["blocking_errors"].append(_issue("EMPTY_LAYER", "空图层无法通过首版实图验收。"))
        if info["crs"] is None:
            if declared_crs:
                info["crs"] = declared_crs
                # Write this text only to the prepared ZIP, never to input.
                info["generated_prj"] = self._run("gdalsrsinfo", ["-o", "wkt_esri", declared_crs])
                info["warnings"].append(
                    _issue("CRS_ASSIGNED_TO_COPY", "按显式确认的 CRS 为发布副本生成 PRJ，不改变坐标。")
                )
            else:
                info["blocking_errors"].append(
                    _issue("CRS_REQUIRED", "无法识别原生 CRS，请提供有效 PRJ 或显式声明 EPSG。")
                )
        elif declared_crs and declared_crs != info["crs"]:
            info["blocking_errors"].append(
                _issue("CRS_CONFLICT", "显式声明与原生 CRS 不一致；首版不会自动覆盖或重投影。")
            )
        names = [field["name"] for field in info["fields"] if "name" in field]
        if len({name.casefold() for name in names}) != len(names):
            info["blocking_errors"].append(_issue("FIELD_NAME_COLLISION", "字段名存在大小写不敏感冲突。"))
        if any(len(name) >= 10 for name in names):
            info["warnings"].append(
                _issue(
                    "DBF_NAME_LIMIT", "存在接近或达到 SHP 字段长度限制的字段；无法仅凭本文件判断历史截断。"
                )
            )
        info["warnings"].append(
            _issue(
                "GEOMETRY_VALIDATION_NOT_RUN",
                "首版只读取结构与范围，未执行全量几何有效性或历史字段损失检查。",
            )
        )

    def _inspect_geotiff(self, info: dict, source: Path, declared_crs: str | None) -> None:
        info["components"] = [str(source)]
        sidecar_names = {source.name.casefold() + suffix for suffix in (".ovr", ".aux.xml", ".msk")}
        sidecar_names |= {source.stem.casefold() + suffix for suffix in (".tfw", ".tifw", ".wld", ".prj")}
        if any(item.name.casefold() in sidecar_names for item in source.parent.iterdir()):
            info["blocking_errors"].append(
                _issue(
                    "EXTERNAL_RASTER_DEPENDENCY", "影像存在外部辅助文件；请先生成包含必要信息的独立 GeoTIFF。"
                )
            )
            return
        metadata = self._json("gdalinfo", ["-json", str(source)])
        if metadata.get("driverShortName") != "GTiff":
            info["blocking_errors"].append(_issue("DRIVER_MISMATCH", "文件内容不是 GeoTIFF。"))
        info["crs"] = _crs(metadata.get("coordinateSystem"))
        if info["crs"] is None:
            message = "影像没有内嵌 CRS；首版不会仅修改服务声明，请先生成带正确 CRS 的副本。"
            info["blocking_errors"].append(_issue("RASTER_CRS_EMBED_REQUIRED", message))
        elif declared_crs and declared_crs != info["crs"]:
            info["blocking_errors"].append(
                _issue("CRS_CONFLICT", "显式声明与影像原生 CRS 不一致；首版不执行重投影。")
            )
        transform = metadata.get("geoTransform")
        size = metadata.get("size", [])
        if isinstance(transform, list) and len(transform) == 6 and len(size) == 2:
            try:
                origin_x, pixel_x, rotation_x, origin_y, rotation_y, pixel_y = map(float, transform)
                points = [
                    [origin_x + x * pixel_x + y * rotation_x, origin_y + x * rotation_y + y * pixel_y]
                    for x, y in ((0, 0), (size[0], 0), (0, size[1]), (size[0], size[1]))
                ]
                info["native_bbox"] = _coordinate_bbox(points)
            except (TypeError, ValueError):
                pass
        else:
            info["blocking_errors"].append(
                _issue("GEOTRANSFORM_REQUIRED", "缺少可用 GeoTransform；GCP/RPC 定位不属于首版范围。")
            )
        geographic_bbox = _coordinate_bbox(metadata.get("wgs84Extent", {}).get("coordinates"))
        if geographic_bbox and (
            -180 <= geographic_bbox[0] <= geographic_bbox[2] <= 180
            and -90 <= geographic_bbox[1] <= geographic_bbox[3] <= 90
        ):
            info["wgs84_bbox"] = geographic_bbox
        bands = metadata.get("bands", [])
        info["band_count"] = len(bands)
        info["raster"] = {
            "size": size,
            "geo_transform": transform,
            "bands": [
                {
                    key: band[key]
                    for key in (
                        "band",
                        "type",
                        "colorInterpretation",
                        "noDataValue",
                        "overviews",
                        "mask",
                        "colorTable",
                    )
                    if key in band
                }
                for band in bands
            ],
            "image_structure": metadata.get("metadata", {}).get("IMAGE_STRUCTURE", {}),
        }
        if not bands:
            info["blocking_errors"].append(_issue("NO_RASTER_BANDS", "影像没有可读取波段。"))
        if len(bands) > 4:
            info["warnings"].append(
                _issue(
                    "MULTISPECTRAL_STYLE_REVIEW",
                    "多光谱影像需要人工确认波段映射；简单默认样式只采用前三波段。",
                )
            )
        dependencies = [Path(p).resolve() for p in metadata.get("files", [])]
        if any(dependency != source for dependency in dependencies):
            info["blocking_errors"].append(
                _issue("EXTERNAL_RASTER_DEPENDENCY", "GDAL 检测到影像外部依赖，单文件上传会丢失信息。")
            )


def hash_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_entry(name: str) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = 0o100644 << 16
    return entry


def package_dataset(info: dict, destination: Path) -> dict:
    """Create a new prepared artifact, checking inputs have not changed mid-copy.

    `destination` may be an artifact filename or a directory. Existing destination
    files are never replaced. ZIP entries retain a safe, single native basename.
    """
    if info.get("blocking_errors"):
        raise DatasetError("PREFLIGHT_BLOCKED", "预检尚有阻断项，不能打包发布。")
    kind = info.get("kind")
    if kind not in ("shapefile", "geotiff"):
        raise DatasetError("UNSUPPORTED_FORMAT", "未知的数据资源类型。")
    source = _plain_file(Path(info["source"]))
    components = [_plain_file(Path(item)) for item in info.get("components", [])]
    if source not in components:
        raise DatasetError("INVALID_COMPONENT_MANIFEST", "数据组件清单未包含源文件。")
    if kind == "geotiff" and components != [source]:
        raise DatasetError("EXTERNAL_RASTER_DEPENDENCY", "首版只打包不含外部依赖的独立 GeoTIFF。")
    name = source.stem
    if not name or name.startswith(".") or re.search(r"[\\/:\x00-\x1f]", name):
        raise DatasetError("UNSAFE_FILENAME", "源文件名含不支持的路径或控制字符。")
    destination = Path(destination).absolute()
    expected_suffix = ".zip" if kind == "shapefile" else source.suffix.lower()
    if destination.suffix.lower() not in ({".zip"} if kind == "shapefile" else {".tif", ".tiff"}):
        destination = destination / (name + expected_suffix)
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise DatasetError("SYMLINK_NOT_ALLOWED", "发布产物目录不能通过符号链接重定向。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise DatasetError("ARTIFACT_EXISTS", "发布产物已经存在，不能覆盖。")
    source_hashes = [
        {"path": str(path), "sha256": hash_file(path), "size": path.stat().st_size} for path in components
    ]
    expected_hashes = {record["path"]: record["sha256"] for record in source_hashes}
    fd, temporary_name = tempfile.mkstemp(prefix=".geopub-", suffix=".part", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        if kind == "shapefile":
            seen: set[str] = set()
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                for component in components:
                    suffix = component.suffix.lower()
                    if (
                        component.parent != source.parent
                        or component.stem.casefold() != name.casefold()
                        or suffix not in _SHP_SUFFIXES
                    ):
                        raise DatasetError(
                            "INVALID_COMPONENT_MANIFEST", "SHP 组件必须为同目录、同名且受支持的文件。"
                        )
                    if suffix in seen:
                        raise DatasetError("DUPLICATE_COMPONENT", "数据组件清单包含重复项。")
                    seen.add(suffix)
                    if suffix == ".cpg" or (suffix == ".prj" and info.get("generated_prj")):
                        continue
                    copied_hash = hashlib.sha256()
                    with (
                        component.open("rb") as incoming,
                        archive.open(_zip_entry(name + suffix), "w", force_zip64=True) as outgoing,
                    ):
                        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                            copied_hash.update(chunk)
                            outgoing.write(chunk)
                    if copied_hash.hexdigest() != expected_hashes[str(component)]:
                        raise DatasetError("SOURCE_CHANGED", "打包过程中源组件发生变化。")
                if not {".shp", ".shx", ".dbf"}.issubset(seen):
                    raise DatasetError("MISSING_COMPONENT", "打包清单缺少必要的 SHP 组件。")
                if info.get("generated_prj"):
                    archive.writestr(_zip_entry(name + ".prj"), info["generated_prj"].encode("utf-8"))
                if info.get("encoding"):
                    archive.writestr(_zip_entry(name + ".cpg"), _encoding(info["encoding"]).encode("ascii"))
                else:
                    raise DatasetError("ENCODING_REQUIRED", "打包前必须确认编码。")
        else:
            with source.open("rb") as incoming, temporary.open("wb") as outgoing:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if hash_file(temporary) != expected_hashes[str(source)]:
                raise DatasetError("SOURCE_CHANGED", "影像复制内容与源文件摘要不一致。")
        for record in source_hashes:
            if (
                hash_file(record["path"]) != record["sha256"]
                or Path(record["path"]).stat().st_size != record["size"]
            ):
                raise DatasetError("SOURCE_CHANGED", "打包过程中源文件发生变化，产物已拒绝。")
        # A same-directory hard link installs the finished artifact atomically
        # and refuses an existing destination. Non-link filesystems use an
        # exclusive copy and remove their own partial output on failure.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            raise DatasetError("ARTIFACT_EXISTS", "目标产物已被其他任务创建。") from None
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.EINVAL}:
                raise DatasetError("ARTIFACT_INSTALL_FAILED", "无法写入任务产物目录。") from None
            created = False
            try:
                with destination.open("xb") as outgoing, temporary.open("rb") as incoming:
                    created = True
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                        outgoing.write(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
            except BaseException:
                if created:
                    destination.unlink(missing_ok=True)
                raise
        return {
            "path": str(destination.resolve()),
            "sha256": hash_file(destination),
            "size": destination.stat().st_size,
            "source_hashes": source_hashes,
            "native_name": name,
            "kind": kind,
        }
    finally:
        temporary.unlink(missing_ok=True)
