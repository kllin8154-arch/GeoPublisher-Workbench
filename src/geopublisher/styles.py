"""Conservative SLD validation and deterministic starter styles."""

from __future__ import annotations

import hashlib
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

SLD = "http://www.opengis.net/sld"
SE = "http://www.opengis.net/se"
OGC = "http://www.opengis.net/ogc"
XLINK = "http://www.w3.org/1999/xlink"
ET.register_namespace("sld", SLD)
ET.register_namespace("ogc", OGC)


class StyleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def validate_sld(path: Path, *, fields: list[Any] | None = None) -> dict:
    """Validate supported SLD structure and dependencies without fetching URLs.

    This is a compatibility preflight, not a claim of full OGC XSD validation or
    pixel-identical rendering. The published layer still requires WMS verification.
    """
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise StyleError("STYLE_NOT_FOUND", "样式文件不存在或为符号链接。")
    if path.stat().st_size > 5 * 1024 * 1024:
        raise StyleError("STYLE_TOO_LARGE", "SLD 超过首版 5 MiB 上限。")
    with path.open("rb") as stream:
        content = stream.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise StyleError("STYLE_TOO_LARGE", "SLD 超过首版 5 MiB 上限。")
    report = {
        "valid": False,
        "version": None,
        "sha256": hashlib.sha256(content).hexdigest(),
        "field_references": [],
        "external_resources": [],
        "scale_ranges": [],
        "symbolizers": [],
        "warnings": [],
        "blocking_errors": [],
    }
    try:
        root = SafeET.fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ET.ParseError, DefusedXmlException, ValueError):
        report["blocking_errors"].append(_issue("INVALID_SLD_XML", "SLD XML 无效，或包含禁止的 DTD/实体。"))
        return report
    if root.tag != f"{{{SLD}}}StyledLayerDescriptor" or root.attrib.get("version") not in {"1.0.0", "1.1.0"}:
        report["blocking_errors"].append(
            _issue("SLD_VERSION_UNSUPPORTED", "只接受 SLD 1.0.0/1.1.0 StyledLayerDescriptor。")
        )
        return report
    report["version"] = root.attrib["version"]
    references: set[str] = set()
    symbols: set[str] = set()
    external: set[str] = set()
    allowed_symbolizers = {
        "PointSymbolizer",
        "LineSymbolizer",
        "PolygonSymbolizer",
        "TextSymbolizer",
        "RasterSymbolizer",
    }
    for node in root.iter():
        local = _local(node.tag)
        if local in {"ExternalGraphic", "ExternalMark", "OnlineResource", "RemoteOWS", "InlineContent"}:
            external.add(local)
        if any(_local(attribute).lower() in {"href", "src", "url"} for attribute in node.attrib):
            external.add(local)
        if local in {"PropertyName", "ValueReference"}:
            value = "".join(node.itertext()).strip()
            if value:
                references.add(value)
        if local.endswith("Symbolizer"):
            if local not in allowed_symbolizers or not node.tag.startswith((f"{{{SLD}}}", f"{{{SE}}}")):
                report["blocking_errors"].append(
                    _issue("SYMBOLIZER_UNSUPPORTED", "样式包含首版不支持的符号化元素。")
                )
            symbols.add(local)
        if local == "Rule":
            limits = {}
            for child in node:
                child_name = _local(child.tag)
                if child_name in {"MinScaleDenominator", "MaxScaleDenominator"}:
                    try:
                        number = float(child.text or "")
                        if not math.isfinite(number) or number < 0:
                            raise ValueError
                        limits[child_name] = number
                    except ValueError:
                        report["blocking_errors"].append(
                            _issue("INVALID_SCALE_RANGE", "样式比例尺范围包含无效数值。")
                        )
            if limits.get("MinScaleDenominator", 0) >= limits.get("MaxScaleDenominator", float("inf")):
                report["blocking_errors"].append(
                    _issue("INVALID_SCALE_RANGE", "样式最小比例尺分母必须小于最大值。")
                )
            if limits:
                report["scale_ranges"].append(limits)
    report["field_references"] = sorted(references)
    report["external_resources"] = sorted(external)
    report["symbolizers"] = sorted(symbols)
    if external:
        report["blocking_errors"].append(
            _issue("EXTERNAL_STYLE_RESOURCE", "首版不接受外部图片、字体符号链接或远程样式资源。")
        )
    if not symbols:
        report["blocking_errors"].append(_issue("NO_SYMBOLIZER", "样式中没有可用于地图验收的符号化元素。"))
    if fields is not None:
        names = {field if isinstance(field, str) else field.get("name") for field in fields}
        missing = sorted(references - names)
        if missing:
            # Field names are task metadata, not credentials; retain exact names
            # to make an actionable compatibility report.
            report["blocking_errors"].append(
                _issue("STYLE_FIELD_MISSING", "样式引用不存在的字段：" + ", ".join(missing))
            )
    if "TextSymbolizer" in symbols:
        report["warnings"].append(
            _issue("SERVER_FONT_REVIEW", "标注字体需在目标 GeoServer 环境确认，最终以实图为准。")
        )
    if report["scale_ranges"]:
        report["warnings"].append(
            _issue("SCALE_RANGE_REVIEW", "样式限制比例尺；验收范围需落在可见比例尺内。")
        )
    report["valid"] = not report["blocking_errors"]
    return report


def _element(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    node = ET.SubElement(parent, f"{{{SLD}}}{tag}", attrs)
    if text is not None:
        node.text = text
    return node


def _css(parent: ET.Element, name: str, value: str) -> None:
    _element(parent, "CssParameter", value, name=name)


def write_default_sld(
    path: Path, *, name: str, geometry_type: str | None = None,
    band_count: int | None = None, alpha_band: int | None = None
) -> Path:
    """Create a minimal SLD; never replace a different existing style."""
    if not name or any(ord(char) < 32 for char in name):
        raise StyleError("INVALID_STYLE_NAME", "样式名称为空或包含控制字符。")
    root = ET.Element(f"{{{SLD}}}StyledLayerDescriptor", {"version": "1.0.0"})
    layer = _element(root, "NamedLayer")
    _element(layer, "Name", name)
    style = _element(layer, "UserStyle")
    _element(style, "Title", name)
    feature_style = _element(style, "FeatureTypeStyle")
    rule = _element(feature_style, "Rule")
    if band_count is not None:
        if band_count < 1:
            raise StyleError("NO_RASTER_BANDS", "无法为无波段影像生成默认样式。")
        raster = _element(rule, "RasterSymbolizer")
        _element(raster, "Opacity", "1.0")
        # ChannelSelection of RGB only strips the fourth native Alpha band on
        # GeoServer 2.24.4. Let native RGBA rendering retain its transparency.
        # Other multi-band images continue using explicit RGB channels.
        if alpha_band is not None and not 1 <= alpha_band <= band_count:
            raise StyleError("INVALID_ALPHA_BAND", "Alpha 波段编号与影像波段数量不一致。")
        if band_count >= 3 and not (band_count == 4 and alpha_band == 4):
            channels = _element(raster, "ChannelSelection")
            for color, band in (("RedChannel", 1), ("GreenChannel", 2), ("BlueChannel", 3)):
                channel = _element(channels, color)
                _element(channel, "SourceChannelName", str(band))
    else:
        geometry = (geometry_type or "").lower()
        if "point" in geometry:
            symbol = _element(rule, "PointSymbolizer")
            graphic = _element(symbol, "Graphic")
            mark = _element(graphic, "Mark")
            _element(mark, "WellKnownName", "circle")
            fill = _element(mark, "Fill")
            _css(fill, "fill", "#356F9B")
            stroke = _element(mark, "Stroke")
            _css(stroke, "stroke", "#FFFFFF")
            _css(stroke, "stroke-width", "1")
            _element(graphic, "Size", "8")
        elif "line" in geometry or "curve" in geometry:
            symbol = _element(rule, "LineSymbolizer")
            stroke = _element(symbol, "Stroke")
            _css(stroke, "stroke", "#356F9B")
            _css(stroke, "stroke-width", "2")
        elif "polygon" in geometry or "surface" in geometry:
            symbol = _element(rule, "PolygonSymbolizer")
            fill = _element(symbol, "Fill")
            _css(fill, "fill", "#8BAF93")
            _css(fill, "fill-opacity", "0.7")
            stroke = _element(symbol, "Stroke")
            _css(stroke, "stroke", "#365A43")
            _css(stroke, "stroke-width", "1")
        else:
            raise StyleError("GEOMETRY_STYLE_UNSUPPORTED", "无法为未知或混合几何类型选择默认样式。")
    ET.indent(root, space="  ")
    content = ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise StyleError("SYMLINK_NOT_ALLOWED", "样式产物路径不能使用符号链接。")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if not path.is_file() or path.read_bytes() != content:
            raise StyleError("STYLE_ALREADY_EXISTS", "目标样式已存在且内容不同，拒绝覆盖。") from None
    return path
