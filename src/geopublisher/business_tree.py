"""Offline, schema-configured multi-level business catalog SQL builder.

A GeoServer layer, a source file, a scale directory, and a business node are
different things. This module ONLY writes reviewable SQL files on the local
machine. Never connects to a database/GeoServer or guesses a live parent ID.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .catalog import CatalogError, _identifier, _literal, _prelude

_SERVICE_FIELDS = {"key", "title", "kind", "platform", "url", "layer", "crs", "format"}
_NODE_FIELDS = {
    "sys", "key", "title", "parent", "kind", "display_order", "visible",
    "zoom_start", "zoom_end", "service_reference", "layer_index",
}
_OPTIONAL_FIELDS = {"memo", "organization"}
_SOURCE_KINDS = {
    "mxd": "shapefile", "gdb": "shapefile", "shp": "shapefile",
    "geotiff": "geotiff", "tiff_mosaic": "imagemosaic",
}


def _reject(code: str, message: str) -> None:
    raise CatalogError(code, message)


def _is_text(value: Any, role: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        _reject("INVALID_TREE_TEXT", f"{role} 必须是非空短文本。")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _reject("INVALID_TREE_TEXT", f"{role} 不允许控制字符。")
    return value


def _key(value: Any, role: str) -> str:
    text = _is_text(value, role)
    assert isinstance(text, str)
    if not re.fullmatch(r"[\w.:-]+", text, re.UNICODE):
        _reject("INVALID_TREE_CODE", f"{role} 只允许字母、数字、下划线、冒号、点和短横线。")
    return text


def _num(value: Any, role: str, *, max_value: int = 100000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= max_value):
        _reject("INVALID_TREE_NUMBER", f"{role} 必须是 0 到 {max_value} 之间的整数。")
    return value


def _exact_keys(value: Any, required: set[str], optional: set[str], role: str) -> dict:
    if not isinstance(value, dict):
        _reject("INVALID_TREE_CONFIG", f"{role} 应为 JSON 对象。")
    missing, extra = required - set(value), set(value) - required - optional
    if missing or extra:
        _reject("INVALID_TREE_CONFIG", f"{role} 字段缺失/多余：缺失={sorted(missing)}，多余={sorted(extra)}")
    return value


def _identifier_map(config: dict, role: str, required: set[str]) -> dict:
    config = _exact_keys(config, required, _OPTIONAL_FIELDS, role + " columns")
    seen: set[str] = set()
    for logical, actual in config.items():
        _identifier(actual)
        if actual in seen or actual.lower() in {key.lower() for key in seen}:
            _reject("DUPLICATE_TREE_COLUMN", f"{role} 存在重复字段名。")
        seen.add(actual)
    return config


def _png_evidence(value: dict) -> str:
    path_string = value.get("png")
    if not isinstance(path_string, str) or not path_string or len(path_string) > 4096:
        _reject("INVALID_WMS_EVIDENCE", "WMS PNG 证据路径无效。")
    file = Path(path_string)
    if not file.is_absolute() or str(file).startswith(("\\\\", "//")) or file.suffix.lower() != ".png":
        _reject("INVALID_WMS_EVIDENCE", "需要本机绝对 PNG 路径，不能引用 UNC、URL 或共享路径。")
    if file.is_symlink() or any(part.is_symlink() for part in file.parents):
        _reject("INVALID_WMS_EVIDENCE", "WMS 证据路径不允许符号链接。")
    if not file.is_file() or not 1024 < file.stat().st_size <= 25_000_000:
        _reject("INVALID_WMS_EVIDENCE", "WMS PNG 不存在或大小不合理。")
    try:
        with Image.open(file) as image:
            if image.format != "PNG" or not (64 <= image.width <= 4096 and 64 <= image.height <= 4096):
                _reject("INVALID_WMS_EVIDENCE", "WMS 证据必须是有效的中等分辨率 PNG。")
            image.verify()
    except (OSError, ValueError, UnidentifiedImageError):
        _reject("INVALID_WMS_EVIDENCE", "WMS PNG 无法解码。")
    return hashlib.sha256(file.read_bytes()).hexdigest().upper()


def _select_nodes(raw: dict) -> tuple[list[dict], list[dict], list[dict]]:
    existing = raw["existing_parents"]
    groups = raw["groups"]
    layers = raw["layers"]
    if not isinstance(existing, list) or not isinstance(groups, list) or not isinstance(layers, list):
        _reject("INVALID_TREE_CONFIG", "existing_parents、groups、layers 必须是数组。")
    if len(layers) < 1 or len(layers) > 1000 or len(groups) > 200 or len(existing) > 50:
        _reject("INVALID_TREE_SIZE", "每批需 1–1000 个图层，分组最多 200 个、引用父节点最多 50 个。")
    keys: set[str] = set()
    for node in existing:
        _exact_keys(node, {"code", "title"}, {"parent"}, "existing parent")
        _key(node["code"], "existing parent.code")
        _is_text(node["title"], "existing parent.title")
        if "parent" in node:
            _key(node["parent"], "existing parent.parent")
        if node["code"] in keys:
            _reject("DUPLICATE_TREE_CODE", "父节点编码重复。")
        keys.add(node["code"])
    for group in groups:
        _exact_keys(group, {"code", "title", "parent", "display_order", "layer_index"},
                    {"scale"}, "group")
        _key(group["code"], "group.code")
        _is_text(group["title"], "group.title")
        if group["parent"] is not None:
            _key(group["parent"], "group.parent")
        if "scale" in group:
            _is_text(group["scale"], "group.scale")
        _num(group["display_order"], "group.display_order")
        _num(group["layer_index"], "group.layer_index")
        if group["code"] in keys:
            _reject("DUPLICATE_TREE_CODE", "父目录、分组与图幅的编码不能重复。")
        keys.add(group["code"])
    for layer in layers:
        _exact_keys(layer, {
            "code", "title", "parent", "service_key", "qualified_name", "source_format",
            "publish_kind", "crs", "display_order", "layer_index", "zoom_start", "zoom_end",
            "wms",
        }, {"scale", "conversion_reviewed", "service_mode"}, "layer")
        for k in ("code", "service_key", "qualified_name", "parent"):
            _key(layer[k], "layer." + k)
        _is_text(layer["title"], "layer.title")
        if "scale" in layer:
            _is_text(layer["scale"], "layer.scale")
        for k in ("display_order", "layer_index", "zoom_start", "zoom_end"):
            _num(layer[k], "layer." + k)
        if layer["zoom_start"] > layer["zoom_end"]:
            _reject("INVALID_ZOOM_RANGE", "图幅 zoom_start 不能大于 zoom_end。")
        if layer["source_format"] not in _SOURCE_KINDS:
            _reject("UNKNOWN_SOURCE_FORMAT", "仅支持 mxd/gdb/shp/geotiff/tiff_mosaic 数据来源。LYR 是样式，不是独立数据图层。")
        if layer["publish_kind"] != _SOURCE_KINDS[layer["source_format"]]:
            _reject("FORMAT_ROUTE_MISMATCH", "原数据格式与 GeoServer 数据存储类型不匹配；MXD/GDB 必须先导出 SHP。")
        if "conversion_reviewed" in layer and not isinstance(layer["conversion_reviewed"], bool):
            _reject("INVALID_CONVERSION_STATE", "conversion_reviewed 必须是明确的布尔值。")
        if layer.get("service_mode", "create") not in ("create", "reuse"):
            _reject("INVALID_SERVICE_MODE", "service_mode 只能是 create 或 reuse。")
        crs = layer["crs"]
        if not isinstance(crs, str) or not re.fullmatch(r"EPSG:[1-9][0-9]{1,7}", crs):
            _reject("UNREVIEWED_CRS", "图幅必须提供明确核验的 EPSG 坐标系，例如 EPSG:4326 或 EPSG:4490。")
        if layer["code"] in keys:
            _reject("DUPLICATE_TREE_CODE", "图幅编码重复或与父目录冲突。")
        keys.add(layer["code"])
        if not layer["qualified_name"].count(":") == 1:
            _reject("INVALID_QUALIFIED_NAME", "WMS 图层名称应采用 workspace:layer。")
        _exact_keys(layer["wms"], {"status", "visual_reviewed"},
                    {"png"}, "layer.wms")
        if layer["wms"]["status"] not in ("PASSED", "PENDING"):
            _reject("INVALID_WMS_STATE", "WMS 状态只支持 PASSED 或 PENDING。")
        if not isinstance(layer["wms"]["visual_reviewed"], bool):
            _reject("INVALID_WMS_STATE", "visual_reviewed 必须明确为布尔值。")

    groups_map = {group["code"]: group for group in groups}
    allowed = {node["code"] for node in existing} | set(groups_map)
    for group in groups:
        if group["parent"] is not None and group["parent"] not in allowed:
            _reject("UNKNOWN_TREE_PARENT", f"分组 {group['code']} 的父节点未声明。")
    for layer in layers:
        if layer["parent"] not in allowed:
            _reject("UNKNOWN_TREE_PARENT", f"图幅 {layer['code']} 的父节点未声明。")
    # Sort groups in dependency order, and detect cycles even for disconnected roots.
    ordered: list[dict] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(code: str) -> None:
        if code in visited:
            return
        if code in visiting:
            _reject("CYCLIC_TREE", f"目录 {code} 存在循环父节点引用。")
        visiting.add(code)
        node = groups_map[code]
        if node["parent"] in groups_map:
            visit(node["parent"])
        visiting.remove(code)
        visited.add(code)
        ordered.append(node)

    for node in groups:
        visit(node["code"])
    for layer in layers:
        node_code = layer["parent"]
        while node_code in groups_map:
            group = groups_map[node_code]
            if "scale" in group and layer.get("scale") != group["scale"]:
                _reject("SCALE_PARENT_MISMATCH", f"图幅 {layer['code']} 的比例尺与其所在目录不一致。")
            node_code = group["parent"]
    return existing, ordered, layers


def _columns_and_row(mapping: dict, values: dict) -> tuple[list[str], list[Any]]:
    selected = [(actual, values[logical]) for logical, actual in mapping.items() if logical in values]
    return [col for col, _ in selected], [value for _, value in selected]


def _insert(table: str, mapping: dict, values: dict) -> str:
    cols, vals = _columns_and_row(mapping, values)
    return ("INSERT INTO " + _identifier(table) + " ("
            + ", ".join(_identifier(col) for col in cols) + ")\n"
            + "VALUES (" + ", ".join(_literal(v) for v in vals) + ");")


def _guard(text: list[str]) -> str:
    body = "\n".join("  " + line for line in text)
    tag = "$tree_guard$"
    while tag in body:
        tag = tag[:-1] + "x$"
    return f"DO {tag}\nBEGIN\n{body}\nEND;\n{tag};\n"


def _where_eq(columns: dict, constraints: dict) -> str:
    return " AND ".join(
        _identifier(columns[logical]) + " IS NOT DISTINCT FROM " + _literal(val)
        for logical, val in constraints.items()
    )


def _service_values(layer: dict, defaults: dict) -> dict:
    return {
        "key": layer["service_key"],
        "title": layer["title"],
        "kind": "wms",
        "platform": defaults["platform"],
        "url": defaults["wms_url"],
        "layer": layer["qualified_name"],
        "crs": layer["crs"],
        "format": "image/png",
        "memo": f"GeoServer {layer['publish_kind']}，来源 {layer['source_format']}，业务目录人工确认",
        "organization": defaults["organization"],
    }


def _node_values(node: dict, defaults: dict, *, group: bool) -> dict:
    return {
        "sys": defaults["sys"],
        "key": node["code"],
        "title": node["title"],
        "parent": node["parent"],
        "kind": None if group else "image",
        "display_order": node["display_order"],
        "visible": 0,
        "zoom_start": 0 if group else node["zoom_start"],
        "zoom_end": 0 if group else node["zoom_end"],
        "service_reference": None if group else node["service_key"],
        "layer_index": node["layer_index"],
        "memo": "新建业务分类目录" if group else "WMS 实测通过后登记的图幅节点",
        "organization": defaults["organization"],
    }


def build_business_tree_sql(config: dict, output: Path | str) -> dict:
    """Create read-only preflight, guarded registration, read-only verification and rollback review.

    A PASSED WMS value requires a locally available valid PNG and explicit
    human visual approval; a draft never produces write-capable SQL.
    """
    raw = _exact_keys(config, {
        "schema_version", "target_schema", "schema_verified", "tables",
        "columns", "defaults", "existing_parents", "groups", "layers",
    }, set(), "business tree")
    if raw["schema_version"] != 1:
        _reject("INVALID_TREE_VERSION", "只接受 business-tree schema_version=1。")
    schema = raw["target_schema"]
    _identifier(schema)
    if not isinstance(raw["schema_verified"], bool):
        _reject("INVALID_SCHEMA_STATE", "schema_verified 必须是布尔值。")
    tables = _exact_keys(raw["tables"], {"service", "node"}, set(), "tables")
    for table in tables.values():
        _identifier(table, relation=True)
    if tables["service"].lower() == tables["node"].lower():
        _reject("INVALID_TABLE_MAPPING", "服务和节点不能映射到同一张表。")
    col_roles = _exact_keys(raw["columns"], {"service", "node"}, set(), "columns")
    cols = {
        "service": _identifier_map(col_roles["service"], "service", _SERVICE_FIELDS),
        "node": _identifier_map(col_roles["node"], "node", _NODE_FIELDS),
    }
    defaults = _exact_keys(raw["defaults"],
                           {"sys", "wms_url", "workspace", "platform", "organization"}, set(), "defaults")
    _key(defaults["sys"], "defaults.sys")
    _key(defaults["workspace"], "defaults.workspace")
    _is_text(defaults["platform"], "defaults.platform")
    _num(defaults["organization"], "defaults.organization")
    wms_url = _is_text(defaults["wms_url"], "defaults.wms_url")
    assert isinstance(wms_url, str)
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+", wms_url):
        _reject("INVALID_WMS_URL", "WMS URL 只接受安全的相对服务路径，例如 geoserver/demo/wms。")
    existing, groups, layers = _select_nodes(raw)
    if any(layer["qualified_name"].split(":", 1)[0] != defaults["workspace"] for layer in layers):
        _reject("WORKSPACE_MISMATCH", "图幅的 GeoServer 工作空间与配置不一致。")
    output = Path(output).expanduser().absolute()
    if output.is_symlink() or any(path.is_symlink() for path in output.parents):
        _reject("UNSAFE_OUTPUT_PATH", "输出路径不允许符号链接。")
    if output.exists():
        _reject("OUTPUT_ALREADY_EXISTS", "请使用全新的业务目录 SQL 输出文件夹，绝不覆盖已有内容。")

    services: dict[str, tuple[str, dict]] = {}
    for layer in layers:
        key = layer["service_key"]
        mode = layer.get("service_mode", "create")
        values = _service_values(layer, defaults)
        if key in services and services[key] != (mode, values):
            _reject("CONFLICTING_SERVICE_BINDING", "一个服务名不能对应多个不同的 WMS 图层或来源。")
        services[key] = (mode, values)
    qnames = [record[1]["layer"] for record in services.values()]
    if len(qnames) != len(set(qnames)):
        _reject("DUPLICATE_WMS_LAYER", "多个服务编码指向同一个限定 WMS 名称，需要人工合并。")

    blocked: list[str] = []
    verified: list[dict] = []
    if not raw["schema_verified"]:
        blocked.append("生产业务表、序列及字段结构尚未完成只读核验")
    for layer in layers:
        wms = layer["wms"]
        if layer["source_format"] in {"mxd", "gdb"} and layer.get("conversion_reviewed") is not True:
            blocked.append(f"{layer['code']}：MXD/GDB 转换与字段/样式映射尚未人工核验")
        if wms["status"] != "PASSED" or wms["visual_reviewed"] is not True:
            blocked.append(f"{layer['code']}：真实 GeoServer GetMap 或人工视觉验收未通过")
            continue
        digest = _png_evidence(wms)
        verified.append({"code":layer["code"], "png_sha256":digest})
    service_table, node_table = tables["service"], tables["node"]
    svc_cols, node_cols = cols["service"], cols["node"]
    schema_prelude = _prelude(schema, read_only=True)
    preflight = [f"-- 多级目录前置核查（只读）；表名不带 schema 前缀。\n{schema_prelude}"]
    for table in (service_table, node_table):
        preflight.append(
            "SELECT column_name, data_type, column_default, is_nullable\n"
            "FROM information_schema.columns\n"
            f"WHERE table_schema = {_literal(schema)} AND table_name = {_literal(table)}\n"
            "ORDER BY ordinal_position;"
        )
    source_codes = [*existing, *groups, *layers]
    for node in source_codes:
        preflight.append(
            f"SELECT {_identifier(node_cols['key'])}, {_identifier(node_cols['title'])}, "
            f"{_identifier(node_cols['parent'])}, {_identifier(node_cols['sys'])}\n"
            f"FROM {_identifier(node_table)} WHERE "
            + _where_eq(node_cols, {"key":node["code"]}) + ";"
        )
    for _, svc in services.values():
        preflight.append(
            f"SELECT * FROM {_identifier(service_table)} WHERE "
            + _where_eq(svc_cols, {"key":svc["key"]}) + " OR "
            + _where_eq(svc_cols, {"layer":svc["layer"]}) + ";"
        )
    preflight.append("COMMIT;")

    node_rows = [(_node_values(node, defaults, group=True), "group") for node in groups]
    node_rows += [(_node_values(node, defaults, group=False), "layer") for node in layers]
    guard = []
    for table in (service_table, node_table):
        guard += [
            "IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            f"WHERE c.relname={_literal(table)} AND n.nspname={_literal(schema)} AND c.relkind IN ('r','p')) THEN",
            f"  RAISE EXCEPTION {_literal('The configured business table is missing: '+table)};",
            "END IF;",
        ]
        guard += [
            f"IF pg_get_serial_sequence({_literal(_identifier(table))}, 'id') IS NULL THEN",
            f"  RAISE EXCEPTION {_literal('Missing owned id sequence for '+table)};",
            "END IF;",
        ]
    for role, table in (("service", service_table), ("node", node_table)):
        expected_columns = sorted(set(cols[role].values()) | {"id"})
        expected_list = ", ".join(_literal(name) for name in expected_columns)
        guard += [
            "IF (SELECT count(*) FROM pg_attribute a "
            "JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            f"WHERE c.relname={_literal(table)} AND n.nspname={_literal(schema)} "
            f"AND NOT a.attisdropped AND a.attnum>0 AND a.attname IN ({expected_list})) "
            f"<> {len(expected_columns)} THEN",
            f"  RAISE EXCEPTION {_literal('Business table columns have changed: '+table)};",
            "END IF;",
        ]
    for node in existing:
        matched = {"key":node["code"], "title":node["title"], "sys":defaults["sys"]}
        if "parent" in node:
            matched["parent"] = node["parent"]
        guard += [
            f"IF (SELECT count(*) FROM {_identifier(node_table)} WHERE "
            + _where_eq(node_cols, matched) + ") <> 1 THEN",
            f"  RAISE EXCEPTION {_literal('Expected exactly one approved existing parent: '+node['code'])};",
            "END IF;",
        ]
    for row, role in node_rows:
        guard += [
            f"IF EXISTS (SELECT 1 FROM {_identifier(node_table)} WHERE "
            + _where_eq(node_cols, {"key":row["key"]}) + " OR ("
            + _where_eq(node_cols, {"title":row["title"], "parent":row["parent"], "sys":row["sys"]})
            + ")) THEN",
            f"  RAISE EXCEPTION {_literal('Business '+role+' code/title collision: '+row['key'])};",
            "END IF;",
        ]
    for mode, record in services.values():
        if mode == "reuse":
            guard += [
                f"IF (SELECT count(*) FROM {_identifier(service_table)} WHERE "
                + _where_eq(svc_cols, {key:record[key] for key in ("key","layer","url","crs","format")})
                + ") <> 1 THEN",
                f"  RAISE EXCEPTION {_literal('Existing service not exactly matched: '+record['key'])};",
                "END IF;",
            ]
        else:
            guard += [
                f"IF EXISTS (SELECT 1 FROM {_identifier(service_table)} WHERE "
                + _where_eq(svc_cols, {"key":record["key"]}) + " OR "
                + _where_eq(svc_cols, {"layer":record["layer"]}) + ") THEN",
                f"  RAISE EXCEPTION {_literal('Service name/WMS collision: '+record['key'])};",
                "END IF;",
            ]

    lines = [
        "-- ONLY execute after GeoServer WMS image and business schema have been verified.",
        _prelude(schema).rstrip(),
        f"LOCK TABLE {_identifier(service_table)}, {_identifier(node_table)} IN SHARE ROW EXCLUSIVE MODE;",
        _guard(guard).rstrip(),
    ]
    # Existing production sequences can lag behind manually assigned IDs.
    for table in (service_table, node_table):
        lines.append(
            f"SELECT setval(pg_get_serial_sequence({_literal(_identifier(table))}, 'id'),"
            f" COALESCE((SELECT MAX(id) FROM {_identifier(table)}),0)+1, false);"
        )
    for mode, record in services.values():
        if mode == "create":
            lines.append(_insert(service_table, svc_cols, record))
    for row, _ in node_rows:
        lines.append(_insert(node_table, node_cols, row))
    lines.append("COMMIT;")
    register = "\n\n".join(lines) + "\n"

    verify = [_prelude(schema, read_only=True)]
    for row, role in node_rows:
        verify.append(
            f"SELECT {_literal(role+':'+row['key'])} AS expected_node, COUNT(*) AS matches\n"
            f"FROM {_identifier(node_table)} WHERE " + _where_eq(node_cols, row) + ";"
        )
    for _, row in services.values():
        verify.append(
            f"SELECT {_literal('service:'+row['key'])} AS expected_service, COUNT(*) AS matches\n"
            f"FROM {_identifier(service_table)} WHERE " + _where_eq(svc_cols, row) + ";"
        )
    verify.append("COMMIT;")

    rollback = [
        "-- 人工回滚审阅：不生成 DELETE，先按精确对象归属核验是否已有其他引用。",
        _prelude(schema, read_only=True),
    ]
    for row, role in node_rows:
        rollback.append(
            f"SELECT {_literal(role+':'+row['key'])} AS rollback_candidate, *\n"
            f"FROM {_identifier(node_table)} WHERE " + _where_eq(node_cols, {"key":row["key"]}) + ";"
        )
    for mode, row in services.values():
        if mode == "create":
            rollback.append(
                f"SELECT {_literal('service:'+row['key'])} AS rollback_candidate, *\n"
                f"FROM {_identifier(service_table)} WHERE " + _where_eq(svc_cols, {"key":row["key"]}) + ";"
            )
    rollback.append("COMMIT;")

    summary = {
        "status": "SQL_GENERATED_NOT_EXECUTED" if not blocked else "BLOCKED_PENDING_REVIEW",
        "registered": False, "database_accessed": False, "geoserver_accessed": False,
        "target_schema": schema, "groups": len(groups), "layers": len(layers),
        "services_new": sum(1 for mode, _ in services.values() if mode == "create"),
        "services_reused": sum(1 for mode, _ in services.values() if mode == "reuse"),
        "existing_parent_codes": [node["code"] for node in existing],
        "blocked": blocked, "wms_evidence": verified,
        "tree": {
            "groups": [{"code":g["code"],"title":g["title"],"parent":g["parent"],
                        "scale":g.get("scale")} for g in groups],
            "layers": [{"code":layer["code"],"title":layer["title"],"parent":layer["parent"],
                        "scale":layer.get("scale"), "source_format":layer["source_format"],
                        "publish_kind":layer["publish_kind"], "service":layer["service_key"],
                        "wms_status":layer["wms"]["status"]} for layer in layers],
        },
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "01_preflight_readonly.sql").write_text("\n\n".join(preflight) + "\n", encoding="utf-8")
    name = "02_register_reviewed.sql" if not blocked else "02_register_BLOCKED.sql"
    if blocked:
        # Never emit an executable INSERT into a draft. Building a SQL preview
        # is not proof of production schema verification or live WMS acceptance.
        register = (
            "-- 阻断：不满足真实 WMS、转换或业务表前置条件，禁止登记。\n"
            + "\n".join("-- " + reason for reason in blocked) + "\n"
            + "BEGIN;\nDO $blocked$ BEGIN RAISE EXCEPTION "
            + _literal("Unverified GeoServer WMS / schema; refuse business registration")
            + "; END $blocked$;\nROLLBACK;\n"
        )
    (output / name).write_text(register, encoding="utf-8")
    (output / "03_verify_readonly.sql").write_text("\n\n".join(verify) + "\n", encoding="utf-8")
    (output / "04_rollback_review_readonly.sql").write_text(
        "\n\n".join(rollback) + "\n", encoding="utf-8"
    )
    (output / "business_tree_review.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary["output"] = str(output)
    summary["registration_sql"] = str(output / name)
    return summary
