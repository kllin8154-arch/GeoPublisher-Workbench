"""Synthetic multi-level catalog tests. No connections or real GIS/site data."""
from __future__ import annotations

import copy
import json
import socket
import subprocess
from pathlib import Path
from random import Random

import pytest
from PIL import Image

from geopublisher.business_tree import build_business_tree_sql
from geopublisher.catalog import CatalogError


def valid_png(tmp_path: Path) -> Path:
    path = tmp_path / "wms-real-local-preview.png"
    picture = Image.new("RGB", (128, 128))
    rng = Random(17)
    picture.putdata([
        (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        for _ in range(128 * 128)
    ])
    picture.save(path)
    assert path.stat().st_size > 1024
    return path


def manifest(tmp_path: Path, *, verified: bool = True) -> dict:
    evidence = str(valid_png(tmp_path))
    wms = {"status": "PASSED", "visual_reviewed": True, "png": evidence}
    if not verified:
        wms = {"status": "PENDING", "visual_reviewed": False}
    return {
        "schema_version": 1,
        "target_schema": "demo_catalog",
        "schema_verified": verified,
        "tables": {"service": "demo_services", "node": "demo_nodes"},
        "columns": {
            "service": {
                "key": "name", "title": "namec", "kind": "maptype",
                "platform": "platform", "url": "url", "layer": "layer",
                "crs": "crs", "format": "format", "memo": "memo",
                "organization": "ssdw",
            },
            "node": {
                "sys": "sys", "key": "code", "title": "namec", "parent": "glcode",
                "kind": "type", "display_order": "disporder", "visible": "visible",
                "zoom_start": "zooms", "zoom_end": "zoome",
                "service_reference": "svr_name", "layer_index": "lay_idx",
                "memo": "memo", "organization": "ssdw",
            },
        },
        "defaults": {
            "sys": "0", "wms_url": "geoserver/demo/wms",
            "workspace": "demo", "platform": "geoserver", "organization": 0,
        },
        "existing_parents": [{"code": "basemap", "title": "底图目录"}],
        "groups": [
            {"code": "geo_50k", "title": "1:5万地质图", "parent": "basic_geo",
             "display_order": 10, "layer_index": 10, "scale": "1:5万"},
            {"code": "basic_geo", "title": "基础地质", "parent": None,
             "display_order": 30, "layer_index": 30},
            {"code": "world", "title": "世界影像", "parent": "basemap",
             "display_order": 50, "layer_index": 1},
            {"code": "geo_200k", "title": "1:20万地质图", "parent": "basic_geo",
             "display_order": 20, "layer_index": 20, "scale": "1:20万"},
        ],
        "layers": [
            {
                "code": "sheet_a", "title": "合成 A 幅", "parent": "geo_50k",
                "service_key": "sheet_a_service", "qualified_name": "demo:sheet_a",
                "source_format": "mxd", "publish_kind": "shapefile", "crs": "EPSG:4326",
                "scale": "1:5万", "conversion_reviewed": True,
                "display_order": 10, "layer_index": 10,
                "zoom_start": 1, "zoom_end": 29, "wms": copy.deepcopy(wms),
            },
            {
                "code": "sheet_b", "title": "合成 B 幅", "parent": "geo_200k",
                "service_key": "sheet_b_service", "qualified_name": "demo:sheet_b",
                "source_format": "gdb", "publish_kind": "shapefile", "crs": "EPSG:4326",
                "scale": "1:20万", "conversion_reviewed": True,
                "display_order": 20, "layer_index": 20,
                "zoom_start": 1, "zoom_end": 29, "wms": copy.deepcopy(wms),
            },
            {
                "code": "world_overview", "title": "全球影像", "parent": "world",
                "service_key": "world_overview", "qualified_name": "demo:world_overview",
                "source_format": "geotiff", "publish_kind": "geotiff", "crs": "EPSG:4326",
                "display_order": 10, "layer_index": 1,
                "zoom_start": 1, "zoom_end": 29, "wms": copy.deepcopy(wms),
            },
            {
                "code": "world_hires", "title": "全球高清影像", "parent": "world",
                "service_key": "world_hires", "qualified_name": "demo:world_hires",
                "source_format": "tiff_mosaic", "publish_kind": "imagemosaic",
                "crs": "EPSG:4326",
                "display_order": 20, "layer_index": 2,
                "zoom_start": 1, "zoom_end": 29, "wms": copy.deepcopy(wms),
            },
        ],
    }


def _outputs(result: dict) -> str:
    return Path(result["registration_sql"]).read_text(encoding="utf-8")


def test_groups_sorted_by_dependencies_and_scales_not_forced_into_basemap(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("No network"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("No process"))
    config = manifest(tmp_path)
    result = build_business_tree_sql(config, tmp_path / "output-new")
    assert result["status"] == "SQL_GENERATED_NOT_EXECUTED"
    assert result["registered"] is False
    assert result["services_new"] == 4
    assert result["groups"] == 4
    assert result["layers"] == 4
    sql = _outputs(result)
    assert sql.count('INSERT INTO "demo_services"') == 4
    assert sql.count('INSERT INTO "demo_nodes"') == 8
    assert sql.index("基础地质") < sql.index("1:5万地质图")
    assert sql.index("基础地质") < sql.index("1:20万地质图")
    assert 'E\'basic_geo\'' in sql
    assert 'E\'world\'' in sql
    assert 'E\'basemap\'' in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "ON CONFLICT" not in sql
    assert "UPDATE " not in sql
    assert "DELETE " not in sql
    assert (tmp_path / "output-new" / "01_preflight_readonly.sql").is_file()
    assert (tmp_path / "output-new" / "03_verify_readonly.sql").is_file()
    assert (tmp_path / "output-new" / "04_rollback_review_readonly.sql").is_file()


def test_pending_wms_and_unreviewed_schema_produce_non_executable_sql(tmp_path):
    config = manifest(tmp_path, verified=False)
    result = build_business_tree_sql(config, tmp_path / "unverified")
    assert result["status"] == "BLOCKED_PENDING_REVIEW"
    assert result["blocked"]
    sql = _outputs(result)
    assert 'RAISE EXCEPTION' in sql
    assert "INSERT INTO" not in sql
    assert "CREATE TABLE" not in sql
    assert "DELETE FROM" not in sql
    assert not (tmp_path / "unverified" / "02_register_reviewed.sql").exists()
    assert result["database_accessed"] is False


def test_reuse_existing_service_is_guarded_not_reinserted(tmp_path):
    config = manifest(tmp_path)
    config["layers"][0]["service_mode"] = "reuse"
    result = build_business_tree_sql(config, tmp_path / "existing-service")
    assert result["services_new"] == 3
    assert result["services_reused"] == 1
    sql = _outputs(result)
    assert sql.count('INSERT INTO "demo_services"') == 3
    assert "Existing service not exactly matched: sheet_a_service" in sql


def test_exact_service_name_duplicate_with_different_layers_is_rejected(tmp_path):
    config = manifest(tmp_path)
    config["layers"][1]["service_key"] = "sheet_a_service"
    with pytest.raises(CatalogError) as err:
        build_business_tree_sql(config, tmp_path / "ambiguous")
    assert err.value.code == "CONFLICTING_SERVICE_BINDING"
    assert not (tmp_path / "ambiguous").exists()


@pytest.mark.parametrize("field, value, code", [
    ("parent", "missing_parent", "UNKNOWN_TREE_PARENT"),
    ("parent", "geo_200k", "SCALE_PARENT_MISMATCH"),
    ("publish_kind", "geotiff", "FORMAT_ROUTE_MISMATCH"),
])
def test_unsafe_parent_scale_format_is_blocked(tmp_path, field, value, code):
    config = manifest(tmp_path)
    config["layers"][0][field] = value
    with pytest.raises(CatalogError) as err:
        build_business_tree_sql(config, tmp_path / "unsafe")
    assert err.value.code == code
    assert not (tmp_path / "unsafe").exists()


def test_cycle_in_group_hierarchy_is_rejected(tmp_path):
    config = manifest(tmp_path)
    config["groups"][1]["parent"] = "geo_50k"
    with pytest.raises(CatalogError) as err:
        build_business_tree_sql(config, tmp_path / "cycle")
    assert err.value.code == "CYCLIC_TREE"


def test_unverified_mxd_conversion_blocks_even_with_valid_png(tmp_path):
    config = manifest(tmp_path)
    config["layers"][0]["conversion_reviewed"] = False
    result = build_business_tree_sql(config, tmp_path / "missing-conversion")
    assert result["status"] == "BLOCKED_PENDING_REVIEW"
    assert "转换" in " ".join(result["blocked"])
    assert "INSERT INTO" not in _outputs(result)


def test_png_evidence_cannot_be_arbitrary_file_or_unc(tmp_path):
    config = manifest(tmp_path)
    config["layers"][0]["wms"]["png"] = str(tmp_path / "missing.png")
    with pytest.raises(CatalogError) as err:
        build_business_tree_sql(config, tmp_path / "missing-evidence")
    assert err.value.code == "INVALID_WMS_EVIDENCE"
    config["layers"][0]["wms"]["png"] = r"\\remote\files\preview.png"
    with pytest.raises(CatalogError) as err:
        build_business_tree_sql(config, tmp_path / "remote-evidence")
    assert err.value.code == "INVALID_WMS_EVIDENCE"


def test_sql_value_quotes_not_executable_and_output_never_overwritten(tmp_path):
    config = manifest(tmp_path)
    config["layers"][2]["title"] = "O'Brien\\n'); DELETE FROM accounts; --"
    result = build_business_tree_sql(config, tmp_path / "ready")
    assert "O''Brien" in _outputs(result)
    with pytest.raises(CatalogError) as err:
        build_business_tree_sql(config, tmp_path / "ready")
    assert err.value.code == "OUTPUT_ALREADY_EXISTS"


def test_unexpected_profile_options_do_not_silently_disappear(tmp_path):
    config = manifest(tmp_path)
    config["auto_execute"] = True
    with pytest.raises(CatalogError) as err:
        build_business_tree_sql(config, tmp_path / "extra")
    assert err.value.code == "INVALID_TREE_CONFIG"


def test_rollback_review_is_strictly_read_only(tmp_path):
    result = build_business_tree_sql(manifest(tmp_path), tmp_path / "new")
    sql = (tmp_path / "new" / "04_rollback_review_readonly.sql").read_text(encoding="utf-8")
    assert "BEGIN READ ONLY" in sql
    assert "rollback_candidate" in sql
    assert "DELETE FROM" not in sql
    assert result["services_new"] == 4


def test_metadata_summary_can_be_read_as_json(tmp_path):
    build_business_tree_sql(manifest(tmp_path), tmp_path / "new")
    parsed = json.loads((tmp_path / "new" / "business_tree_review.json").read_text("utf-8"))
    assert parsed["tree"]["groups"][0]["code"] == "basic_geo"
    assert parsed["tree"]["layers"][0]["scale"] == "1:5万"
    assert parsed["wms_evidence"][0]["png_sha256"]



def test_cli_business_tree_example_is_offline_and_blocked(tmp_path, capsys):
    from geopublisher.cli import main

    source = Path(__file__).parents[1] / "examples" / "business-tree.example.json"
    dest = tmp_path / "cli-review"
    assert main(["catalog-tree", str(source), "--output", str(dest)]) == 3
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "BLOCKED_PENDING_REVIEW"
    assert output["database_accessed"] is False
    assert output["geoserver_accessed"] is False
    assert (dest / "02_register_BLOCKED.sql").is_file()
    assert not (dest / "02_register_reviewed.sql").exists()



def test_explicit_nongeographic_local_crs_code_is_supported_as_metadata(tmp_path):
    config = manifest(tmp_path)
    config["layers"][0]["crs"] = "EPSG:4490"
    result = build_business_tree_sql(config, tmp_path / "crs4490")
    assert result["status"] == "SQL_GENERATED_NOT_EXECUTED"
    assert "EPSG:4490" in _outputs(result)


@pytest.mark.parametrize("crs", ["EPSG:0", "EPSG:abc", "EPSG:4326;DROP", "WKT_UNKNOWN", 4326])
def test_unverified_crs_rejected(tmp_path, crs):
    config = manifest(tmp_path)
    config["layers"][0]["crs"] = crs
    with pytest.raises(CatalogError) as err:
        build_business_tree_sql(config, tmp_path / "badcrs")
    assert err.value.code == "UNREVIEWED_CRS"
