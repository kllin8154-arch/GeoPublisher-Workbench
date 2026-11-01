from __future__ import annotations

import copy
import json
import re
import socket
import subprocess
from pathlib import Path

import pytest

from geopublisher.catalog import CatalogError, _literal, generate_catalog


def bindings():
    return {
        "schema_version": 1,
        "registered": False,
        "layers": [
            {
                "task_id": "task-123",
                "qualified_name": "demo:points",
                "wms_url": "http://localhost:8080/geoserver/wms",
                "crs": "EPSG:4326",
                "title": "合成点",
            },
            {
                "task_id": "task-123",
                "qualified_name": "demo:lines",
                "wms_url": "http://localhost:8080/geoserver/wms",
                "crs": "EPSG:4326",
                "title": "合成线",
            },
        ],
    }


def mapping():
    return {
        "schema_version": 1,
        "database": "postgresql",
        "target_schema": "reviewed_schema",
        "schema_verified": True,
        "layer_order_start": 1,
        "service": {
            "table": "demo_mapservice",
            "key": "service_key",
            "mode": "create",
            "unique_key_verified": True,
            "columns": {
                "service_key": {"source": "service_token"},
                "service_url": {"source": "wms_url"},
                "created_task": {"source": "task_id"},
            },
        },
        "layer": {
            "table": "demo_maplayer",
            "key": "layer_key",
            "unique_key_verified": True,
            "service_reference_column": "service_key",
            "service_reference_verified": True,
            "columns": {
                "layer_key": {"source": "qualified_name"},
                "service_key": {"source": "service_key"},
                "title": {"source": "title"},
                "lay_idx": {"source": "layer_index"},
                "created_task": {"source": "task_id"},
            },
        },
    }


def read_outputs(result):
    return tuple(
        Path(result[name]).read_text(encoding="utf-8")
        for name in ("registration_sql", "verification_sql", "rollback_sql")
    )


def statements_only(sql):
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def test_generate_sql_without_connecting_or_executing(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: pytest.fail("No network access permitted"))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("No SQL execution permitted"))
    result = generate_catalog(bindings(), mapping(), tmp_path)
    assert result["status"] == "SQL_GENERATED_NOT_EXECUTED"
    assert result["registered"] is False
    assert result["layer_count"] == 2
    assert result["service_count"] == 1
    assert result["rollback_mode"] == "manual-review"
    registration, verification, rollback = read_outputs(result)
    assert registration.count('INSERT INTO "demo_mapservice"') == 1
    assert registration.count('INSERT INTO "demo_maplayer"') == 2
    assert "BEGIN READ ONLY;" in verification
    assert "references_outside_plan" in rollback


def test_registration_checks_every_key_before_first_insert_and_has_no_upsert(tmp_path):
    registration, _, _ = read_outputs(generate_catalog(bindings(), mapping(), tmp_path))
    assert registration.rindex("Catalog key already exists") < registration.index("INSERT INTO")
    assert "ON CONFLICT" not in registration
    assert not re.search(r"\bUPDATE\s+\"", statements_only(registration))
    assert "i.indisunique" in registration
    assert "i.indpred IS NULL" in registration
    assert "i.indnkeyatts = 1" in registration


def test_tables_have_no_schema_prefix_and_lookup_is_pinned(tmp_path):
    registration, verification, rollback = read_outputs(generate_catalog(bindings(), mapping(), tmp_path))
    for sql in (registration, verification, rollback):
        assert 'SET LOCAL search_path = pg_catalog, "reviewed_schema", pg_temp;' in sql
        assert '"reviewed_schema"."demo_' not in sql
        assert "n.nspname = E'reviewed_schema'" in sql


def test_manual_rollback_never_deletes_even_with_task_marker_columns(tmp_path):
    _, _, rollback = read_outputs(generate_catalog(bindings(), mapping(), tmp_path))
    assert "created_task" in mapping()["layer"]["columns"]
    assert not re.search(r"\b(DELETE|UPDATE|INSERT|TRUNCATE)\b", statements_only(rollback))
    assert "BEGIN READ ONLY" in rollback
    assert "total_referencing_layers" in rollback


def test_quotes_backslashes_and_newlines_are_literal_values(tmp_path):
    source = bindings()
    source["layers"][0]["title"] = "O'Brien\\folder\n'); DROP TABLE demo_maplayer; --"
    registration, _, _ = read_outputs(generate_catalog(source, mapping(), tmp_path))
    assert "E'O''Brien\\\\folder\\n''); DROP TABLE demo_maplayer; --'" in registration
    assert _literal("\\';SELECT 1;") == "E'\\\\'';SELECT 1;'"


def test_outer_dollar_quote_delimiter_cannot_be_injected(tmp_path):
    source = bindings()
    source["layers"][0]["title"] = "$geopub_catalog$; END; $geopub_catalog_x$"
    registration, _, _ = read_outputs(generate_catalog(source, mapping(), tmp_path))
    delimiter = re.search(r"DO (\$[A-Za-z_]+\$)", registration).group(1)
    assert delimiter == "$geopub_catalog_x_x$"
    assert registration.count(delimiter) == 2


@pytest.mark.parametrize(
    "bad",
    ["public.demo_mapservice", "table;DROP", 'double"quote', "123table", "pg_class", "étiquette", "a" * 64],
)
def test_unsafe_table_identifiers_rejected(tmp_path, bad):
    config = mapping()
    config["service"]["table"] = bad
    with pytest.raises(CatalogError):
        generate_catalog(bindings(), config, tmp_path)
    assert not list(tmp_path.iterdir())


def test_unreviewed_example_is_deliberately_blocked(tmp_path):
    example = json.loads(
        (Path(__file__).parents[1] / "examples/catalog-mapping.example.json").read_text(encoding="utf-8")
    )
    with pytest.raises(CatalogError) as error:
        generate_catalog(bindings(), example, tmp_path)
    assert error.value.code == "SCHEMA_NOT_VERIFIED"


@pytest.mark.parametrize("role", ["service", "layer"])
def test_unique_key_attestation_is_required(tmp_path, role):
    config = mapping()
    config[role]["unique_key_verified"] = False
    with pytest.raises(CatalogError) as error:
        generate_catalog(bindings(), config, tmp_path)
    assert error.value.code == "UNVERIFIED_UNIQUE_KEY"


def test_reference_mapping_cannot_point_to_unrelated_value(tmp_path):
    config = mapping()
    config["layer"]["columns"]["service_key"] = {"value": "unrelated"}
    with pytest.raises(CatalogError) as error:
        generate_catalog(bindings(), config, tmp_path)
    assert error.value.code == "INVALID_SERVICE_REFERENCE"


@pytest.mark.parametrize(
    "spec", [{"sql": "nextval('some_sequence')"}, {"source": "title", "value": "x"}, {"source": ["title"]}]
)
def test_raw_sql_and_ambiguous_mappings_are_rejected(tmp_path, spec):
    config = mapping()
    config["layer"]["columns"]["title"] = spec
    with pytest.raises(CatalogError):
        generate_catalog(bindings(), config, tmp_path)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "text\0tail", [1], {"sql": "x"}])
def test_invalid_literals_are_rejected(tmp_path, value):
    config = mapping()
    config["layer"]["columns"]["title"] = {"value": value}
    with pytest.raises(CatalogError) as error:
        generate_catalog(bindings(), config, tmp_path)
    assert error.value.code == "INVALID_SQL_VALUE"


def test_service_reuse_requires_exact_values_and_does_not_insert_service(tmp_path):
    config = mapping()
    config["service"]["mode"] = "reuse-verified"
    config["service"]["columns"]["service_key"] = {"value": "existing-verified-service"}
    config["service"]["columns"].pop("created_task")
    registration, _, _ = read_outputs(generate_catalog(bindings(), config, tmp_path))
    assert 'INSERT INTO "demo_mapservice"' not in registration
    assert '"service_url" IS NOT DISTINCT FROM' in registration
    assert "FOR SHARE" in registration
    assert "Existing service does not match" in registration
    assert registration.count('INSERT INTO "demo_maplayer"') == 2


def test_duplicate_layer_keys_do_not_generate_partial_scripts(tmp_path):
    config = mapping()
    config["layer"]["columns"]["layer_key"] = {"value": "same-key"}
    with pytest.raises(CatalogError) as error:
        generate_catalog(bindings(), config, tmp_path)
    assert error.value.code == "LAYER_KEY_COLLISION"
    assert not list(tmp_path.iterdir())


def test_multiple_services_with_conflicting_key_are_rejected(tmp_path):
    source, config = bindings(), mapping()
    source["layers"][1]["wms_url"] = "http://localhost:8081/geoserver/wms"
    config["service"]["columns"]["service_key"] = {"value": "same-key"}
    with pytest.raises(CatalogError) as error:
        generate_catalog(source, config, tmp_path)
    assert error.value.code == "SERVICE_KEY_COLLISION"


def test_mixed_tasks_and_duplicate_bindings_are_rejected(tmp_path):
    source = bindings()
    source["layers"][1]["task_id"] = "other-task"
    with pytest.raises(CatalogError) as error:
        generate_catalog(source, mapping(), tmp_path)
    assert error.value.code == "MIXED_TASK_BINDINGS"
    source["layers"][1] = copy.deepcopy(source["layers"][0])
    with pytest.raises(CatalogError) as error:
        generate_catalog(source, mapping(), tmp_path)
    assert error.value.code == "DUPLICATE_BINDING"


@pytest.mark.parametrize(
    "url", ["http://user:secret@localhost/wms", "https://localhost/wms?token=secret", "file:///tmp/wms"]
)
def test_service_url_cannot_embed_credentials_or_token_queries(tmp_path, url):
    source = bindings()
    source["layers"][0]["wms_url"] = url
    with pytest.raises(CatalogError) as error:
        generate_catalog(source, mapping(), tmp_path)
    assert error.value.code == "INVALID_SERVICE_URL"


def test_same_generation_reuses_identical_artifacts_without_overwriting_changed_files(tmp_path):
    first = generate_catalog(bindings(), mapping(), tmp_path)
    second = generate_catalog(bindings(), mapping(), tmp_path)
    assert first == second
    path = Path(first["registration_sql"])
    path.write_text("manually reviewed change")
    with pytest.raises(CatalogError) as error:
        generate_catalog(bindings(), mapping(), tmp_path)
    assert error.value.code == "CATALOG_ARTIFACT_EXISTS"
    assert path.read_text() == "manually reviewed change"


def test_symlink_output_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "redirect"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(CatalogError) as error:
        generate_catalog(bindings(), mapping(), link)
    assert error.value.code == "SYMLINK_NOT_ALLOWED"
