"""Generate reviewed PostgreSQL catalog scripts without connecting to a DB.

The generator knows no site-specific schema. Registration is create-only (or
explicitly verified service reuse); rollback is deliberately read-only because
a generated SQL file is not evidence that its INSERT statements ran.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class CatalogError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_SERVICE_SOURCES = {"task_id", "wms_url", "service_token", "service_index"}
_LAYER_SOURCES = {"task_id", "qualified_name", "wms_url", "title", "crs", "layer_index", "service_key"}


def _identifier(value: Any, *, relation: bool = False) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise CatalogError(
            "INVALID_SQL_IDENTIFIER", "数据库标识符只允许 1–63 位 ASCII 字母、数字及下划线，不接受模式前缀。"
        )
    if relation and (value.lower().startswith("pg_") or value.lower() == "information_schema"):
        raise CatalogError("SYSTEM_SCHEMA_NOT_ALLOWED", "不能向系统模式或系统关系生成业务注册脚本。")
    return '"' + value + '"'


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CatalogError("INVALID_SQL_VALUE", "SQL 值不能使用 NaN 或无限数值。")
        return repr(value)
    if not isinstance(value, str) or len(value) > 16384 or "\0" in value:
        raise CatalogError("INVALID_SQL_VALUE", "SQL 值必须为有限数值、布尔、NULL 或不含空字符的短文本。")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise CatalogError("INVALID_SQL_VALUE", "SQL 文本含无效 Unicode 字符。") from None
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "''")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return "E'" + escaped + "'"


def _keys(value: dict, allowed: set[str], name: str) -> None:
    unexpected = set(value) - allowed
    if unexpected:
        raise CatalogError(
            "UNKNOWN_MAPPING_OPTION", f"{name} 存在未知配置项，拒绝忽略：" + ", ".join(sorted(unexpected))
        )


def _table(mapping: dict, role: str) -> dict:
    value = mapping.get(role)
    if not isinstance(value, dict):
        raise CatalogError("TABLE_MAPPING_REQUIRED", f"缺少 {role} 表映射。")
    allowed = {"table", "key", "columns", "unique_key_verified"}
    if role == "service":
        allowed.add("mode")
    else:
        allowed |= {"service_reference_column", "service_reference_verified"}
    _keys(value, allowed, role)
    _identifier(value.get("table"), relation=True)
    _identifier(value.get("key"))
    if value.get("unique_key_verified") is not True:
        raise CatalogError("UNVERIFIED_UNIQUE_KEY", f"请先核实 {role} 的单字段主键或唯一键。")
    columns = value.get("columns")
    if not isinstance(columns, dict) or not columns or len(columns) > 128:
        raise CatalogError("COLUMNS_REQUIRED", "字段映射必须为非空对象，且不超过 128 个字段。")
    if value["key"] not in columns:
        raise CatalogError("KEY_VALUE_REQUIRED", f"{role} 唯一键必须提供明确的值映射。")
    allowed_sources = _SERVICE_SOURCES if role == "service" else _LAYER_SOURCES
    for column, specification in columns.items():
        _identifier(column)
        if not isinstance(specification, dict) or len(specification) != 1:
            raise CatalogError("INVALID_COLUMN_MAPPING", "每个字段只能指定 source 或 value 其中一项。")
        if set(specification) == {"source"}:
            if not isinstance(specification["source"], str) or specification["source"] not in allowed_sources:
                raise CatalogError("UNKNOWN_VALUE_SOURCE", f"{role} 字段使用了不支持的来源。")
        elif set(specification) == {"value"}:
            _literal(specification["value"])
        else:
            raise CatalogError("SQL_EXPRESSION_NOT_ALLOWED", "不接受原始 SQL 表达式、序列函数或脚本字段。")
    result = dict(value)
    if role == "service":
        result["mode"] = value.get("mode", "create")
        if result["mode"] not in {"create", "reuse-verified"}:
            raise CatalogError("INVALID_SERVICE_MODE", "service.mode 只能为 create 或 reuse-verified。")
    else:
        reference = value.get("service_reference_column")
        _identifier(reference)
        if value.get("service_reference_verified") is not True:
            raise CatalogError("UNVERIFIED_RELATION", "请先核实图层表到服务唯一键的关联字段。")
        if columns.get(reference) != {"source": "service_key"}:
            raise CatalogError("INVALID_SERVICE_REFERENCE", "图层关联字段必须使用 service_key 来源。")
    return result


def _row(table: dict, context: dict) -> dict:
    row = {
        name: spec["value"] if "value" in spec else context[spec["source"]]
        for name, spec in table["columns"].items()
    }
    for value in row.values():
        _literal(value)
    key = row[table["key"]]
    if isinstance(key, bool) or not isinstance(key, (str, int)) or key == "":
        raise CatalogError("INVALID_KEY_VALUE", "唯一键值必须为非空文本或整数；不允许自动分配或 NULL。")
    return row


def _predicate(row: dict, *, alias: str | None = None) -> str:
    prefix = _identifier(alias) + "." if alias else ""
    return " AND ".join(
        prefix + _identifier(name) + " IS NOT DISTINCT FROM " + _literal(value) for name, value in row.items()
    )


def _key_predicate(table: dict, row: dict, *, alias: str | None = None) -> str:
    return _predicate({table["key"]: row[table["key"]]}, alias=alias)


def _insert(table: dict, row: dict) -> str:
    return (
        "INSERT INTO "
        + _identifier(table["table"])
        + " ("
        + ", ".join(map(_identifier, row))
        + ")\n"
        + "  VALUES ("
        + ", ".join(_literal(value) for value in row.values())
        + ");"
    )


def _do(body: str) -> str:
    # SQL string escaping alone cannot protect the outer dollar-quoted DO
    # body; choose a delimiter absent from the entire generated body.
    label = "geopub_catalog"
    delimiter = f"${label}$"
    while delimiter in body:
        label += "_x"
        delimiter = f"${label}$"
    return f"DO {delimiter}\nBEGIN\n{body}\nEND;\n{delimiter};\n"


def _prelude(schema: str, *, read_only: bool = False) -> str:
    return (
        "BEGIN" + (" READ ONLY" if read_only else "") + ";\n"
        "SET LOCAL lock_timeout = '5s';\n"
        "SET LOCAL statement_timeout = '60s';\n"
        "SET LOCAL standard_conforming_strings = on;\n"
        # pg_temp is explicitly last so a temporary table cannot silently
        # shadow the reviewed business table; functions resolve in pg_catalog.
        f"SET LOCAL search_path = pg_catalog, {_identifier(schema)}, pg_temp;\n\n"
    )


def _schema_check(table: dict, schema: str) -> str:
    name, key = table["table"], table["key"]
    regclass = "to_regclass(" + _literal(_identifier(name)) + ")"
    return (
        "  IF NOT EXISTS (\n"
        "    SELECT 1 FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace\n"
        f"    WHERE c.oid = {regclass} AND n.nspname = {_literal(schema)}\n"
        "      AND c.relkind IN ('r', 'p')\n"
        "  ) THEN\n"
        f"    RAISE EXCEPTION {_literal('Reviewed table is missing or resolves to a different schema: ' + name)};\n"
        "  END IF;\n"
        "  IF NOT EXISTS (\n"
        "    SELECT 1 FROM pg_index AS i JOIN pg_attribute AS a\n"
        "      ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]\n"
        f"    WHERE i.indrelid = {regclass} AND a.attname = {_literal(key)}\n"
        "      AND i.indisunique AND i.indisvalid AND i.indnkeyatts = 1\n"
        "      AND i.indpred IS NULL AND i.indexprs IS NULL AND NOT a.attisdropped\n"
        "  ) THEN\n"
        f"    RAISE EXCEPTION {_literal('Required non-partial unique key is missing: ' + name + '/' + key)};\n"
        "  END IF;"
    )


def _verification(table: dict, row: dict, label: str) -> str:
    match = _predicate(row)
    return (
        f"SELECT {_literal(label)} AS resource,\n"
        "       COUNT(*) AS matching_key_count,\n"
        f"       COUNT(*) FILTER (WHERE {match}) AS matching_value_count,\n"
        "       CASE WHEN COUNT(*) = 0 THEN 'MISSING'\n"
        f"            WHEN COUNT(*) = 1 AND COUNT(*) FILTER (WHERE {match}) = 1 THEN 'MATCHED'\n"
        "            ELSE 'MISMATCH' END AS verification_status\n"
        f"FROM {_identifier(table['table'])}\n"
        f"WHERE {_key_predicate(table, row)};"
    )


def _validate_bindings(bindings: dict) -> tuple[list[dict], str]:
    if not isinstance(bindings, dict) or bindings.get("schema_version") != 1:
        raise CatalogError("BINDINGS_VERSION_UNSUPPORTED", "只支持版本 1 的 catalog-bindings.json。")
    if bindings.get("registered") is not False:
        raise CatalogError("BINDINGS_ALREADY_REGISTERED", "输入必须明确为尚未登记的图层绑定清单。")
    layers = bindings.get("layers")
    if not isinstance(layers, list) or not layers or len(layers) > 1000:
        raise CatalogError("NO_CATALOG_LAYERS", "清单须包含 1–1000 个已验收图层。")
    tasks: set[str] = set()
    qualified: set[tuple[str, str]] = set()
    for layer in layers:
        if not isinstance(layer, dict):
            raise CatalogError("INVALID_BINDING", "图层绑定必须为对象。")
        for field in ("task_id", "qualified_name", "wms_url", "crs", "title"):
            if not isinstance(layer.get(field), str) or not layer[field].strip():
                raise CatalogError("INVALID_BINDING", f"图层绑定缺少有效字段：{field}")
            _literal(layer[field])
        task_id = layer["task_id"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", task_id):
            raise CatalogError("INVALID_TASK_ID", "任务标识格式不合法。")
        tasks.add(task_id)
        try:
            url = urlsplit(layer["wms_url"])
            valid = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and not (url.username or url.password or url.query or url.fragment)
            )
        except ValueError:
            valid = False
        if not valid:
            raise CatalogError(
                "INVALID_SERVICE_URL", "WMS URL 必须为不含凭据、查询串和片段的 HTTP(S) 服务地址。"
            )
        identity = (layer["wms_url"], layer["qualified_name"])
        if identity in qualified:
            raise CatalogError("DUPLICATE_BINDING", "绑定清单包含重复图层。")
        qualified.add(identity)
    if len(tasks) != 1:
        raise CatalogError("MIXED_TASK_BINDINGS", "一次目录注册只接受同一发布任务的图层。")
    return layers, next(iter(tasks))


def generate_catalog(bindings: dict, mapping: dict, output_dir: Path) -> dict:
    """Write create/verify/manual-rollback scripts; never connect or execute SQL."""
    layers, task_id = _validate_bindings(bindings)
    if not isinstance(mapping, dict):
        raise CatalogError("MAPPING_REQUIRED", "请提供经人工核验的目录字段映射。")
    _keys(
        mapping,
        {
            "schema_version",
            "database",
            "target_schema",
            "schema_verified",
            "service",
            "layer",
            "layer_order_start",
        },
        "mapping",
    )
    if mapping.get("schema_version") != 1 or mapping.get("database") != "postgresql":
        raise CatalogError("MAPPING_VERSION_UNSUPPORTED", "首版仅支持 PostgreSQL 的版本 1 映射。")
    if mapping.get("schema_verified") is not True:
        raise CatalogError(
            "SCHEMA_NOT_VERIFIED", "请先人工核验实际数据库结构；示例映射不能直接作为现场结构。"
        )
    schema = mapping.get("target_schema")
    _identifier(schema, relation=True)
    service, layer_table = _table(mapping, "service"), _table(mapping, "layer")
    if service["table"] == layer_table["table"]:
        raise CatalogError("TABLE_MAPPING_COLLISION", "服务和图层必须映射到两个不同表。")
    start = mapping.get("layer_order_start", 1)
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise CatalogError("INVALID_LAYER_ORDER", "图层顺序起点须为非负整数。")
    service_rows: list[dict] = []
    service_for_url: dict[str, dict] = {}
    service_keys: dict[str, dict] = {}
    for index, url in enumerate(sorted({layer["wms_url"] for layer in layers}), 1):
        token = task_id + ":service:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        row = _row(
            service, {"task_id": task_id, "wms_url": url, "service_token": token, "service_index": index}
        )
        key = json.dumps(row[service["key"]], ensure_ascii=False)
        if key in service_keys and service_keys[key] != row:
            raise CatalogError("SERVICE_KEY_COLLISION", "不同服务映射到了同一个唯一键且字段值不同。")
        if key not in service_keys:
            service_rows.append(row)
            service_keys[key] = row
        service_for_url[url] = row
    layer_rows: list[dict] = []
    layer_keys: set[str] = set()
    for index, layer in enumerate(layers, start):
        context = {
            **layer,
            "layer_index": index,
            "service_key": service_for_url[layer["wms_url"]][service["key"]],
        }
        row = _row(layer_table, context)
        key = json.dumps(row[layer_table["key"]], ensure_ascii=False)
        if key in layer_keys:
            raise CatalogError("LAYER_KEY_COLLISION", "多个图层映射到了同一个目录唯一键。")
        layer_keys.add(key)
        layer_rows.append(row)
    entries = [("service", service, row) for row in service_rows] + [
        ("layer", layer_table, row) for row in layer_rows
    ]
    body = [_schema_check(service, schema), _schema_check(layer_table, schema)]
    for role, table, row in entries:
        if role == "service" and service["mode"] == "reuse-verified":
            condition = (
                f"NOT EXISTS (SELECT 1 FROM {_identifier(table['table'])} WHERE {_predicate(row)} FOR SHARE)"
            )
            message = "Existing service does not match the reviewed values; no changes made."
        else:
            condition = (
                f"EXISTS (SELECT 1 FROM {_identifier(table['table'])} WHERE {_key_predicate(table, row)})"
            )
            message = "Catalog key already exists; registration is create-only and must not be replayed."
        body.append(f"  IF {condition} THEN\n    RAISE EXCEPTION {_literal(message)};\n  END IF;")
    for role, table, row in entries:
        if role == "service" and service["mode"] == "reuse-verified":
            continue
        body.append("  " + _insert(table, row).replace("\n", "\n  "))
    mapping_hash = hashlib.sha256(
        json.dumps(mapping, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    header = (
        f"-- GeoPublisher catalog export; task {task_id}\n"
        f"-- Mapping SHA-256: {mapping_hash}\n"
        "-- SQL GENERATED ONLY. No database connection or execution has occurred.\n"
    )
    registration = (
        header + "-- Execute this entire transaction after reviewing schema, fields and values.\n"
        "-- Any existing target key or unique-key mismatch aborts registration.\n"
        + _prelude(schema)
        + _do("\n\n".join(body))
        + "\nCOMMIT;\n"
    )
    queries = [
        _verification(table, row, f"{role}:{index}") for index, (role, table, row) in enumerate(entries, 1)
    ]
    read_checks = _do(_schema_check(service, schema) + "\n\n" + _schema_check(layer_table, schema))
    verification = (
        header
        + "-- MATCHED compares the configured columns only; it is not proof of task ownership.\n"
        + _prelude(schema, read_only=True)
        + read_checks
        + "\n\n".join(queries)
        + "\n\nCOMMIT;\n"
    )
    rollback = (
        header + "-- MANUAL REVIEW ONLY: this file deliberately contains no DELETE statements.\n"
        "-- Generated INSERT SQL does not prove execution, ownership or an unchanged full row.\n"
        "-- Confirm database execution receipts and a before/after snapshot before any removal.\n"
        "-- Never remove a parent service while other layers reference it.\n"
        + _prelude(schema, read_only=True)
        + read_checks
        + "\n\n".join(queries)
    )
    reference = layer_table["service_reference_column"]
    for index, service_row in enumerate(service_rows, 1):
        parent_key = service_row[service["key"]]
        own_layer_rows = [row for row in layer_rows if row[reference] == parent_key]
        own_keys = " OR ".join("(" + _key_predicate(layer_table, row) + ")" for row in own_layer_rows)
        rollback += (
            f"\n\nSELECT {_literal('service:' + str(index))} AS parent_service,\n"
            "       COUNT(*) AS total_referencing_layers,\n"
            f"       COUNT(*) FILTER (WHERE NOT ({own_keys})) AS references_outside_plan\n"
            f"FROM {_identifier(layer_table['table'])}\n"
            f"WHERE {_identifier(reference)} IS NOT DISTINCT FROM {_literal(parent_key)};"
        )
    rollback += "\n\nCOMMIT;\n"
    output_dir = Path(output_dir).absolute()
    if any(path.is_symlink() for path in (output_dir, *output_dir.parents)):
        raise CatalogError("SYMLINK_NOT_ALLOWED", "SQL 输出目录不能使用符号链接。")
    files = {
        "registration_sql": (output_dir / "register.sql", registration),
        "verification_sql": (output_dir / "verify.sql", verification),
        "rollback_sql": (output_dir / "rollback.sql", rollback),
    }
    for path, content in files.values():
        if path.is_symlink() or (
            path.exists() and (not path.is_file() or path.read_text(encoding="utf-8") != content)
        ):
            raise CatalogError("CATALOG_ARTIFACT_EXISTS", "SQL 产物已存在且内容不同，拒绝覆盖。")
    output_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        for path, content in files.values():
            if not path.exists():
                with path.open("x", encoding="utf-8", newline="\n") as stream:
                    created.append(path)
                    stream.write(content)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return {
        "status": "SQL_GENERATED_NOT_EXECUTED",
        "task_id": task_id,
        "registered": False,
        "rollback_mode": "manual-review",
        "mapping_sha256": mapping_hash,
        "layer_count": len(layer_rows),
        "service_count": len(service_rows),
        "service_mode": service["mode"],
        **{key: str(path.resolve()) for key, (path, _) in files.items()},
        "warnings": ["尚未连接或写入业务数据库。", "回滚文件仅供人工核对，不包含自动删除语句。"],
    }
