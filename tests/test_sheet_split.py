"""Real native GDAL tests on generated geometries; no site or online GIS data."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from geopublisher.batch_lineage import conversion_batch, verified_batch_sources
from geopublisher.datasets import DatasetError, GdalRuntime
from geopublisher.plans import digest_file
from geopublisher.sheet_split import (
    _basename,
    _bbox_within,
    _predicate,
    inspect_sheet_split,
    split_sheet,
    verified_split_sources,
)


@pytest.fixture(scope="module")
def runtime():
    root = Path(__file__).resolve().parents[1]
    expected = root / ".pixi" / "envs" / "default"
    bin_dir = expected / "Library" / "bin" if os.name == "nt" else expected / "bin"
    if not bin_dir.is_dir():
        exe = shutil.which("ogrinfo")
        if exe is None:
            pytest.skip("No local GDAL available for synthetic vector integration")
        bin_dir = Path(exe).parent
    try:
        candidate = GdalRuntime(bin_dir)
        candidate.probe()
        if not (bin_dir / ("ogr2ogr.exe" if os.name == "nt" else "ogr2ogr")).is_file():
            pytest.skip("GDAL ogr2ogr is not bundled")
        return candidate
    except DatasetError:
        pytest.skip("GDAL runtime is unavailable")


def make_source(runtime, tmp_path, values=("H49E001006", "H49E001006", "H49E001007")):
    directory = tmp_path / "input"
    directory.mkdir(parents=True)
    features = [{
        "type": "Feature",
        "properties": {"SHEET_ID": code, "LABEL": f"row{i}"},
        "geometry": {"type": "Point", "coordinates": [110.0 + i, 25.0 + i]},
    } for i, code in enumerate(values)]
    content = {
        "type": "FeatureCollection",
        "name": "synthetic",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
        "features": features,
    }
    geo = directory / "synthetic.geojson"
    geo.write_text(json.dumps(content), encoding="utf-8")
    shp = directory / "synthetic.shp"
    run = subprocess.run([
        str(runtime.bin_dir / ("ogr2ogr.exe" if os.name == "nt" else "ogr2ogr")),
        "-f", "ESRI Shapefile", "-lco", "ENCODING=UTF-8", str(shp), str(geo),
    ], env=runtime._environment(), capture_output=True, timeout=30, shell=False)
    assert run.returncode == 0
    return shp


def test_native_split_conserves_counts_fields_extent_and_immutability(runtime, tmp_path):
    source = make_source(runtime, tmp_path)
    before = {ext: digest_file(source.with_suffix(ext))
              for ext in (".shp", ".dbf", ".shx", ".prj", ".cpg")}
    preview = inspect_sheet_split(runtime, source, "SHEET_ID")
    assert preview["source_count"] == 3
    assert [(g["sheet_code"], g["feature_count"]) for g in preview["groups"]] == [
        ("H49E001006", 2), ("H49E001007", 1),
    ]
    assert not (tmp_path / "export").exists()
    report = split_sheet(
        runtime, source, "SHEET_ID", tmp_path / "export",
        review_sha256=preview["review_sha256"],
    )
    assert report["status"] == "COMPLETE_VERIFIED"
    assert report["database_registered"] is False
    assert report["geoserver_published"] is False
    assert report["full_sheet_boundary_verified"] is False
    assert len(verified_split_sources(tmp_path / "export")) == 2
    assert verified_batch_sources(tmp_path / "export") == verified_split_sources(tmp_path / "export")
    assert [r["feature_count"] for r in report["groups"]] == [2, 1]
    assert all(_bbox_within(g["bbox"], report["source_bbox"]) for g in report["groups"])
    assert all(digest_file(source.with_suffix(ext)) == sha for ext, sha in before.items())
    assert not (tmp_path / "export" / "_partial").exists()
    assert all((tmp_path / "export" / g["shp"]).is_file() for g in report["groups"])
    import csv

    with (tmp_path / "export" / "sheet-split-summary.csv").open(
        encoding="utf-8-sig", newline="",
    ) as stream:
        rows = list(csv.reader(stream))
    assert len(rows) == 3
    assert rows[0][0] == "图幅编号"
    assert [row[1] for row in rows[1:]] == ["2", "1"]


def test_split_wrong_review_hash_never_creates_destination(runtime, tmp_path):
    source = make_source(runtime, tmp_path)
    with pytest.raises(DatasetError) as exc:
        split_sheet(runtime, source, "SHEET_ID", tmp_path / "no-write",
                    review_sha256="0" * 64)
    assert exc.value.code == "SPLIT_REVIEW_CHANGED"
    assert not (tmp_path / "no-write").exists()


def test_null_unassigned_sheets_cannot_be_silently_discarded(runtime, tmp_path):
    source = make_source(runtime, tmp_path, ("H49E001006", None, "H49E001007"))
    with pytest.raises(DatasetError) as exc:
        inspect_sheet_split(runtime, source, "SHEET_ID")
    assert exc.value.code == "SPLIT_VALUE_INVALID"


def test_malicious_sheet_value_blocks_before_any_output(runtime, tmp_path):
    source = make_source(runtime, tmp_path, ("H49E001006", "007' OR 1=1 --"))
    with pytest.raises(DatasetError) as exc:
        inspect_sheet_split(runtime, source, "SHEET_ID")
    assert exc.value.code == "SPLIT_VALUE_INVALID"


def test_field_must_exist_and_single_sheet_does_not_split(runtime, tmp_path):
    source = make_source(runtime, tmp_path, ("H49E001006", "H49E001006"))
    with pytest.raises(DatasetError) as exc:
        inspect_sheet_split(runtime, source, "SHEET_ID\" OR 1=1")
    assert exc.value.code == "SPLIT_FIELD_INVALID"
    with pytest.raises(DatasetError) as exc:
        inspect_sheet_split(runtime, source, "SHEET_ID")
    assert exc.value.code == "SPLIT_SINGLE_VALUE"


def test_changed_source_requires_new_review(runtime, tmp_path):
    source = make_source(runtime, tmp_path)
    preview = inspect_sheet_split(runtime, source, "SHEET_ID")
    # Native source hash includes mutable DBF and changes even if SHP geometry stays.
    with source.with_suffix(".dbf").open("ab") as stream:
        stream.write(b"synthetic-modification")
    with pytest.raises(DatasetError) as exc:
        split_sheet(runtime, source, "SHEET_ID", tmp_path / "blocked",
                    review_sha256=preview["review_sha256"])
    assert exc.value.code == "SPLIT_REVIEW_CHANGED"
    assert not (tmp_path / "blocked").exists()


def test_completed_manifest_blocks_modified_export(runtime, tmp_path):
    source = make_source(runtime, tmp_path)
    preview = inspect_sheet_split(runtime, source, "SHEET_ID")
    result = split_sheet(runtime, source, "SHEET_ID", tmp_path / "export",
                         review_sha256=preview["review_sha256"])
    file = tmp_path / "export" / result["groups"][0]["shp"]
    with file.open("ab") as stream:
        stream.write(b"modified")
    with pytest.raises(DatasetError) as exc:
        verified_split_sources(tmp_path / "export")
    assert exc.value.code == "SPLIT_MANIFEST_INVALID"


def test_split_preserves_verified_gdb_parent_lineage(runtime, tmp_path):
    source = make_source(runtime, tmp_path)
    original_manifest = source.parent / "conversion-report.json"
    original_manifest.write_text(json.dumps({
        "schema_version": 1,
        "operation": "GDB_TO_SHP_LOCAL_ONLY",
        "source_display_name": "synthetic.gdb",
        "destination": str(source.parent),
        "layers": [{
            "status": "EXPORTED", "source_layer": "faults",
            "output_basename": source.stem,
            "output_files": [{"name": source.name, "sha256": digest_file(source)}],
        }],
    }), encoding="utf-8")
    parent = conversion_batch(source, source_sha256=digest_file(source))
    preview = inspect_sheet_split(runtime, source, "SHEET_ID")
    report = split_sheet(
        runtime, source, "SHEET_ID", tmp_path / "export",
        review_sha256=preview["review_sha256"],
    )
    assert report["source_batch"] == parent
    exported = verified_batch_sources(tmp_path / "export")
    assert len(exported) == 2
    batches = [conversion_batch(path, source_sha256=digest_file(path)) for path in exported]
    assert all(item is not None for item in batches)
    assert {item["group_code"] for item in batches} == {parent["group_code"]}
    assert all(item["source_filename"] == "synthetic.gdb" for item in batches)
    assert {item["source_layer"].rsplit(" / 图幅 ", 1)[-1] for item in batches} == {
        "H49E001006", "H49E001007",
    }


def test_cancel_safe_checkpoint_leaves_no_valid_manifest(runtime, tmp_path):
    source = make_source(runtime, tmp_path)
    preview = inspect_sheet_split(runtime, source, "SHEET_ID")
    with pytest.raises(DatasetError) as exc:
        split_sheet(runtime, source, "SHEET_ID", tmp_path / "cancelled",
                    review_sha256=preview["review_sha256"], cancelled=lambda: True)
    assert exc.value.code == "SPLIT_CANCELLED"
    assert not (tmp_path / "cancelled" / "sheet-split-manifest.json").exists()
    with pytest.raises(DatasetError):
        verified_split_sources(tmp_path / "cancelled")


def test_output_collision_and_path_risk_refuse_overwrite(runtime, tmp_path):
    source = make_source(runtime, tmp_path)
    preview = inspect_sheet_split(runtime, source, "SHEET_ID")
    with pytest.raises(DatasetError):
        split_sheet(runtime, source, "SHEET_ID", source.parent,
                    review_sha256=preview["review_sha256"])
    assert (source.parent / "synthetic.shp").is_file()
    (tmp_path / "already").mkdir()
    with pytest.raises(DatasetError):
        split_sheet(runtime, source, "SHEET_ID", tmp_path / "already",
                    review_sha256=preview["review_sha256"])


def test_safe_names_and_bounding_extent_validation():
    assert _basename(Path("测试中文.shp"), "H49E001006").startswith("layer__H49E001006")
    assert _bbox_within([1, 2, 3, 4], [0, 0, 5, 5])
    assert not _bbox_within([-1, 2, 3, 4], [0, 0, 5, 5])
    with pytest.raises(DatasetError):
        _predicate("SHEET_ID", "x' OR 1=1", "String")


def test_sheet_split_cli_preview_then_requires_review_digest(runtime, tmp_path, capsys):
    from geopublisher.cli import main

    source = make_source(runtime, tmp_path)
    bin_path = str(runtime.bin_dir)
    assert main(["sheet-split", str(source), "--field", "SHEET_ID",
                 "--gdal-bin", bin_path, "--preview"]) == 0
    preview_text = capsys.readouterr().out
    preview = json.loads(preview_text)
    assert preview["status"] == "SHEET_SPLIT_READ_ONLY"
    assert preview["source_count"] == 3
    output = tmp_path / "from-cli"
    assert main(["sheet-split", str(source), "--field", "SHEET_ID",
                 "--gdal-bin", bin_path, "--output", str(output)]) != 0
    assert not output.exists()
    assert main(["sheet-split", str(source), "--field", "SHEET_ID",
                 "--gdal-bin", bin_path, "--output", str(output),
                 "--review-sha256", preview["review_sha256"]]) == 0
    assert len(verified_split_sources(output)) == 2


def test_published_split_plan_freezes_original_gdb_source_and_manifest(runtime, tmp_path):
    from geopublisher.config import Environment
    from geopublisher.plans import build_plan, verify_artifacts

    source = make_source(runtime, tmp_path)
    original_manifest = source.parent / "conversion-report.json"
    original_manifest.write_text(json.dumps({
        "schema_version": 1,
        "operation": "GDB_TO_SHP_LOCAL_ONLY",
        "source_display_name": "synthetic.gdb",
        "destination": str(source.parent),
        "layers": [{
            "status": "EXPORTED", "source_layer": "faults",
            "output_basename": source.stem,
            "output_files": [{"name": source.name, "sha256": digest_file(source)}],
        }],
    }), encoding="utf-8")
    preview = inspect_sheet_split(runtime, source, "SHEET_ID")
    split_sheet(
        runtime, source, "SHEET_ID", tmp_path / "export",
        review_sha256=preview["review_sha256"],
    )
    children = verified_batch_sources(tmp_path / "export")
    environment = Environment(
        "synthetic", "http://127.0.0.1:8080/geoserver", "demo",
        str(runtime.bin_dir), gdal_version=None,
    )
    plan = build_plan(environment, children, tmp_path / "task-verified")
    assert plan["ready"] is True
    assert len(plan["resources"]) == 2
    for item in plan["resources"]:
        assert item["batch_source"]["source_filename"] == "synthetic.gdb"
        frozen = item["artifact"]["source_hashes"]
        assert any(r["path"] == str(original_manifest) for r in frozen)
        assert any(r["path"] == str(source.with_suffix(".dbf")) for r in frozen)
        assert any(r["path"] == str(tmp_path / "export" / "sheet-split-manifest.json")
                   for r in frozen)
        assert any(r["path"] == str(tmp_path / "export" / "sheet-split-summary.csv")
                   for r in frozen)
    verify_artifacts(plan)
    from geopublisher.catalog_draft import generate_catalog_draft
    from geopublisher.sheet_review import sheet_review_template, write_sheet_review

    plan_file = tmp_path / "task-verified" / "plan.json"
    sheet_rows = sheet_review_template(plan_file)["entries"]
    assert len(sheet_rows) == 2
    for entry in sheet_rows:
        code = entry["source_layer"].rsplit(" / 图幅 ", 1)[-1]
        entry.update(
            status="CONFIRMED_SINGLE",
            sheet_code=code,
            sheet_title=f"已人工核对：{code}",
        )
    reviewed = tmp_path / "reviewed-sheets.json"
    write_sheet_review(plan_file, reviewed, entries=sheet_rows)
    draft_result = generate_catalog_draft(
        plan_file, tmp_path / "catalog", sheet_review_file=reviewed,
    )
    assert draft_result["batch_count"] == 1
    assert draft_result["sheet_count"] == 2
    catalog = json.loads(Path(draft_result["draft"]).read_text(encoding="utf-8"))
    assert len({r["parent"] for r in catalog["layers"]}) == 2
    assert all(r["classification_reviewed"] is False for r in catalog["layers"])
    assert catalog["schema_verified"] is False
    with source.with_suffix(".dbf").open("ab") as stream:
        stream.write(b"subsequent-source-change")
    with pytest.raises(Exception) as exc:
        verify_artifacts(plan)
    assert getattr(exc.value, "code", None) == "SOURCE_CHANGED"


def test_offline_chinese_space_paths_are_supported(runtime, tmp_path):
    source = make_source(runtime, tmp_path / "中文 目录")
    preview = inspect_sheet_split(runtime, source, "SHEET_ID")
    output = tmp_path / "中文 产物"
    result = split_sheet(runtime, source, "SHEET_ID", output,
                         review_sha256=preview["review_sha256"])
    assert result["status"] == "COMPLETE_VERIFIED"
    assert len(verified_split_sources(output)) == 2


def test_over_hundred_sheet_ids_stops_before_writes(runtime, tmp_path):
    source = make_source(
        runtime, tmp_path, tuple(f"S{i:03d}" for i in range(101)),
    )
    with pytest.raises(DatasetError) as error:
        inspect_sheet_split(runtime, source, "SHEET_ID")
    assert error.value.code == "SPLIT_TOO_MANY_SHEETS"


def test_integer_sheet_field_exact_counts(runtime, tmp_path):
    source = make_source(runtime, tmp_path, (101, 101, 102))
    preview = inspect_sheet_split(runtime, source, "SHEET_ID")
    assert preview["field_type"] in {"Integer", "Integer64"}
    assert [x["sheet_code"] for x in preview["groups"]] == ["101", "102"]
    result = split_sheet(runtime, source, "SHEET_ID", tmp_path / "split-int",
                         review_sha256=preview["review_sha256"])
    assert sum(x["feature_count"] for x in result["groups"]) == 3


def test_missing_cpg_requires_explicit_encoding_and_preserves_source(runtime, tmp_path):
    source = make_source(runtime, tmp_path)
    source.with_suffix(".cpg").unlink()
    with pytest.raises(DatasetError) as exc:
        inspect_sheet_split(runtime, source, "SHEET_ID")
    assert exc.value.code == "SPLIT_COMPONENT_MISSING"
    preview = inspect_sheet_split(runtime, source, "SHEET_ID", encoding="UTF-8")
    result = split_sheet(
        runtime, source, "SHEET_ID", tmp_path / "encoded",
        encoding="UTF-8", review_sha256=preview["review_sha256"],
    )
    assert result["source_unchanged"] is True
    assert not source.with_suffix(".cpg").exists()
    assert all(path.with_suffix(".cpg").exists() for path in
               verified_split_sources(tmp_path / "encoded"))
