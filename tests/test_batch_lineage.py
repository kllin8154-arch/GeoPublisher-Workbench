"""Synthetic offline provenance for batch GDB/MXD import; never use site GIS data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from geopublisher.batch_lineage import conversion_batch
from geopublisher.config import ConfigError


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_gdb_multiple_exported_sheets_share_one_parent_and_preserve_original_titles(tmp_path):
    source = tmp_path / "converted"
    source.mkdir()
    files = [source / "sheet_01.shp", source / "sheet_02.shp"]
    for item in files:
        item.write_bytes(("synthetic-" + item.stem).encode())
    rows = []
    for index, file in enumerate(files, start=1):
        rows.append({
            "source_layer": f"第{index}幅",
            "output_basename": file.stem,
            "status": "EXPORTED_REVIEW_REQUIRED",
            "output_files": [{"name": file.name, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}],
        })
    _write(source / "conversion-report.json", {
        "schema_version": 1, "operation": "GDB_TO_SHP_LOCAL_ONLY",
        "source_display_name": "湖南基础地质.gdb", "destination": str(source),
        "layers": rows,
    })
    found = [
        conversion_batch(item, source_sha256=hashlib.sha256(item.read_bytes()).hexdigest())
        for item in files
    ]
    assert all(item["format"] == "GDB" for item in found)
    assert len({item["group_code"] for item in found}) == 1
    assert [item["source_layer"] for item in found] == ["第1幅", "第2幅"]
    assert all(item["source_filename"] == "湖南基础地质.gdb" for item in found)
    assert all("source_sha" not in item for item in found)


def test_mxd_multiple_map_frames_export_each_sheet_under_same_parent(tmp_path):
    folder = tmp_path / "arcmap_output"
    folder.mkdir()
    for name in ("S010", "S011"):
        (folder / (name + ".shp")).write_bytes(name.encode())
        (folder / (name + ".lyr")).write_bytes(b"synthetic-lyr")
    _write(folder / "mxd-export-manifest.json", {
        "schema_version": 1, "converter": "ArcMap-10x-local", "source_format": "MXD",
        "source_filename": "1比5万图幅集.mxd", "files_relative_to_output": True,
        "items": [
            {"source_layer": "图幅一", "data_frame": "main",
             "status": "EXPORTED", "shp": "S010.shp", "lyr": "S010.lyr"},
            {"source_layer": "图幅二", "data_frame": "main",
             "status": "EXPORTED", "shp": "S011.shp", "lyr": "S011.lyr"},
        ],
    })
    a = conversion_batch(folder / "S010.shp", source_sha256="a" * 64)
    b = conversion_batch(folder / "S011.shp", source_sha256="b" * 64)
    assert a["group_code"] == b["group_code"]
    assert a["format"] == "MXD"
    assert a["source_layer"] == "图幅一"
    assert b["source_layer"] == "图幅二"


@pytest.mark.parametrize("alter", [
    lambda report: report["layers"][0].update(status="FAILED"),
    lambda report: report["layers"][0].update(output_files=[]),
    lambda report: report["layers"].append(dict(report["layers"][0])),
    lambda report: report.update(source_display_name="../secrets.gdb"),
])
def test_gdb_missing_failed_duplicate_or_path_spoof_is_not_silently_accepted(tmp_path, alter):
    shp = tmp_path / "sheet.shp"
    shp.write_bytes(b"synthetic")
    digest = hashlib.sha256(shp.read_bytes()).hexdigest()
    report = {
        "operation": "GDB_TO_SHP_LOCAL_ONLY",
        "source_display_name": "test.gdb", "destination": str(tmp_path),
        "layers": [{"source_layer": "sheet", "output_basename": "sheet",
                    "status": "EXPORTED",
                    "output_files": [{"name": shp.name, "sha256": digest}]}],
    }
    alter(report)
    _write(tmp_path / "conversion-report.json", report)
    with pytest.raises(ConfigError) as err:
        conversion_batch(shp, source_sha256=digest)
    assert err.value.code == "BATCH_MANIFEST_INVALID"


def test_unrelated_shp_stays_unclassified(tmp_path):
    shp = tmp_path / "unrelated.shp"
    shp.write_bytes(b"synthetic")
    assert conversion_batch(shp, source_sha256="a" * 64) is None


def test_mxd_missing_matching_lyr_blocks_unreviewed_sheet(tmp_path):
    shp = tmp_path / "S010.shp"
    shp.write_bytes(b"synthetic")
    _write(tmp_path / "mxd-export-manifest.json", {
        "converter": "ArcMap-10x-local", "source_format": "MXD",
        "source_filename": "map.mxd", "files_relative_to_output": True,
        "items": [{"source_layer": "sheet1", "status": "EXPORTED",
                   "shp": "S010.shp", "lyr": "S010.lyr"}],
    })
    with pytest.raises(ConfigError):
        conversion_batch(shp, source_sha256="a" * 64)


def test_same_filename_in_different_conversion_jobs_never_merges_parent_nodes(tmp_path):
    origins = []
    for folder_name in ("job_a", "job_b"):
        folder = tmp_path / folder_name
        folder.mkdir()
        shp = folder / "sheet.shp"
        shp.write_bytes(folder_name.encode("ascii"))
        digest = hashlib.sha256(shp.read_bytes()).hexdigest()
        _write(folder / "conversion-report.json", {
            "operation": "GDB_TO_SHP_LOCAL_ONLY",
            "source_display_name": "same_name.gdb",
            "destination": str(folder),
            "layers": [{
                "source_layer": "同名图幅", "output_basename": "sheet",
                "status": "EXPORTED", "output_files": [{
                    "name": shp.name, "sha256": digest,
                }],
            }],
        })
        origins.append(conversion_batch(shp, source_sha256=digest))
    assert origins[0]["source_filename"] == origins[1]["source_filename"]
    assert origins[0]["group_code"] != origins[1]["group_code"]
