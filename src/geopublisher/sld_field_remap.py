"""Explicit, auditable SLD field-reference repair after GDB -> SHP export.

No GIS source files are modified. Only direct OGC PropertyName and FES
ValueReference leaf elements can be replaced, after verifying the local
conversion-report and the actual SHP sidecar hashes. Complex expressions are
intentionally not converted automatically.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from .config import ConfigError
from .styles import OGC, SE, SLD, validate_sld

_MAX_REPORT_BYTES = 16 * 1024 * 1024
_MAX_STYLE_BYTES = 5 * 1024 * 1024
_FES = "http://www.opengis.net/fes/2.0"
_PROPERTY_TAGS = {f"{{{OGC}}}PropertyName", f"{{{_FES}}}ValueReference"}
_COMPLEX_ANCESTORS = {
    "Function", "Add", "Sub", "Mul", "Div", "Recode", "Categorize",
    "Interpolate", "Expression", "Concatenate",
}
_SHORT_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,9}\Z")
_SAFE_DIGEST = re.compile(r"[0-9a-fA-F]{64}\Z")

ET.register_namespace("sld", SLD)
ET.register_namespace("ogc", OGC)
ET.register_namespace("se", SE)
ET.register_namespace("fes", _FES)


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _local_regular(path: Path) -> Path:
    path = path.absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ConfigError("FIELD_MAP_SYMLINK", "字段映射和 SHP 不能位于符号链接路径。")
    if not path.is_file():
        raise ConfigError("FIELD_MAP_NOT_FOUND", "请在 GDB 转换输出目录找到 conversion-report.json。")
    return path


def load_reviewed_field_map(shp: Path | str, *, fields: list[dict]) -> dict:
    """Return *verified* original->DBF aliases for one converted SHP.

    A user clicking the explicit checkbox only approves a locally generated
    report tied by SHA256 to the exact SHP and sidecars; an arbitrary CSV or
    stale report is not a sufficient authorization.
    """
    source = _local_regular(Path(shp))
    if source.suffix.casefold() != ".shp":
        raise ConfigError("FIELD_MAP_SHAPE_ONLY", "字段重映射仅支持 GDB 导出的 SHP。")
    directory = source.parent
    report_path = _local_regular(directory / "conversion-report.json")
    if not 0 < report_path.stat().st_size <= _MAX_REPORT_BYTES:
        raise ConfigError("FIELD_MAP_SIZE", "字段对照报告为空或过大。")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise ConfigError("FIELD_MAP_INVALID", "无法解析 GDB 转换报告，请重新执行离线转换。") from None
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or report.get("operation") != "GDB_TO_SHP_LOCAL_ONLY"
        or report.get("source_unchanged") is not True
        or report.get("gdal_version") != "3.13.3"
        or not isinstance(report.get("layers"), list)
        or not isinstance(report.get("destination"), str)
        or Path(report["destination"]).resolve() != directory.resolve()
    ):
        raise ConfigError("FIELD_MAP_UNTRUSTED", "字段映射来源不符合受控 GDB 转换记录。")
    matching = [
        row for row in report["layers"]
        if isinstance(row, dict)
        and isinstance(row.get("output_basename"), str)
        and row["output_basename"].casefold() == source.stem.casefold()
    ]
    if len(matching) != 1:
        raise ConfigError("FIELD_MAP_AMBIGUOUS", "找不到该 SHP 的唯一原始 GDB 图层对照。")
    row = matching[0]
    if row.get("status") not in {"EXPORTED", "EXPORTED_REVIEW_REQUIRED"}:
        raise ConfigError("FIELD_MAP_UNVERIFIED", "该图层转换未成功，不能套用字段映射。")
    files = row.get("output_files")
    expected = {source.stem.casefold() + suffix for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg")}
    if (
        not isinstance(files, list)
        or len(files) != 5
        or {str(item.get("name", "")).casefold() for item in files if isinstance(item, dict)} != expected
    ):
        raise ConfigError("FIELD_MAP_INCOMPLETE", "字段映射缺少 SHP 配套文件的完整校验记录。")
    for item in files:
        name = item["name"]
        sha = item.get("sha256")
        if not isinstance(sha, str) or not _SAFE_DIGEST.fullmatch(sha):
            raise ConfigError("FIELD_MAP_HASH_INVALID", "GDB 转换报告包含无效文件校验值。")
        file = _local_regular(directory / name)
        if file.parent != directory or _file_sha(file).casefold() != sha.casefold():
            raise ConfigError("FIELD_MAP_SOURCE_CHANGED", "SHP 文件已改变，需重新核对字段或重新转换。")
    actual = [
        field.get("name") for field in fields
        if isinstance(field, dict)
    ]
    if not actual or any(not isinstance(name, str) for name in actual):
        raise ConfigError("FIELD_MAP_FIELDS_UNKNOWN", "无法核实当前 SHP 的字段目录。")
    if len({name.casefold() for name in actual}) != len(actual):
        raise ConfigError("FIELD_MAP_FIELDS_AMBIGUOUS", "当前 SHP 字段名存在大小写冲突。")
    source_fields = row.get("field_map")
    if not isinstance(source_fields, list) or not source_fields:
        raise ConfigError("FIELD_MAP_INVALID", "GDB 转换报告缺少字段映射列表。")
    original_to_alias = {}
    seen_targets = set()
    seen_originals = set()
    for item in source_fields:
        if not isinstance(item, dict):
            raise ConfigError("FIELD_MAP_INVALID", "转换报告存在无效字段记录。")
        original, alias = item.get("original"), item.get("shp_field")
        if not isinstance(original, str) or not original or "\0" in original:
            raise ConfigError("FIELD_MAP_INVALID", "转换报告存在无效原始字段名。")
        if not isinstance(alias, str) or not _SHORT_FIELD.fullmatch(alias):
            raise ConfigError("FIELD_MAP_INVALID", "转换目标字段名不符合 SHP 限制。")
        if original.casefold() in seen_originals or alias.casefold() in seen_targets:
            raise ConfigError("FIELD_MAP_COLLISION", "原字段或转换字段存在重复，拒绝自动修复样式。")
        seen_originals.add(original.casefold())
        seen_targets.add(alias.casefold())
        if not item.get("omitted"):
            original_to_alias[original] = alias
    if {name.casefold() for name in original_to_alias.values()} != {name.casefold() for name in actual}:
        raise ConfigError("FIELD_MAP_FIELDS_CHANGED", "报告中的字段与实际 SHP 字段不一致。")
    return {
        "source": str(source),
        "original_to_shp": original_to_alias,
        "shp_fields": actual,
        "report_sha256": _file_sha(report_path),
        "shp_sha256": _file_sha(source),
        "review_notice": "本次只修正简单字段引用；分类值、字体、表达式和图例仍需人工对照。",
    }


def rewrite_sld_field_references(
    original_sld: Path | str,
    output_sld: Path | str,
    mapping: dict,
) -> dict:
    """Copy and rewrite direct field refs only; no in-place mutation or network."""
    style = _local_regular(Path(original_sld))
    target = Path(output_sld).absolute()
    if target.exists() or target == style or target.is_symlink():
        raise ConfigError("SLD_REWRITE_EXISTS", "只能生成全新的计划样式副本，不能覆盖源 SLD。")
    if not 0 < style.stat().st_size <= _MAX_STYLE_BYTES:
        raise ConfigError("SLD_REWRITE_SIZE", "源 SLD 文件为空或超过 5 MiB。")
    before = validate_sld(style)
    if not before["valid"]:
        raise ConfigError("SLD_REWRITE_UNSAFE", "原始 SLD 预检存在阻断，禁止自动修改。")
    data = style.read_bytes()
    try:
        root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ET.ParseError, DefusedXmlException, ValueError):
        raise ConfigError("SLD_REWRITE_XML", "SLD 包含无效或不允许的 XML。") from None
    reference_map = mapping["original_to_shp"]
    actual = {field.casefold() for field in mapping["shp_fields"]}
    changes = []
    seen = set()

    def simple_property_nodes(parent: ET.Element, in_expression: bool = False):
        ancestry = in_expression or parent.tag.rsplit("}", 1)[-1] in _COMPLEX_ANCESTORS
        if parent.tag in _PROPERTY_TAGS:
            if ancestry:
                raise ConfigError(
                    "SLD_COMPLEX_REFERENCE",
                    "样式引用位于函数或复杂表达式中，必须在 QGIS 中人工修改。",
                )
            yield parent
        for child in parent:
            yield from simple_property_nodes(child, ancestry)

    for node in simple_property_nodes(root):
        if len(node) or node.attrib or not node.text or (node.tail and node.tail.strip()):
            raise ConfigError(
                "SLD_COMPLEX_REFERENCE",
                "样式包含复杂字段表达式，只能在 QGIS 中手工核对，禁止自动替换。",
            )
        field = node.text.strip()
        if not field or any(part in field for part in ("/", ":", "[", "]", "(", ")", "*")):
            raise ConfigError(
                "SLD_COMPLEX_REFERENCE",
                "检测到 XPath/复杂属性表达式，不能按短字段名直接替换。",
            )
        target_name = reference_map.get(field)
        if target_name is None:
            if field.casefold() not in actual:
                raise ConfigError(
                    "SLD_FIELD_UNKNOWN", "样式引用的字段在当前 SHP 与映射报告中均不存在。",
                )
            continue
        if field.casefold() in actual and field.casefold() != target_name.casefold():
            raise ConfigError("SLD_FIELD_AMBIGUOUS", "原字段名也出现在 SHP 中，字段含义不唯一。")
        if target_name != field:
            node.text = target_name
            if field not in seen:
                changes.append({"original": field, "shp_field": target_name})
                seen.add(field)
    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if len(payload) > _MAX_STYLE_BYTES:
        raise ConfigError("SLD_REWRITE_SIZE", "生成的样式超出 5 MiB 限制。")
    if target.parent.is_symlink():
        raise ConfigError("SLD_REWRITE_SYMLINK", "计划样式目录不能是符号链接。")
    with target.open("xb") as stream:
        stream.write(payload)
    validated = validate_sld(target, fields=mapping["shp_fields"])
    if not validated["valid"]:
        target.unlink(missing_ok=True)
        raise ConfigError("SLD_REWRITE_INVALID", "转换后 SLD 与实际 SHP 字段无法匹配。")
    return {
        "explicitly_approved": True,
        "original_sld_sha256": hashlib.sha256(data).hexdigest(),
        "mapped_sld_sha256": hashlib.sha256(payload).hexdigest(),
        "conversion_report_sha256": mapping["report_sha256"],
        "source_shp_sha256": mapping["shp_sha256"],
        "changes": changes,
        "requires_visual_review": True,
        "notice": mapping["review_notice"],
    }
