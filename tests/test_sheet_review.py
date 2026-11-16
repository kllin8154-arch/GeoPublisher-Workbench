"""Synthetic-only logical map sheet review tests; no GIS data leaves the host."""
from __future__ import annotations

import copy
import json
import socket
from pathlib import Path

import pytest
from test_catalog_draft import fixture_plan

from geopublisher.business_tree import build_business_tree_sql
from geopublisher.catalog_draft import PENDING_GROUP, generate_catalog_draft
from geopublisher.config import ConfigError
from geopublisher.plans import fingerprint
from geopublisher.sheet_review import (
    sheet_group_code,
    sheet_review_template,
    validate_sheet_review,
    write_sheet_review,
)


def source_batch_plan(tmp_path: Path, *, different_source=False) -> Path:
    file = fixture_plan(tmp_path, with_report=False)
    plan = json.loads(file.read_text("utf-8"))
    plan["schema_version"] = 2
    from geopublisher.imports import prepare_imports
    from geopublisher.plans import digest_file

    for i, resource in enumerate(plan["resources"]):
        source = tmp_path / ("synthetic_source_" + str(i) if different_source else "source")
        source.mkdir(exist_ok=True)
        original = source / ("sample_" + str(i) + ".shp")
        original.write_bytes(b"synthetic-" + bytes([i]))
        marker = source / "conversion-report.json"
        marker.write_text("synthetic local batch", encoding="utf-8")
        resource["kind"] = "shapefile"
        resource["inspection"]["source"] = str(original)
        resource["inspection"]["fields"] = (
            [{"name": "SHEET_ID", "type": "String"}, {"name": "GEO_TYPE", "type": "String"}]
            if i == 0 else [{"name": "地质单元", "type": "String"}]
        )
        resource["batch_source"] = {
            "format": "GDB", "source_filename": "基础地质.gdb",
            "source_layer": ("地层面" if i == 0 else "地质界线"),
            "group_code": ("draft_batch_" + ("a" if not different_source or i == 0 else "b") * 16),
            "manifest_path": str(marker),
            "manifest_sha256": digest_file(marker),
        }
        resource["artifact"]["source_hashes"] = [
            {"path": str(original), "sha256": digest_file(original)},
            {"path": str(marker), "sha256": digest_file(marker)},
        ]
    plan["import_intent"] = prepare_imports([
        Path(item["inspection"]["source"]) for item in plan["resources"]
    ])
    plan["plan_hash"] = fingerprint({k: v for k, v in plan.items() if k != "plan_hash"})
    file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return file


def reviewed_rows(file: Path) -> list[dict]:
    items = sheet_review_template(file)["entries"]
    for item in items:
        item["status"] = "CONFIRMED_SINGLE"
        item["sheet_code"] = "H49E001006"
        item["sheet_title"] = "H49E001006 图幅"
    return items


def test_new_review_is_unapproved_with_only_candidate_field_names(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: pytest.fail("No network"))
    file = source_batch_plan(tmp_path)
    result = write_sheet_review(file, tmp_path / "unreviewed.json")
    assert result["pending"] == 2
    rows = json.loads((tmp_path / "unreviewed.json").read_text("utf-8"))["entries"]
    assert rows[0]["suggested_fields"] == ["SHEET_ID"]
    assert rows[1]["suggested_fields"] == []
    assert all(row["sheet_code"] == "" for row in rows)
    with pytest.raises(ConfigError):
        generate_catalog_draft(
            file, tmp_path / "must-not-exist",
            sheet_review_file=tmp_path / "unreviewed.json",
        )
    assert not (tmp_path / "must-not-exist").exists()


def test_two_layers_share_one_reviewed_sheet_and_keep_db_registration_blocked(tmp_path):
    file = source_batch_plan(tmp_path)
    reviewed = tmp_path / "reviewed.json"
    result = write_sheet_review(file, reviewed, entries=reviewed_rows(file))
    assert result["sheet_count"] == 1 and result["pending"] == 0
    output = generate_catalog_draft(
        file, tmp_path / "catalog", sheet_review_file=reviewed,
    )
    data = json.loads(Path(output["draft"]).read_text("utf-8"))
    assert output["batch_count"] == 1 and output["sheet_count"] == 1
    assert len(data["groups"]) == 3  # temporary root + source file + logical sheet
    source_group, sheet_group = data["groups"][1:]
    assert source_group["parent"] == PENDING_GROUP
    assert sheet_group["parent"] == source_group["code"]
    assert sheet_group["code"] == sheet_group_code(source_group["code"], "H49E001006")
    assert [layer["parent"] for layer in data["layers"]] == [sheet_group["code"]] * 2
    assert [layer["title"] for layer in data["layers"]] == ["地层面", "地质界线"]
    assert all(group["classification_reviewed"] is False for group in data["groups"])
    assert all(layer["wms"]["visual_reviewed"] is False for layer in data["layers"])
    review = json.loads(Path(output["report"]).read_text("utf-8"))
    assert review["sheet_count"] == 1
    assert review["batch_parents"][0]["children"] == 2
    sql = build_business_tree_sql(data, tmp_path / "blocked-sql")
    assert sql["status"] == "BLOCKED_PENDING_REVIEW"


def test_same_sheet_name_from_different_sources_never_merges(tmp_path):
    file = source_batch_plan(tmp_path, different_source=True)
    rows = reviewed_rows(file)
    report = tmp_path / "reviewed.json"
    write_sheet_review(file, report, entries=rows)
    output = generate_catalog_draft(file, tmp_path / "catalog", sheet_review_file=report)
    data = json.loads(Path(output["draft"]).read_text("utf-8"))
    assert output["sheet_count"] == 2
    parents = [node["parent"] for node in data["groups"][1:] if node["code"].startswith("draft_sheet_")]
    assert len(set(parents)) == 2


@pytest.mark.parametrize("corrupt", [
    "plan_tampered", "origin_modified", "row_duplicate", "title_conflict",
    "bad_code", "need_split", "pending", "wrong_id",
])
def test_mismatch_split_and_unreviewed_never_create_claimed_sheet(tmp_path, corrupt):
    file = source_batch_plan(tmp_path)
    entries = reviewed_rows(file)
    data = sheet_review_template(file)
    data["entries"] = entries
    if corrupt == "plan_tampered":
        data["plan_sha256"] = "0" * 64
    elif corrupt == "origin_modified":
        data["entries"][0]["source_layer"] = "spoofed"
    elif corrupt == "row_duplicate":
        data["entries"][1] = copy.deepcopy(data["entries"][0])
    elif corrupt == "title_conflict":
        data["entries"][1]["sheet_title"] = "冲突图幅"
    elif corrupt == "bad_code":
        data["entries"][0]["sheet_code"] = "../../escape"
    elif corrupt == "need_split":
        data["entries"][0].update(status="NEEDS_SPLIT", sheet_code="", sheet_title="")
    elif corrupt == "pending":
        data["entries"][0].update(status="PENDING", sheet_code="", sheet_title="")
    elif corrupt == "wrong_id":
        data["entries"][0]["resource_id"] = "r9999"
    reviewed = tmp_path / "bad.json"
    reviewed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigError):
        validate_sheet_review(file, reviewed)
    with pytest.raises(ConfigError):
        generate_catalog_draft(file, tmp_path / "blocked", sheet_review_file=reviewed)
    assert not (tmp_path / "blocked").exists()


def test_sheet_review_can_save_needs_split_but_cannot_use_it_to_register(tmp_path):
    file = source_batch_plan(tmp_path)
    rows = sheet_review_template(file)["entries"]
    rows[0]["status"] = "NEEDS_SPLIT"
    written = write_sheet_review(file, tmp_path / "review.json", entries=rows)
    assert written["needs_split"] == 1 and written["sheet_count"] == 0
    with pytest.raises(ConfigError):
        validate_sheet_review(file, written["file"])


def test_cli_creates_unapproved_local_review_then_uses_explicit_approved_file(tmp_path, capsys):
    from geopublisher.cli import main

    file = source_batch_plan(tmp_path)
    pending = tmp_path / "cli-unapproved.json"
    assert main(["sheet-review", str(file), "--output", str(pending)]) == 0
    assert pending.is_file()
    assert json.loads(pending.read_text("utf-8"))["entries"][0]["status"] == "PENDING"
    rows = reviewed_rows(file)
    reviewed = tmp_path / "cli-reviewed.json"
    write_sheet_review(file, reviewed, entries=rows)
    assert main([
        "catalog-draft", str(file), "--sheet-review", str(reviewed),
        "--output", str(tmp_path / "cli-catalog"),
    ]) == 0
    profile = json.loads((tmp_path / "cli-catalog" / "business_tree_draft.json").read_text("utf-8"))
    assert len(profile["groups"]) == 3
    assert all(group["classification_reviewed"] is False for group in profile["groups"])


def test_review_file_never_overwrites_existing_review(tmp_path):
    file = source_batch_plan(tmp_path)
    output = tmp_path / "sheet-review.json"
    write_sheet_review(file, output)
    original = output.read_bytes()
    with pytest.raises(ConfigError):
        write_sheet_review(file, output)
    assert output.read_bytes() == original
