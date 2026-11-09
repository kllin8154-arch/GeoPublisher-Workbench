"""Read-only PostgreSQL business catalog evidence via pinned SSH loopback.

This is NOT a SQL executor. It verifies a local business-tree JSON mapping
against one authorized PostgreSQL database with a transaction explicitly in
READ ONLY mode. It never sets schema_verified or writes to the database.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from .business_tree import _NODE_FIELDS, _SERVICE_FIELDS, _service_values
from .catalog import _identifier
from .server_deploy import LinuxTarget, _local_regular
from .server_forward import open_service


class DatabaseReviewError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _error(code: str, message: str) -> None:
    raise DatabaseReviewError(code, message)


def _safe_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 160:
        _error("INVALID_CATALOG_REVIEW", f"{label} 必须为长度 1–160 的文字。")
    if "\x00" in value or any(ord(c) < 32 for c in value):
        _error("INVALID_CATALOG_REVIEW", f"{label} 包含控制字符。")
    return value


def review_mapping(file: Path | str) -> tuple[Path, dict, str]:
    source = _local_regular(file, suffix=".json")
    if source.stat().st_size > 2 * 1024 * 1024:
        _error("REVIEW_FILE_TOO_LARGE", "业务目录配置超过本功能支持的上限。")
    raw = source.read_bytes()
    try:
        profile = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(profile, dict) or profile.get("schema_version") != 1:
            raise ValueError()
        schema = profile["target_schema"]
        _identifier(schema)
        tables = profile["tables"]
        columns = profile["columns"]
        for role in ("node", "service"):
            table = tables[role]
            _identifier(table, relation=True)
            mapping = columns[role]
            expected = _NODE_FIELDS if role == "node" else _SERVICE_FIELDS
            if not isinstance(mapping, dict) or not expected <= set(mapping):
                raise ValueError()
            for value in mapping.values():
                _identifier(value)
            if len(set(mapping.values())) != len(mapping):
                raise ValueError()
        existing = profile["existing_parents"]
        groups = profile["groups"]
        layers = profile["layers"]
        if not isinstance(existing, list) or len(existing) > 50:
            raise ValueError()
        if not isinstance(groups, list) or len(groups) > 200:
            raise ValueError()
        if not isinstance(layers, list) or not 1 <= len(layers) <= 1000:
            raise ValueError()
        _safe_text(profile["defaults"]["sys"], "defaults.sys")
        if len({node["code"] for node in existing}) != len(existing):
            raise ValueError()
        if len({node["code"] for node in groups + layers}) != len(groups + layers):
            raise ValueError()
        for node in existing:
            _safe_text(node["code"], "existing parent.code")
            _safe_text(node["title"], "existing parent.title")
            if "parent" in node:
                _safe_text(node["parent"], "existing parent.parent")
        for node in groups:
            _safe_text(node["code"], "group.code")
            _safe_text(node["title"], "group.title")
            if node["parent"] is not None:
                _safe_text(node["parent"], "group.parent")
        for layer in layers:
            for field in ("code", "title", "service_key", "qualified_name", "crs"):
                _safe_text(layer[field], f"layer.{field}")
            if layer.get("service_mode", "create") not in ("create", "reuse"):
                raise ValueError()
        if tables["node"].casefold() == tables["service"].casefold():
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise DatabaseReviewError("INVALID_CATALOG_REVIEW", "配置 JSON 缺少必要的业务表、列映射或目录信息。") from exc
    return source, profile, hashlib.sha256(raw).hexdigest()


def assess_catalog_snapshot(profile: dict, snapshot: dict) -> dict:
    """Pure local comparison; never converts a probe into automatic approval."""
    blocks: list[str] = []
    tables = profile["tables"]
    cols = profile["columns"]
    relations = {name: kind for name, kind in snapshot["relations"]}
    fields: dict[str, dict[str, dict]] = {"node": {}, "service": {}}
    for row in snapshot["columns"]:
        table, column, data_type, nullable, default, identity, text_length = row
        for role in ("node", "service"):
            if table == tables[role]:
                fields[role][column] = {
                    "data_type": str(data_type),
                    "is_nullable": nullable == "YES",
                    "has_default": default is not None,
                    "is_identity": identity == "YES",
                    "max_length": text_length,
                }
    for role in ("node", "service"):
        name = tables[role]
        if relations.get(name) not in ("r", "p"):
            blocks.append(f"{role} 目标表 {name} 不存在或不是 PostgreSQL 普通/分区表")
            continue
        missing = sorted((set(cols[role].values()) | {"id"}) - set(fields[role]))
        if missing:
            blocks.append(f"{role} 表缺少字段：{', '.join(missing)}")
        text_fields = (
            {"sys", "key", "title", "parent", "kind", "service_reference", "memo"}
            if role == "node" else
            {"key", "title", "kind", "platform", "url", "layer", "crs", "format", "memo"}
        )
        numeric_fields = (
            {"display_order", "visible", "zoom_start", "zoom_end", "layer_index", "organization"}
            if role == "node" else {"organization"}
        )
        for logical, actual in cols[role].items():
            observed = fields[role].get(actual)
            if observed is None:
                continue
            if logical in text_fields and observed["data_type"] not in (
                "character varying", "character", "text"
            ):
                blocks.append(f"{role}.{actual} 应为文本类型，实际为 {observed['data_type']}")
            if logical in numeric_fields and observed["data_type"] not in (
                "smallint", "integer", "bigint", "numeric", "decimal"
            ):
                blocks.append(f"{role}.{actual} 应为数值类型，实际为 {observed['data_type']}")
        if role == "node":
            kind_column = fields[role].get(cols[role]["kind"])
            parent_column = fields[role].get(cols[role]["parent"])
            if profile["groups"] and kind_column and not kind_column["is_nullable"]:
                blocks.append("节点类型字段非空，但新建业务目录会写入 NULL，必须调整映射/结构")
            if any(group["parent"] is None for group in profile["groups"]):
                if parent_column and not parent_column["is_nullable"]:
                    blocks.append("父目录字段非空，但清单存在根目录 parent=NULL")
        id_column = fields[role].get("id")
        if id_column is not None and not snapshot["serial_sequences"].get(role):
            blocks.append(f"{role} 表 id 没有可核实的自增/关联序列")
        unhandled = sorted(
            name for name, data in fields[role].items()
            if name not in cols[role].values() and name != "id"
            and not data["is_nullable"] and not data["has_default"] and not data["is_identity"]
        )
        if unhandled:
            blocks.append(f"{role} 表存在不能省略的非空字段：{', '.join(unhandled)}")
    parent_review = []
    existing = profile["existing_parents"]
    found_nodes = snapshot["nodes"]
    defaults = profile["defaults"]
    for expected in existing:
        records = [row for row in found_nodes if row[0] == expected["code"]]
        same = [
            row for row in records
            if row[1] == expected["title"] and row[3] == defaults["sys"]
            and ("parent" not in expected or row[2] == expected["parent"])
        ]
        ok = len(same) == 1 and len(records) == 1
        parent_review.append({
            "code": expected["code"],
            "expected_title": expected["title"],
            "match_count": len(same),
            "exact_match": ok,
        })
        if not ok:
            blocks.append(f"已有父节点 {expected['code']} 未找到唯一且一致的 code/title/sys/parent")
    new_nodes = profile["groups"] + profile["layers"]
    collisions = []
    for desired in new_nodes:
        code, title = desired["code"], desired["title"]
        parent = desired["parent"]
        matches = [
            row for row in found_nodes
            if row[0] == code or (
                row[1] == title and row[2] == parent and row[3] == defaults["sys"]
            )
        ]
        if matches:
            collisions.append(code)
            blocks.append(f"拟建节点 {code} 与已有 code 或同名同父目录冲突")
    services = snapshot["services"]
    service_review = []
    seen_services = set()
    for layer in profile["layers"]:
        service_key = layer["service_key"]
        if service_key in seen_services:
            blocks.append(f"服务 {service_key} 在清单中重复，应单独复核")
            continue
        seen_services.add(service_key)
        mode = layer.get("service_mode", "create")
        expected = _service_values(layer, defaults)
        candidates = [
            row for row in services
            if row[0] == service_key or row[1] == expected["layer"]
        ]
        identical = [
            row for row in candidates
            if row == (
                expected["key"], expected["layer"], expected["url"],
                expected["crs"], expected["format"],
            )
        ]
        ok = (not candidates if mode == "create"
              else len(identical) == 1 and len(candidates) == 1)
        if not ok:
            blocks.append(
                f"服务 {service_key} 的目标模式 {mode} 与数据库已有 key/layer/url/crs/format 不一致"
            )
        service_review.append({
            "key": service_key, "mode": mode,
            "existing_matches": len(candidates), "exact_match": ok,
        })
    if snapshot.get("truncated", False):
        blocks.append("返回行数达到安全上限；必须缩小范围重新核验")
    return {
        "status": "BLOCKED_PENDING_HUMAN_REVIEW" if blocks else "READ_ONLY_EVIDENCE_READY_NOT_APPROVED",
        "schema_verified": False,
        "sql_executed": False,
        "database_modified": False,
        "table_columns": fields,
        "existing_parents": parent_review,
        "new_node_collisions": sorted(set(collisions)),
        "services": service_review,
        "blocking_reasons": blocks,
    }


def _read_snapshot(conn, profile: dict, *, database: str) -> dict:
    schema, tables, columns = (
        profile["target_schema"], profile["tables"], profile["columns"]
    )
    conn.execute("BEGIN READ ONLY")
    try:
        conn.execute("SET LOCAL statement_timeout = 10000")
        conn.execute(
            sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema))
        )
        identity = conn.execute(
            "SELECT current_database(), current_user, current_schema(), "
            "current_setting('transaction_read_only'), current_setting('server_version_num')"
        ).fetchone()
        if identity is None or identity[0] != database or identity[2] != schema or identity[3] != "on":
            _error("DATABASE_IDENTITY_MISMATCH", "目标数据库/模式未精确匹配，或事务不是 READ ONLY。")
        relations = conn.execute(
            "SELECT c.relname, c.relkind FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = ANY(%s)",
            (schema, [tables["node"], tables["service"]]),
        ).fetchall()
        field_rows = conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default, is_identity, "
            "character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = ANY(%s) "
            "ORDER BY table_name, ordinal_position LIMIT 1025",
            (schema, [tables["node"], tables["service"]]),
        ).fetchall()
        sequences = {}
        for role in ("node", "service"):
            result = conn.execute(
                "SELECT pg_catalog.pg_get_serial_sequence(%s, 'id')",
                (f'"{schema}"."{tables[role]}"',),
            ).fetchone()
            sequences[role] = result[0] if result else None
        node_col = columns["node"]
        node_code = [n["code"] for n in profile["existing_parents"] + profile["groups"] + profile["layers"]]
        titles = [n["title"] for n in profile["groups"] + profile["layers"]]
        node_sql = sql.SQL(
            "SELECT {}, {}, {}, {} FROM {} "
            "WHERE {} = ANY(%s) OR {} = ANY(%s) "
            "ORDER BY {} LIMIT 1202"
        ).format(
            sql.Identifier(node_col["key"]),
            sql.Identifier(node_col["title"]),
            sql.Identifier(node_col["parent"]),
            sql.Identifier(node_col["sys"]),
            sql.Identifier(tables["node"]),
            sql.Identifier(node_col["key"]),
            sql.Identifier(node_col["title"]),
            sql.Identifier(node_col["key"]),
        )
        node_rows = conn.execute(node_sql, (node_code, titles)).fetchall() if node_code or titles else []
        service_col = columns["service"]
        service_keys = [layer["service_key"] for layer in profile["layers"]]
        service_layers = [layer["qualified_name"] for layer in profile["layers"]]
        service_sql = sql.SQL(
            "SELECT {}, {}, {}, {}, {} FROM {} "
            "WHERE {} = ANY(%s) OR {} = ANY(%s) "
            "ORDER BY {} LIMIT 1202"
        ).format(
            *(sql.Identifier(service_col[key]) for key in ("key", "layer", "url", "crs", "format")),
            sql.Identifier(tables["service"]),
            sql.Identifier(service_col["key"]),
            sql.Identifier(service_col["layer"]),
            sql.Identifier(service_col["key"]),
        )
        service_rows = conn.execute(service_sql, (service_keys, service_layers)).fetchall()
        return {
            "identity": {
                "database": identity[0], "role": identity[1],
                "schema": identity[2], "read_only": identity[3],
                "server_version_num": identity[4],
            },
            "relations": relations,
            "columns": field_rows,
            "serial_sequences": sequences,
            "nodes": node_rows,
            "services": service_rows,
            "truncated": len(field_rows) >= 1025 or len(node_rows) >= 1202 or len(service_rows) >= 1202,
        }
    finally:
        conn.execute("ROLLBACK")


def probe_catalog(
    target: LinuxTarget,
    session,
    profile_file: str | Path,
    *,
    database: str, username: str, password: str,
    output: str | Path,
    connect=psycopg.connect,
) -> dict:
    """Query only specified business tables and metadata; create local evidence."""
    source, profile, digest = review_mapping(profile_file)
    _identifier(database)
    _identifier(username)
    if not password:
        _error("DATABASE_PASSWORD_REQUIRED", "请输入本次使用的只读数据库账号密码。")
    raw_dest = str(output)
    if raw_dest.startswith(("\\\\", "//")):
        _error("UNSAFE_REPORT_OUTPUT", "核验报告不能写入远程共享路径。")
    destination = Path(output).expanduser().absolute()
    if destination.exists() or destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        _error("UNSAFE_REPORT_OUTPUT", "报告须写入本机全新目录，不得覆盖或使用符号链接。")
    with open_service(session, service="postgres") as tunnel:
        try:
            with connect(
                host="127.0.0.1", port=tunnel.port, dbname=database,
                user=username, password=password,
                connect_timeout=10, autocommit=True, sslmode="disable",
                options="-c default_transaction_read_only=on "
                        "-c statement_timeout=10000 -c idle_in_transaction_session_timeout=15000",
                application_name="geopublisher_catalog_readonly",
            ) as conn:
                snapshot = _read_snapshot(conn, profile, database=database)
        except psycopg.Error as exc:
            raise DatabaseReviewError(
                "DATABASE_READONLY_PROBE_FAILED",
                "PostgreSQL 只读查询失败，请检查 206 SSH 隧道、数据库名、SELECT 权限与字段映射。",
            ) from exc
    review = assess_catalog_snapshot(profile, snapshot)
    review.update({
        "database_identity": snapshot["identity"],
        "target": target.safe_info(),
        "review_file_sha256": digest,
        "source_review_file": str(source),
        "review_required": True,
        "schema_verified_remains_manual": True,
        "sql_executed": False,
        "database_modified": False,
    })
    # No evidence is written until the read-only transaction successfully ends.
    destination.mkdir(parents=True, exist_ok=False)
    path = destination / "business_db_readonly_review.json"
    with path.open("x", encoding="utf-8") as fd:
        fd.write(json.dumps(review, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        fd.flush()
        os.fsync(fd.fileno())
    return {
        "status": review["status"],
        "database": database,
        "review": str(path),
        "blocking_reasons": review["blocking_reasons"],
        "read_only": True,
        "database_modified": False,
        "schema_verified_remains_manual": True,
    }
