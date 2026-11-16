"""Offline, read-only geometry coverage audit of already verified SHP sheet splits.

The human must supply an authoritative polygon layer and its exact map-sheet-ID
field. GDAL/GEOS performs full geometry predicates; bounding boxes are NOT proof.
No site credentials, database or GeoServer connections are required.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .datasets import DatasetError, GdalRuntime
from .plans import digest_file, save_json
from .sheet_split import _hashes, _identifier, _safe_path, verified_split_sources

_MAX_BOUNDARY_WKT = 250_000
_WKT_SAFE = re.compile(r"[A-Za-z0-9+\-.,()\s]+\Z")


def _deny(code: str, message: str) -> None:
    raise DatasetError(code, message)


def _rows(data: dict, *, expected_count: int | None = None) -> list[dict]:
    layers = data.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        _deny("BOUNDARY_SQL_RESULT_INVALID", "GDAL 未返回唯一的只读空间查询结果。")
    layer = layers[0]
    rows = layer.get("features")
    if not isinstance(rows, list) or (
        expected_count is not None and len(rows) != expected_count
    ):
        _deny("BOUNDARY_SQL_RESULT_INVALID", "GDAL 返回的空间查询行数不完整。")
    if type(layer.get("featureCount")) is not int or layer["featureCount"] != len(rows):
        _deny("BOUNDARY_SQL_RESULT_INVALID", "GDAL 返回的空间查询数量不一致。")
    return [item.get("properties", {}) for item in rows]


def _polygon_wkt(value: object) -> str:
    if (
        not isinstance(value, str) or len(value) > _MAX_BOUNDARY_WKT
        or not _WKT_SAFE.fullmatch(value)
        or not value.startswith(("POLYGON(", "POLYGON (", "MULTIPOLYGON(", "MULTIPOLYGON ("))
    ):
        _deny("BOUNDARY_GEOMETRY_UNSAFE", "图框面几何为空、过大或包含不支持的 WKT 字符。")
    return value


def inspect_sheet_boundaries(
    runtime: GdalRuntime, split_dir: Path | str, boundary_shp: Path | str,
    boundary_field: str, *, encoding: str | None = None,
) -> dict:
    """Compare every *actual feature geometry* against its approved sheet polygon.

    Returns an auditable summary. Geometry failures are recorded, not promoted
    into a fictitious successful map-sheet validation.
    """
    root = _safe_path(split_dir, file=False)
    children = verified_split_sources(root)
    manifest_file = root / "sheet-split-manifest.json"
    report = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    expected = {group["sheet_code"]: group for group in report["groups"]}
    if len(expected) != len(children):
        _deny("BOUNDARY_GROUP_INVALID", "分幅报告存在重复图号或图层数量不一致。")
    boundary = _safe_path(boundary_shp, file=True)
    boundary_hashes = _hashes(boundary, allow_missing_cpg=bool(encoding))
    info = runtime.inspect(boundary, encoding=encoding)
    if info.get("blocking_errors"):
        _deny("BOUNDARY_SOURCE_BLOCKED", "标准图幅面源文件未通过 GDAL 只读预检。")
    if info.get("crs") != report["source_crs"]:
        _deny("BOUNDARY_CRS_MISMATCH", "标准图幅面与已拆分图层的坐标参考系不同，请先在离线副本上核实。")
    geometry_type = info.get("geometry_type", "")
    if not isinstance(geometry_type, str) or "Polygon" not in geometry_type:
        _deny("BOUNDARY_GEOMETRY_TYPE", "标准图幅边界必须是 Polygon 或 MultiPolygon 面图层。")
    fields = [f for f in info.get("fields", [])
              if f.get("name") == boundary_field and f.get("type") == "String"]
    if len(fields) != 1:
        _deny("BOUNDARY_FIELD_INVALID", "请人工选择真实存在的字符串图幅编号字段，不接受自动推断。")
    if not isinstance(info.get("feature_count"), int) or info["feature_count"] < len(expected):
        _deny("BOUNDARY_COUNT_INCOMPLETE", "标准图幅面记录数少于所需图幅数。")

    # Only query needed sheet IDs, even if the reference catalog contains
    # thousands of unrelated sheets. Sheet codes were validated by split manifest.
    ids = sorted(expected)
    in_codes = ", ".join("'" + code + "'" for code in ids)
    sql = (
        f"SELECT {_identifier(boundary_field)} AS sheet_id, "
        "ST_AsText(geometry) AS wkt, ST_IsValid(geometry) AS valid, "
        f"ST_Area(geometry) AS area FROM {_identifier(boundary.stem)} "
        f"WHERE {_identifier(boundary_field)} IN ({in_codes})"
    )
    boundary_rows = _rows(runtime._json("ogrinfo", [
        "-ro", "-json", "-features", "-geom", "NO", "-limit", str(len(ids) + 1),
        "-dialect", "SQLITE", "-sql", sql, str(boundary),
    ]))
    polygons: dict[str, str] = {}
    for item in boundary_rows:
        code = item.get("sheet_id")
        if not isinstance(code, str) or code not in expected or code in polygons:
            _deny("BOUNDARY_DUPLICATE_ID", "图框编号重复或与待验收图幅不一致。")
        if (
            item.get("valid") != 1 or type(item.get("area")) not in (int, float)
            or not math.isfinite(item["area"]) or item["area"] <= 0
        ):
            _deny("BOUNDARY_POLYGON_INVALID", "图幅边界存在无效、零面积或无法识别的面几何。")
        polygons[code] = _polygon_wkt(item.get("wkt"))
    if set(polygons) != set(expected):
        _deny("BOUNDARY_MISSING_SHEETS", "图幅边界数据缺少一张或多张必需的真实图号。")

    results = []
    for child in children:
        match = next((g for g in report["groups"] if g["shp"] == child.name), None)
        if match is None:
            _deny("BOUNDARY_GROUP_INVALID", "分幅文件与报告行无法一一匹配。")
        code = match["sheet_code"]
        # ST_Covers includes a point lying precisely on the polygon boundary.
        # CASE treats SQL NULL and invalid geometries as nonconforming.
        geom = polygons[code]
        sql = (
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN geometry IS NOT NULL AND ST_IsValid(geometry) = 1 "
            f"AND ST_Covers(ST_GeomFromText('{geom}'), geometry) = 1 "
            "THEN 0 ELSE 1 END) AS outside "
            f"FROM {_identifier(child.stem)}"
        )
        rows = _rows(runtime._json("ogrinfo", [
            "-ro", "-json", "-features", "-geom", "NO", "-dialect", "SQLITE",
            "-sql", sql, str(child),
        ]), expected_count=1)
        actual, outside = rows[0].get("total"), rows[0].get("outside")
        if (
            type(actual) is not int or type(outside) is not int
            or actual != match["feature_count"] or not 0 <= outside <= actual
        ):
            _deny("BOUNDARY_COUNT_MISMATCH", "几何覆盖查询与已验签的分幅要素数量不一致。")
        results.append({
            "sheet_code": code, "shp": child.name,
            "feature_count": actual, "outside_count": outside,
            "status": "COVERED" if outside == 0 else "OUTSIDE_BOUNDARY",
        })
    if _hashes(boundary, allow_missing_cpg=bool(encoding)) != boundary_hashes:
        _deny("BOUNDARY_CHANGED", "标准图幅范围数据在验收时发生变化，请重新验收。")
    # Previous source components are also checked, not only the child SHP hashes.
    if verified_split_sources(root) != children:
        _deny("BOUNDARY_SPLIT_CHANGED", "已拆分成果在验收期间发生变化。")
    audit = {
        "schema_version": 1,
        "operation": "OFFLINE_SHEET_GEOMETRY_COVERAGE_ONLY",
        "status": "COVERED" if all(x["outside_count"] == 0 for x in results)
                  else "BLOCKED_OUTSIDE_BOUNDARY",
        "split_manifest": str(manifest_file),
        "split_manifest_sha256": digest_file(manifest_file),
        "boundary_shp": str(boundary),
        "boundary_field": boundary_field,
        "boundary_hashes": boundary_hashes,
        "source_crs": report["source_crs"],
        "sheet_count": len(results),
        "outside_count": sum(x["outside_count"] for x in results),
        "groups": sorted(results, key=lambda item: item["sheet_code"]),
        "geological_completeness_reviewed": False,
        "topology_reviewed": False,
        "wms_reviewed": False,
        "database_registered": False,
        "no_server_operation": True,
    }
    return audit


def save_sheet_boundary_audit(audit: dict, output: Path | str) -> dict:
    """Write a new local JSON result; both pass and block states are audit-worthy."""
    path = _safe_path(output, file=False)
    if path.exists() or not path.parent.is_dir():
        _deny("BOUNDARY_OUTPUT_EXISTS", "请使用全新本地审核目录，不能覆盖已有结果。")
    source_dirs = (
        Path(audit["boundary_shp"]).parent,
        Path(audit["split_manifest"]).parent,
    )
    if any(path == parent or path.is_relative_to(parent) for parent in source_dirs):
        _deny("BOUNDARY_OUTPUT_UNSAFE", "审计目录不能放在原图幅、已拆分成果文件夹内。")
    path.mkdir()
    save_json(path / "boundary-review.json", audit)
    return {
        "status": audit["status"], "report": str(path / "boundary-review.json"),
        "sheet_count": audit["sheet_count"], "outside_count": audit["outside_count"],
    }
