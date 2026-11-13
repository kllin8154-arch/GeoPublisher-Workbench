"""Synthetic whole-GDB/MXD batch manifests; no real GIS or network access."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from geopublisher.conversion_batches import (
    conversion_origins,
    load_conversion_batch,
    verify_conversion_origins,
)
from geopublisher.datasets import DatasetError
from geopublisher.plans import digest_file


def _write_shp(folder: Path, name: str) -> list[dict]:
    files = []
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        file = folder / (name + suffix)
        file.write_bytes(b"synthetic-" + suffix.encode())
        files.append({"name": file.name, "sha256": digest_file(file)})
    return files


def _gdb(folder: Path) -> Path:
    folder.mkdir()
    first = _write_shp(folder, "sheet_01")
    second = _write_shp(folder, "sheet_02")
    report = {
        "schema_version": 1, "operation": "GDB_TO_SHP_LOCAL_ONLY",
        "source_unchanged": True, "destination": str(folder),
        "source_display_name": "多图幅.gdb", "layers": [
            {"source_layer": "图幅01", "output_basename": "sheet_01",
             "status": "EXPORTED_REVIEW_REQUIRED", "output_files": first},
            {"source_layer": "图幅02", "output_basename": "sheet_02",
             "status": "EXPORTED", "output_files": second},
            {"source_layer": "损坏图幅", "status": "FAILED"},
        ],
    }
    (folder / "conversion-report.json").write_text(json.dumps(report), encoding="utf-8")
    return folder


def _mxd(folder: Path) -> Path:
    folder.mkdir()
    _write_shp(folder, "sheet_a")
    _write_shp(folder, "sheet_b")
    # A partial failed SHP may exist, but it is NOT in the successful selection.
    (folder / "bad.shp").write_bytes(b"partial")
    report = {
        "schema_version": 1, "converter": "ArcMap-10x-local",
        "source_filename": "一幅多层.mxd", "uploaded": False,
        "items": [
            {"source_layer": "地层", "data_frame": "图框1", "status": "EXPORTED", "shp": "sheet_a.shp"},
            {"source_layer": "断层", "data_frame": "图框1", "status": "SHP_EXPORTED_LYR_FAILED",
             "shp": "sheet_b.shp"},
            {"source_layer": "破损", "data_frame": "图框2", "status": "FAILED", "shp": "bad.shp"},
        ],
    }
    (folder / "mxd-export-manifest.json").write_text(json.dumps(report), encoding="utf-8")
    return folder


@pytest.mark.parametrize("kind", ["gdb", "mxd"])
def test_whole_source_groups_only_successful_shp(tmp_path, kind):
    folder = _gdb(tmp_path / "gdb-export") if kind == "gdb" else _mxd(tmp_path / "mxd-export")
    batch = load_conversion_batch(folder)
    assert len(batch["layers"]) == 2
    assert len(batch["excluded"]) == 1
    assert {Path(x["shp_path"]).stem for x in batch["layers"]} == (
        {"sheet_01", "sheet_02"} if kind == "gdb" else {"sheet_a", "sheet_b"}
    )
    assert len({x["batch_id"] for x in batch["layers"]}) == 1
    mapped = conversion_origins([Path(x["shp_path"]) for x in batch["layers"]])
    resources = [{"inspection": {"source": path}, "conversion_origin": origin}
                 for path, origin in mapped.items()]
    verify_conversion_origins(resources)
    assert all(item["source_format"] == kind for item in mapped.values())


def test_gdb_changed_file_and_report_fail_closed(tmp_path):
    folder = _gdb(tmp_path / "gdb")
    first = load_conversion_batch(folder)["layers"][0]
    resources = [{
        "inspection": {"source": first["shp_path"]},
        "conversion_origin": {k: v for k, v in first.items() if k != "shp_path"},
    }]
    (folder / "sheet_01.dbf").write_bytes(b"tampered")
    with pytest.raises(DatasetError) as err:
        verify_conversion_origins(resources)
    assert err.value.code == "BATCH_OUTPUT_CHANGED"


def test_mxd_changed_report_requires_new_plan(tmp_path):
    folder = _mxd(tmp_path / "mxd")
    first = load_conversion_batch(folder)["layers"][0]
    resources = [{
        "inspection": {"source": first["shp_path"]},
        "conversion_origin": {k: v for k, v in first.items() if k != "shp_path"},
    }]
    report = folder / "mxd-export-manifest.json"
    report.write_text(report.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(DatasetError) as err:
        verify_conversion_origins(resources)
    assert err.value.code == "BATCH_LINEAGE_CHANGED"


def test_unrecorded_partial_shp_not_importable(tmp_path):
    folder = _mxd(tmp_path / "mxd")
    with pytest.raises(DatasetError) as err:
        conversion_origins([folder / "bad.shp"])
    assert err.value.code == "BATCH_UNRECORDED_SHP"


def test_reject_report_path_traversal_and_symlink(tmp_path):
    folder = _mxd(tmp_path / "mxd")
    report_path = folder / "mxd-export-manifest.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["items"][0]["shp"] = "..\\secret.shp"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(DatasetError) as err:
        load_conversion_batch(folder)
    assert err.value.code == "BATCH_INVALID_REPORT"


def test_publish_plan_freezes_gdb_parent_lineage_before_db_draft(tmp_path, monkeypatch):
    from geopublisher import datasets
    from geopublisher.config import ConfigError, Environment
    from geopublisher.plans import build_plan, load_plan, verify_artifacts

    folder = _gdb(tmp_path / "source-export")

    class SyntheticGdal:
        def __init__(self, *args, **kwargs):
            pass

        def probe(self):
            return {"version": "3.13.3"}

        def inspect(self, path, *, encoding=None, declared_crs=None):
            path = Path(path)
            return {
                "kind": "shapefile", "source": str(path),
                "components": [str(path.with_suffix(ext)) for ext in
                               (".shp", ".shx", ".dbf", ".prj", ".cpg")],
                "crs": "EPSG:4326", "encoding": "UTF-8",
                "native_bbox": [100, 20, 101, 21],
                "wgs84_bbox": [100, 20, 101, 21],
                "feature_count": 10, "geometry_type": "Point",
                "fields": [], "band_count": None, "warnings": [],
                "blocking_errors": [], "gdal_version": "3.13.3",
            }

    monkeypatch.setattr(datasets, "GdalRuntime", SyntheticGdal)
    env = Environment(
        "synthetic", "https://example.invalid/geoserver", "demo", str(tmp_path / "fakegdal"),
    )
    task = build_plan(env, [folder / "sheet_01.shp", folder / "sheet_02.shp"], tmp_path / "plan")
    assert task["ready"]
    assert {item["conversion_origin"]["source_name"] for item in task["resources"]} == {"多图幅.gdb"}
    assert {item["conversion_origin"]["batch_id"] for item in task["resources"]} == {
        task["resources"][0]["conversion_origin"]["batch_id"]
    }
    assert load_plan(tmp_path / "plan" / "plan.json") == task
    verify_artifacts(task)
    # Report may not be silently changed after approval and before network writes.
    manifest = folder / "conversion-report.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ConfigError) as err:
        verify_artifacts(task)
    assert err.value.code == "BATCH_LINEAGE_CHANGED"
