"""Offline ArcMap/QGIS integration without importing ArcPy or PyQGIS.

The user's QGIS Style Assistant 0.4.0 exports QML/SLD pairs by layer name.
GeoPublisher only accepts local SLD for service publication, and never claims
to faithfully convert binary ArcMap styles without ArcMap/QGIS software.
"""

from __future__ import annotations

import os
import sys
from importlib import resources
from pathlib import Path

from .config import ConfigError

_MAX_SLD_BYTES = 5 * 1024 * 1024


def resolve_sld_directory(source_paths: list[Path], directory: Path | str) -> dict[Path, Path]:
    """Pair files by an exact stem match; never guess or reuse unrelated styles."""
    folder = Path(directory).expanduser()
    if not folder.is_dir() or folder.is_symlink():
        raise ConfigError("SLD_DIRECTORY_MISSING", "所选 SLD 目录不存在或是符号链接。")
    folder = folder.resolve()
    candidates: dict[str, list[Path]] = {}
    for child in folder.iterdir():
        if child.suffix.casefold() != ".sld":
            continue
        if child.is_symlink() or not child.is_file():
            raise ConfigError("SLD_UNSAFE_FILE", f"SLD 目录中存在非普通文件：{child.name}")
        if child.stat().st_size <= 0 or child.stat().st_size > _MAX_SLD_BYTES:
            raise ConfigError("SLD_FILE_SIZE", f"SLD 文件为空或超过 5 MiB：{child.name}")
        candidates.setdefault(child.stem.casefold(), []).append(child)
    if not source_paths:
        raise ConfigError("SLD_NO_SOURCES", "请选择 SHP/TIFF 数据再匹配批量样式。")
    mapped = {}
    for src in source_paths:
        source = Path(src)
        if source.suffix.casefold() not in {".shp", ".tif", ".tiff"}:
            raise ConfigError("SLD_SOURCE_UNSUPPORTED", "批量匹配仅支持 SHP 或 GeoTIFF 数据文件。")
        matches = candidates.get(source.stem.casefold(), [])
        if not matches:
            raise ConfigError(
                "SLD_NOT_FOUND",
                f"没有找到与 {source.name} 同名的 SLD。请在 QGIS 插件导出并核对图层名称。",
            )
        if len(matches) != 1:
            raise ConfigError("SLD_AMBIGUOUS", f"找到多个与 {source.name} 同名的 SLD，无法安全选择。")
        mapped[source.resolve()] = matches[0]
    if len({str(item).casefold() for item in mapped.values()}) != len(mapped):
        raise ConfigError("SLD_AMBIGUOUS", "多个数据集指向同一 SLD，拒绝自动套用。")
    return mapped


def arcmap_helper_bytes(*, demo: bool = False) -> bytes:
    """Load ArcMap 10.x helper code; none of these scripts runs in GeoPublisher."""
    filename = "arcmap_create_synthetic_mxd.py" if demo else "arcmap_mxd_to_shp.py"
    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        path = root / "geopublisher" / "resources" / filename
        if path.is_file():
            return path.read_bytes()
    try:
        return resources.files("geopublisher").joinpath("resources", filename).read_bytes()
    except (OSError, FileNotFoundError, ModuleNotFoundError):
        raise ConfigError("ARCMAP_HELPER_MISSING", "打包中缺少 MXD 离线脚本，请检查程序完整性。") from None


def save_arcmap_helper(destination: Path | str, *, demo: bool = False) -> Path:
    """Export an ArcMap helper, never invoke ArcPy within GeoPublisher."""
    target = Path(destination).expanduser().absolute()
    if target.suffix.casefold() != ".py":
        raise ConfigError("ARCMAP_HELPER_SUFFIX", "请保存为 .py 脚本。")
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise ConfigError("SYMLINK_NOT_ALLOWED", "脚本输出不能是符号链接目录。")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as stream:
            stream.write(arcmap_helper_bytes(demo=demo))
    except FileExistsError:
        raise ConfigError("ARCMAP_HELPER_EXISTS", "目标脚本已经存在，拒绝覆盖。") from None
    return target


def arcmap_helper_compatible() -> bool:
    """Only a capability note. Pro/ArcMap installation is NOT inferred from PATH."""
    return os.name == "nt"
