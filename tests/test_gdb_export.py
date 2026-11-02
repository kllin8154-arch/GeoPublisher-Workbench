"""Offline tests for safe FileGDB -> SHP conversion. All data are synthetic."""

from pathlib import Path

import pytest

from geopublisher import gdb_export
from geopublisher.datasets import DatasetError, discover_sources


def _layer(name, count, fields, geometry="Point"):
    return {
        "name": name,
        "featureCount": count,
        "fields": fields,
        "geometryFields": [
            {
                "type": geometry,
                "coordinateSystem": {"projjson": {"id": {"authority": "EPSG", "code": 4508}}},
            }
        ],
    }


def _field(name, kind="String", width=None):
    return {"name": name, "type": kind, "width": width}


def _sample_metadata():
    return {
        "driverShortName": "OpenFileGDB",
        "layers": [
            _layer("_POINT", 2, [_field("Feature_Id"), _field("标准化色"), _field("LongFieldIdentifier")]),
            _layer("A_LABEL_A", 2, [_field("Element", "Binary"), _field("TextString")], "MultiPolygon"),
            _layer("EMPTY_PNT", 0, [_field("Value", "Integer")]),
            _layer("BROKEN", 4, [_field("Feature_Id")], "MultiLineString"),
        ],
    }


class FakeRuntime:
    def __init__(self):
        self.metadata = _sample_metadata()

    def _json(self, _command, args):
        item = Path(args[-1])
        if item.suffix == ".gdb":
            return self.metadata
        layer = next(
            entry for entry in self.metadata["layers"]
            if gdb_export._filename(entry["name"], set()) == item.stem
        )
        return {
            "layers": [{
                "featureCount": layer["featureCount"],
                "fields": [
                    {"name": alias["shp_field"]}
                    for alias in gdb_export._field_aliases(layer["fields"])
                    if not alias["omitted"]
                ],
                "geometryFields": [{
                    "coordinateSystem": {"projjson": {"id": {"code": 4508}}}
                }],
            }]
        }

    def probe(self):
        return {"version": "3.13.3"}


def test_aliases_ascii_only_unique_and_stable():
    originals = [
        _field("Feature_Id"), _field("Feature_Id"),
        _field("标准化色"), _field("MapGIS标注"),
        _field("Deformation_Type_NEW"), _field("Deformation_Name_NEW"),
    ]
    aliases = gdb_export._field_aliases(originals)
    assert len({a["shp_field"].casefold() for a in aliases}) == len(aliases)
    assert aliases == gdb_export._field_aliases(originals)
    assert aliases[0]["shp_field"] == "Feature_Id"
    for entry in aliases:
        assert len(entry["shp_field"].encode("ascii")) <= 10
    assert aliases[2]["renamed"] is True
    assert gdb_export._filename("_GEOPOLYGON", set()) == "_GEOPOLYGON"


def test_ogr_sql_quoted_field_names_and_unsupported_binary_excluded():
    mapped = gdb_export._field_aliases([
        _field("Feature_Id"),
        _field("Element", "Binary"),
        _field('test"column', "Integer"),
    ])
    sql = gdb_export._sql_query("_LAYER", mapped)
    assert '"Feature_Id" AS "Feature_Id"' in sql
    assert '"test""column" AS' in sql
    assert '"Element"' not in sql
    assert sql.endswith('FROM "_LAYER"')
    with pytest.raises(DatasetError, match="属性都不支持"):
        gdb_export._sql_query("X", gdb_export._field_aliases([_field("Element", "Binary")]))


def test_synthetic_convert_preserves_source_quarantines_failure_and_skips_annotation(tmp_path, monkeypatch):
    source = tmp_path / "lab.gdb"
    source.mkdir()
    (source / "a0001.gdbtable").write_bytes(b"synthetic source only")
    report_path = tmp_path / "out"
    before = gdb_export.gdb_fingerprint(source)

    def fake_convert(_runtime, _source, layer, shp):
        shp.parent.mkdir(parents=True, exist_ok=True)
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            shp.with_suffix(suffix).write_bytes(b"synthetic")
        if layer["name"] == "BROKEN":
            raise DatasetError("GDB_INDEX_READ_ANOMALY", "synthetic index reading error")

    monkeypatch.setattr(gdb_export, "_export_one", fake_convert)
    result = gdb_export.convert_gdb(FakeRuntime(), source, report_path)
    assert result["source_unchanged"] is True
    assert gdb_export.gdb_fingerprint(source) == before
    assert result["summary"] == {
        "EXPORTED_REVIEW_REQUIRED": 1,
        "SKIPPED_ANNOTATION": 1,
        "SKIPPED_EMPTY": 1,
        "FAILED": 1,
    }
    assert (report_path / "_POINT.shp").is_file()
    assert (report_path / "_POINT.prj").is_file()
    assert not (report_path / "BROKEN.shp").exists()
    assert (report_path / "artifacts" / "BROKEN" / "BROKEN.shp").exists()
    assert [file.name for file in discover_sources([report_path])] == ["_POINT.shp"]
    row = next(item for item in result["layers"] if item["source_layer"] == "_POINT")
    assert row["source_feature_count"] == row["output_feature_count"] == 2
    assert row["crs"] == "EPSG:4508"
    assert any(item["renamed"] for item in row["field_map"])
    broken = next(item for item in result["layers"] if item["source_layer"] == "BROKEN")
    assert broken["errors"][0]["code"] == "GDB_INDEX_READ_ANOMALY"
    assert broken["incomplete_artifact_quarantined"] is True
    assert (report_path / "conversion-report.json").exists()


def test_preflight_rejects_wrong_selection_and_reused_output(tmp_path):
    source = tmp_path / "valid.gdb"
    source.mkdir()
    (source / "a.gdbtable").write_bytes(b"test")
    output = tmp_path / "output"
    with pytest.raises(DatasetError) as exc:
        gdb_export.convert_gdb(FakeRuntime(), source, output, only_layers=["UNAVAILABLE"])
    assert exc.value.code == "GDB_LAYER_SELECTION_INVALID"
    assert not output.exists()
    output.mkdir()
    with pytest.raises(DatasetError) as exc:
        gdb_export.convert_gdb(FakeRuntime(), source, output)
    assert exc.value.code == "GDB_OUTPUT_EXISTS"


def test_metadata_marks_annotation_and_risks_without_reading_rows(tmp_path):
    source = tmp_path / "local.gdb"
    source.mkdir()
    info = gdb_export.inspect_gdb(FakeRuntime(), source)
    assert info["layer_count"] == 4
    assert info["feature_count"] == 8
    annotation = next(item for item in info["layers"] if item["name"] == "A_LABEL_A")
    assert annotation["annotation"]
    assert annotation["field_map"][0]["omitted"]
    assert annotation["crs"] == "EPSG:4508"
