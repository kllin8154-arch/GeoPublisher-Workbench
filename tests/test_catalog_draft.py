"""Offline preparation of safe, unclassified GIS business tree drafts."""
from __future__ import annotations

import copy
import json
import socket
import subprocess
import uuid
from pathlib import Path

import pytest
from PIL import Image

from geopublisher.business_tree import build_business_tree_sql
from geopublisher.catalog import CatalogError
from geopublisher.catalog_draft import PENDING_GROUP, generate_catalog_draft
from geopublisher.config import Environment
from geopublisher.plans import fingerprint


def fixture_plan(tmp_path: Path, *, count=2, with_report=True) -> Path:
    task_dir = tmp_path / "example-publish-task"
    task_dir.mkdir()
    files = []
    for i in range(count):
        kind = "shapefile" if i == 0 else "geotiff"
        rid = f"r{i+1:04d}"
        filename = "synthetic_faults" if i == 0 else f"synthetic_imagery_{i}"
        data_dir = task_dir / rid
        data_dir.mkdir()
        source = tmp_path / ("synthetic_input_" + rid + (".shp" if i == 0 else ".tif"))
        source.write_bytes(b"synthetic")
        package = data_dir / ("dataset.zip" if i == 0 else "dataset.tif")
        package.write_bytes(b"synthetic-copy")
        style = data_dir / "style.sld"
        style.write_text("synthetic", encoding="utf-8")
        files.append({
            "resource_id": rid,
            "target_name": filename,
            "kind": kind,
            "store_name": filename + "_store",
            "style_name": filename + "_style",
            "blocking_errors": [],
            "inspection": {
                "source": str(source), "crs": "EPSG:4490" if i == 0 else "EPSG:4326",
                "wgs84_bbox": [100, 20, 101, 21],
            },
            "artifact": {
                "path": str(package), "sha256": "a" * 64,
                "source_hashes": [{"path": str(source), "sha256": "a" * 64}],
            },
            "style": {"path": str(style), "sha256": "b" * 64},
        })
    task = str(uuid.uuid4())
    plan = {
        "schema_version": 1, "task_id": task,
        "environment": Environment(
            "synthetic_environment", "https://private.example.invalid/geoserver", "demo", "/local/gdal"
        ).to_dict(),
        "ready": True, "enable_wfs": False, "strategy": "fail",
        "output_dir": str(task_dir),
        "resources": files, "catalog_mode": "export-only",
    }
    plan["plan_hash"] = fingerprint(plan)
    (task_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    if with_report:
        png = task_dir / "r0001" / "preview.png"
        img = Image.new("RGB", (128, 128))
        img.putdata([((x * 17 + y) % 256, (x + y * 19) % 256, (x * 37 + y * 3) % 256)
                     for y in range(128) for x in range(128)])
        img.save(png)
        report = {
            "task_id": task, "workspace": "demo", "output_dir": str(task_dir),
            "status": "REVIEW_REQUIRED",
            "resources": [
                {
                    "resource_id": "r0001", "target_name": files[0]["target_name"],
                    "qualified_name": "demo:" + files[0]["target_name"],
                    "status": "SUCCEEDED", "preview_path": str(png),
                    "wms": {"evidence": {"status": "PASSED"}},
                },
                {
                    "resource_id": "r0002", "target_name": files[1]["target_name"],
                    "qualified_name": "demo:" + files[1]["target_name"],
                    "status": "REVIEW_REQUIRED",
                },
            ] if count == 2 else [],
        }
        (task_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return task_dir / "plan.json"


def read_profile(result: dict) -> dict:
    return json.loads(Path(result["draft"]).read_text(encoding="utf-8"))


def test_draft_auto_populates_only_trusted_plan_metadata_and_requires_review(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kw: pytest.fail("Network not allowed"))
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kw: pytest.fail("Process not allowed"))
    plan = fixture_plan(tmp_path)
    result = generate_catalog_draft(plan, tmp_path / "draft-output")
    assert result["status"] == "DRAFT_NOT_REVIEWED"
    assert result["registered"] is False
    assert result["layer_count"] == 2
    profile = read_profile(result)
    assert profile["schema_verified"] is False
    assert profile["groups"][0]["code"] == PENDING_GROUP
    assert profile["groups"][0]["classification_reviewed"] is False
    assert profile["layers"][0]["qualified_name"] == "demo:synthetic_faults"
    assert profile["layers"][0]["crs"] == "EPSG:4490"
    assert profile["layers"][0]["wms"]["status"] == "PENDING"
    assert profile["layers"][0]["classification_reviewed"] is False
    assert profile["layers"][0]["wms"]["png"].endswith("preview.png")
    assert profile["layers"][1]["wms"].get("png") is None
    assert profile["layers"][0]["source_format"] == "shp"
    all_text = json.dumps(profile)
    assert "private.example.invalid" not in all_text
    assert str(tmp_path / "synthetic_input_r0001.shp") not in all_text
    reviewed = build_business_tree_sql(profile, tmp_path / "sql-output")
    assert reviewed["status"] == "BLOCKED_PENDING_REVIEW"
    assert not (tmp_path / "sql-output" / "02_register_reviewed.sql").exists()
    assert "INSERT INTO" not in (tmp_path / "sql-output" / "02_register_BLOCKED.sql").read_text("utf-8")


def test_unknown_report_never_claims_success(tmp_path):
    result = generate_catalog_draft(fixture_plan(tmp_path, with_report=False), tmp_path / "draft")
    draft = read_profile(result)
    assert all(layer["wms"]["status"] == "PENDING" for layer in draft["layers"])
    assert all("png" not in layer["wms"] for layer in draft["layers"])
    details = json.loads(Path(result["report"]).read_text("utf-8"))
    assert details["matching_report"] == "MISSING"


def test_plan_hash_tampering_is_blocked(tmp_path):
    plan = fixture_plan(tmp_path)
    edited = json.loads(plan.read_text("utf-8"))
    edited["resources"][0]["target_name"] = "unaudited"
    plan.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(Exception) as err:
        generate_catalog_draft(plan, tmp_path / "draft")
    assert getattr(err.value, "code", None) == "PLAN_CHANGED"
    assert not (tmp_path / "draft").exists()


def test_report_from_wrong_task_is_rejected(tmp_path):
    plan = fixture_plan(tmp_path)
    report = plan.parent / "report.json"
    data = json.loads(report.read_text("utf-8"))
    data["task_id"] = str(uuid.uuid4())
    report.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CatalogError) as err:
        generate_catalog_draft(plan, tmp_path / "draft")
    assert err.value.code == "INVALID_CATALOG_DRAFT"


def test_report_with_wrong_preview_or_nonmatching_layer_does_not_promote_wms(tmp_path):
    plan = fixture_plan(tmp_path)
    report = plan.parent / "report.json"
    data = json.loads(report.read_text("utf-8"))
    data["resources"][0]["preview_path"] = str(tmp_path / "not-in-task" / "photo.png")
    report.write_text(json.dumps(data), encoding="utf-8")
    result = generate_catalog_draft(plan, tmp_path / "draft")
    assert "png" not in read_profile(result)["layers"][0]["wms"]


def test_approved_schema_template_is_downgraded_to_unverified(tmp_path):
    plan = fixture_plan(tmp_path)
    sample = Path(__file__).parents[1] / "examples" / "business-tree.example.json"
    template = json.loads(sample.read_text("utf-8"))
    template["schema_verified"] = True
    custom = tmp_path / "field_mapping.json"
    custom.write_text(json.dumps(template), encoding="utf-8")
    result = generate_catalog_draft(plan, tmp_path / "draft", template_file=custom)
    draft = read_profile(result)
    assert draft["target_schema"] == template["target_schema"]
    assert draft["tables"] == template["tables"]
    assert draft["schema_verified"] is False
    assert draft["existing_parents"] == []
    assert draft["layers"][0]["parent"] == PENDING_GROUP


def test_draft_never_overwrites_existing_directory_or_targets_source_dir(tmp_path):
    plan = fixture_plan(tmp_path)
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(CatalogError):
        generate_catalog_draft(plan, target)
    with pytest.raises(CatalogError):
        generate_catalog_draft(plan, plan.parent / "inside-publish-plan")
    assert not (plan.parent / "inside-publish-plan").exists()


def test_unclassified_group_stays_blocked_even_if_user_edits_review_flags(tmp_path):
    plan = fixture_plan(tmp_path)
    draft = read_profile(generate_catalog_draft(plan, tmp_path / "draft"))
    draft["schema_verified"] = True
    draft["groups"][0]["classification_reviewed"] = True
    for layer in draft["layers"]:
        layer["classification_reviewed"] = True
    sql = build_business_tree_sql(draft, tmp_path / "blocked")
    assert "临时目录" in "".join(sql["blocked"])
    assert "INSERT INTO" not in (tmp_path / "blocked" / "02_register_BLOCKED.sql").read_text("utf-8")



def test_cli_catalog_draft_never_promotes_visual_or_db_approval(tmp_path, capsys):
    from geopublisher.cli import main

    plan = fixture_plan(tmp_path)
    target = tmp_path / "cli-draft"
    assert main(["catalog-draft", str(plan), "--output", str(target)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "DRAFT_NOT_REVIEWED"
    parsed = json.loads((target / "business_tree_draft.json").read_text("utf-8"))
    assert parsed["schema_verified"] is False
    assert all(not layer["wms"]["visual_reviewed"] for layer in parsed["layers"])


def test_bad_or_unexpected_evidence_type_fails_closed(tmp_path):
    plan = fixture_plan(tmp_path)
    report_path = plan.parent / "report.json"
    report = json.loads(report_path.read_text("utf-8"))
    report["resources"][0]["wms"]["evidence"] = None
    report_path.write_text(json.dumps(report), encoding="utf-8")
    draft = generate_catalog_draft(plan, tmp_path / "draft")
    assert "png" not in read_profile(draft)["layers"][0]["wms"]


def test_template_does_not_reuse_old_parent_or_geoserver_workspace(tmp_path):
    plan = fixture_plan(tmp_path)
    source = Path(__file__).parents[1] / "examples" / "business-tree.example.json"
    template = copy.deepcopy(json.loads(source.read_text("utf-8")))
    template["defaults"]["workspace"] = "other_workspace"
    template_path = tmp_path / "template.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")
    draft = read_profile(generate_catalog_draft(
        plan, tmp_path / "draft", template_file=template_path
    ))
    assert draft["defaults"]["workspace"] == "demo"
    assert draft["defaults"]["wms_url"] == "geoserver/demo/wms"
    assert draft["existing_parents"] == []
    assert len(draft["groups"]) == 1



def test_catalog_draft_rejects_unc_output_without_network_access(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: pytest.fail("No network"))
    plan = fixture_plan(tmp_path, with_report=False)
    with pytest.raises(CatalogError) as err:
        generate_catalog_draft(plan, r"\\untrusted-file-server\share\new-catalog-folder")
    assert err.value.code == "INVALID_CATALOG_DRAFT"


def test_gdb_two_sheets_become_database_parent_and_children(tmp_path):
    """The JSON is only SQL input; final parent/children are INSERTs into node table."""
    plan_file = fixture_plan(tmp_path)
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    source2 = tmp_path / "synthetic_second.shp"
    source2.write_bytes(b"synthetic")
    # Reuse the fixture's second resource slot for another SHP in the same batch.
    second = plan["resources"][1]
    second["kind"] = "shapefile"
    second["target_name"] = "synthetic_second"
    second["inspection"]["source"] = str(source2)
    second["inspection"]["crs"] = "EPSG:4490"
    second["artifact"]["source_hashes"] = [
        {"path": str(source2), "sha256": "a" * 64}
    ]
    shared = {
        "batch_id": "src_0123456789abcdef", "source_format": "gdb",
        "source_name": "多个图幅.gdb", "source_frame": "",
        "manifest": str(tmp_path / "some_conversion" / "conversion-report.json"),
        "manifest_sha256": "a" * 64, "conversion_status": "EXPORTED",
    }
    for index, resource in enumerate(plan["resources"], start=1):
        resource["conversion_origin"] = {
            **shared, "source_layer": f"图幅{index:02d}",
        }
    plan["plan_hash"] = fingerprint({k: v for k, v in plan.items() if k != "plan_hash"})
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    result = generate_catalog_draft(plan_file, tmp_path / "gdb-draft")
    profile = read_profile(result)
    assert result["source_parent_count"] == 1
    assert len(profile["groups"]) == 2
    parent = profile["groups"][1]
    assert parent["title"] == "多个图幅.gdb"
    assert parent["parent"] == PENDING_GROUP
    assert all(layer["parent"] == parent["code"] for layer in profile["layers"])
    assert all(layer["source_format"] == "gdb" for layer in profile["layers"])
    assert all(layer["conversion_reviewed"] is False for layer in profile["layers"])
    assert profile["schema_verified"] is False
    assert build_business_tree_sql(profile, tmp_path / "blocked")["status"] == "BLOCKED_PENDING_REVIEW"

    # Simulated *human review* of schema, original conversion and real WMS evidence.
    # This verifies generated database INSERT ordering, never executes it.
    profile["groups"] = [{**parent, "parent": "existing_scale", "classification_reviewed": True}]
    profile["existing_parents"] = [{"code": "existing_scale", "title": "1:5万基础地质图"}]
    profile["schema_verified"] = True
    png = plan_file.parent / "r0001" / "preview.png"
    for layer in profile["layers"]:
        layer["classification_reviewed"] = True
        layer["conversion_reviewed"] = True
        layer["wms"] = {"status": "PASSED", "visual_reviewed": True, "png": str(png)}
    done = build_business_tree_sql(profile, tmp_path / "ready-sql")
    assert done["status"] == "SQL_GENERATED_NOT_EXECUTED"
    assert done["registered"] is False
    sql = Path(done["registration_sql"]).read_text(encoding="utf-8")
    node_sql = sql.split('INSERT INTO "review_nodes"', 1)[1]
    assert node_sql.index(parent["code"]) < node_sql.index("synthetic_faults")
    assert 'INSERT INTO "review_nodes"' in sql
    assert 'INSERT INTO "review_services"' in sql
    assert "COMMIT;" in sql
