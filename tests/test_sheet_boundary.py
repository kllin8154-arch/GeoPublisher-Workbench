"""Real GDAL/GEOS coverage checks against locally generated point and polygon files."""
from __future__ import annotations

import json
import os
import subprocess

import pytest
from test_sheet_split import make_source

from geopublisher.datasets import DatasetError
from geopublisher.sheet_boundary import inspect_sheet_boundaries, save_sheet_boundary_audit
from geopublisher.sheet_split import inspect_sheet_split, split_sheet, verified_split_sources

pytest_plugins = ["test_sheet_split"]


def _ring(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]


def make_boundary(runtime, tmp_path, *, hole=False, missing=False,
                  duplicate=False, bad_geom=False, edge=False, extra=False,
                  crs="EPSG:4326"):
    bounds = tmp_path / "bounds"
    bounds.mkdir(exist_ok=True)
    outer = _ring(110.0, 25.0, 111.1, 26.1) if edge else _ring(109.9, 24.9, 111.1, 26.1)
    hole_ring = list(reversed(_ring(110.9, 25.9, 111.05, 26.05)))
    if bad_geom:
        outer = [[109.9, 24.9], [111.1, 26.1], [109.9, 26.1],
                 [111.1, 24.9], [109.9, 24.9]]
    first = {"type": "Feature", "properties": {"SHEET_ID": "H49E001006"},
             "geometry": {"type": "Polygon", "coordinates": [outer] + ([hole_ring] if hole else [])}}
    second = {"type": "Feature", "properties": {"SHEET_ID": "H49E001007"},
              "geometry": {"type": "Polygon", "coordinates": [_ring(111.9, 26.9, 112.1, 27.1)]}}
    features = [first] + ([] if missing else [second])
    if duplicate:
        features.append(first)
    if extra:
        features.extend({
            "type": "Feature", "properties": {"SHEET_ID": f"UNRELATED{i}"},
            "geometry": {"type": "Polygon",
                         "coordinates": [_ring(70 + i, 10, 70.2 + i, 10.2)]},
        } for i in range(30))
    obj = {"type": "FeatureCollection", "name": "bounds",
           "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
           "features": features}
    geo = bounds / "bounds.geojson"
    geo.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    shp = bounds / "bounds.shp"
    command = [
        str(runtime.bin_dir / ("ogr2ogr.exe" if os.name == "nt" else "ogr2ogr")),
        "-f", "ESRI Shapefile", "-lco", "ENCODING=UTF-8",
    ]
    if crs != "EPSG:4326":
        command += ["-t_srs", crs]
    command += [str(shp), str(geo)]
    result = subprocess.run(command, env=runtime._environment(), capture_output=True,
                            timeout=30, shell=False)
    assert result.returncode == 0, result.stderr
    return shp


def make_split(runtime, tmp_path, values=("H49E001006", "H49E001006", "H49E001007")):
    source = make_source(runtime, tmp_path, values)
    preview = inspect_sheet_split(runtime, source, "SHEET_ID")
    split_sheet(runtime, source, "SHEET_ID", tmp_path / "split",
                review_sha256=preview["review_sha256"])
    assert len(verified_split_sources(tmp_path / "split")) == 2
    return tmp_path / "split"


def test_polygon_coverage_passes_real_geometry_and_writes_local_only(runtime, tmp_path):
    root = make_split(runtime, tmp_path)
    bounds = make_boundary(runtime, tmp_path)
    audit = inspect_sheet_boundaries(runtime, root, bounds, "SHEET_ID")
    assert audit["status"] == "COVERED"
    assert audit["sheet_count"] == 2
    assert audit["outside_count"] == 0
    assert audit["topology_reviewed"] is False
    assert audit["wms_reviewed"] is False
    assert audit["database_registered"] is False
    assert audit["no_server_operation"] is True
    assert all(group["status"] == "COVERED" for group in audit["groups"])
    output = tmp_path / "audit"
    result = save_sheet_boundary_audit(audit, output)
    assert result["status"] == "COVERED"
    saved = json.loads((output / "boundary-review.json").read_text("utf-8"))
    assert saved["boundary_hashes"] == audit["boundary_hashes"]
    assert saved["split_manifest_sha256"] == audit["split_manifest_sha256"]
    assert not any(key in saved for key in ("wkt", "geometry"))


def test_inside_bbox_but_in_polygon_hole_still_blocks(runtime, tmp_path):
    root = make_split(runtime, tmp_path)
    bounds = make_boundary(runtime, tmp_path, hole=True)
    audit = inspect_sheet_boundaries(runtime, root, bounds, "SHEET_ID")
    assert audit["status"] == "BLOCKED_OUTSIDE_BOUNDARY"
    assert audit["outside_count"] == 1
    assert audit["groups"][0]["outside_count"] == 1
    report = save_sheet_boundary_audit(audit, tmp_path / "blocked-audit")
    assert report["status"] == "BLOCKED_OUTSIDE_BOUNDARY"


def test_missing_boundary_or_duplicate_sheet_is_rejected(runtime, tmp_path):
    root = make_split(runtime, tmp_path)
    missing = make_boundary(runtime, tmp_path, missing=True)
    with pytest.raises(DatasetError) as err:
        inspect_sheet_boundaries(runtime, root, missing, "SHEET_ID")
    assert err.value.code in {"BOUNDARY_COUNT_INCOMPLETE", "BOUNDARY_MISSING_SHEETS"}


def test_duplicate_boundary_sheet_id_is_rejected(runtime, tmp_path):
    root = make_split(runtime, tmp_path)
    bounds = make_boundary(runtime, tmp_path, duplicate=True)
    with pytest.raises(DatasetError) as err:
        inspect_sheet_boundaries(runtime, root, bounds, "SHEET_ID")
    assert err.value.code == "BOUNDARY_DUPLICATE_ID"


def test_invalid_polygon_cannot_pass_as_true_sheet(runtime, tmp_path):
    root = make_split(runtime, tmp_path)
    bounds = make_boundary(runtime, tmp_path, bad_geom=True)
    with pytest.raises(DatasetError) as err:
        inspect_sheet_boundaries(runtime, root, bounds, "SHEET_ID")
    assert err.value.code in {"BOUNDARY_POLYGON_INVALID", "BOUNDARY_SOURCE_BLOCKED"}


def test_wrong_crs_and_unreviewed_field_are_blocked(runtime, tmp_path):
    root = make_split(runtime, tmp_path)
    bounds = make_boundary(runtime, tmp_path, crs="EPSG:3857")
    with pytest.raises(DatasetError) as err:
        inspect_sheet_boundaries(runtime, root, bounds, "SHEET_ID")
    assert err.value.code == "BOUNDARY_CRS_MISMATCH"
    with pytest.raises(DatasetError) as err:
        inspect_sheet_boundaries(runtime, root, bounds, "NOT_SHEET_ID")
    assert err.value.code == "BOUNDARY_CRS_MISMATCH"


def test_audit_must_use_separate_new_directory(runtime, tmp_path):
    root = make_split(runtime, tmp_path)
    bounds = make_boundary(runtime, tmp_path)
    audit = inspect_sheet_boundaries(runtime, root, bounds, "SHEET_ID")
    with pytest.raises(DatasetError):
        save_sheet_boundary_audit(audit, root / "inplace")
    with pytest.raises(DatasetError):
        save_sheet_boundary_audit(audit, bounds.parent / "inplace")
    (tmp_path / "existing").mkdir()
    with pytest.raises(DatasetError):
        save_sheet_boundary_audit(audit, tmp_path / "existing")


def test_point_on_polygon_border_is_covered_not_outside(runtime, tmp_path):
    directory = make_split(runtime, tmp_path)
    boundary = make_boundary(runtime, tmp_path, edge=True)
    result = inspect_sheet_boundaries(runtime, directory, boundary, "SHEET_ID")
    assert result["status"] == "COVERED"
    assert result["outside_count"] == 0


def test_reference_grid_can_contain_many_unrelated_sheets(runtime, tmp_path):
    directory = make_split(runtime, tmp_path)
    boundary = make_boundary(runtime, tmp_path, extra=True)
    result = inspect_sheet_boundaries(runtime, directory, boundary, "SHEET_ID")
    assert result["status"] == "COVERED"
    assert result["sheet_count"] == 2


def test_unsafe_or_unknown_field_cannot_inject_sql(runtime, tmp_path):
    directory = make_split(runtime, tmp_path)
    boundary = make_boundary(runtime, tmp_path)
    for field in ("NO_SUCH_COLUMN", 'SHEET_ID" OR 1=1--'):
        with pytest.raises(DatasetError) as err:
            inspect_sheet_boundaries(runtime, directory, boundary, field)
        assert err.value.code == "BOUNDARY_FIELD_INVALID"
    from geopublisher.sheet_boundary import _polygon_wkt

    for text in ("POLYGON EMPTY", "POINT(0 0)", "POLYGON((0 0));DELETE FROM x", "x' y"):
        with pytest.raises(DatasetError):
            _polygon_wkt(text)


def test_tampered_split_components_are_rejected_before_boundary_query(runtime, tmp_path):
    directory = make_split(runtime, tmp_path)
    boundary = make_boundary(runtime, tmp_path)
    result = inspect_sheet_boundaries(runtime, directory, boundary, "SHEET_ID")
    assert result["status"] == "COVERED"
    child = verified_split_sources(directory)[0].with_suffix(".dbf")
    with child.open("ab") as stream:
        stream.write(b"modified-after-verification")
    with pytest.raises(DatasetError) as err:
        inspect_sheet_boundaries(runtime, directory, boundary, "SHEET_ID")
    assert err.value.code == "SPLIT_MANIFEST_INVALID"


def test_cli_readonly_preview_and_explicit_report_does_not_require_server(runtime, tmp_path, capsys):
    from geopublisher.cli import main

    directory = make_split(runtime, tmp_path)
    boundary = make_boundary(runtime, tmp_path)
    args = ["sheet-boundary-check", str(directory), "--boundary", str(boundary),
            "--field", "SHEET_ID", "--gdal-bin", str(runtime.bin_dir)]
    assert main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "COVERED"
    assert payload["report_created"] is False
    assert not (tmp_path / "review").exists()
    assert main(args + ["--output", str(tmp_path / "review")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outside_count"] == 0
    assert (tmp_path / "review" / "boundary-review.json").is_file()
    assert main(args + ["--output", str(tmp_path / "review")]) == 2
    assert "BOUNDARY_OUTPUT_EXISTS" in capsys.readouterr().out


def test_cli_returns_nonzero_and_saves_blocking_audit_for_polygon_hole(runtime, tmp_path, capsys):
    from geopublisher.cli import main

    directory = make_split(runtime, tmp_path)
    boundary = make_boundary(runtime, tmp_path, hole=True)
    status = main([
        "sheet-boundary-check", str(directory), "--boundary", str(boundary),
        "--field", "SHEET_ID", "--gdal-bin", str(runtime.bin_dir),
        "--output", str(tmp_path / "blocked"),
    ])
    assert status == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["outside_count"] == 1
    assert (tmp_path / "blocked" / "boundary-review.json").is_file()
