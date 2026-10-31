from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from geopublisher import datasets
from geopublisher.datasets import (
    DatasetError,
    GdalRuntime,
    discover_sources,
    hash_file,
    package_dataset,
)

WKT = 'GEOGCS["WGS 84",DATUM["WGS_1984"],AUTHORITY["EPSG","4326"]]'


def vector_metadata(crs=True):
    geometry = {"type": "Point", "extent": [10, 20, 11, 21]}
    if crs:
        geometry["coordinateSystem"] = {"wkt": WKT, "projjson": {"id": {"authority": "EPSG", "code": 4326}}}
    return {
        "driverShortName": "ESRI Shapefile",
        "layers": [
            {
                "name": "points",
                "geometryFields": [geometry],
                "featureCount": 2,
                "fields": [{"name": "name", "type": "String", "width": 80}],
            }
        ],
    }


def make_shp(tmp_path, *, cpg=True, prj=True):
    directory = tmp_path / "中文 空格"
    directory.mkdir(exist_ok=True)
    source = directory / "points.shp"
    source.write_bytes(b"\x00\x00\x27\x0a" + b"\0" * 96)
    source.with_suffix(".shx").write_bytes(b"shx-content")
    source.with_suffix(".dbf").write_bytes(b"dbf-content")
    if cpg:
        source.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
    if prj:
        source.with_suffix(".prj").write_text(WKT, encoding="ascii")
    return source


def runtime_for(tmp_path, monkeypatch, metadata):
    runtime = GdalRuntime(tmp_path)
    runtime._capability = {"version": "3.13.3"}
    monkeypatch.setattr(runtime, "_json", lambda *args: metadata)
    return runtime


def error_codes(info):
    return {item["code"] for item in info["blocking_errors"]}


def test_explicit_runtime_never_silently_uses_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(DatasetError) as error:
        GdalRuntime(None)
    assert error.value.code == "GDAL_NOT_CONFIGURED"


def test_probe_rejects_wrong_runtime_version(tmp_path, monkeypatch):
    runtime = GdalRuntime(tmp_path, expected_version="3.13.3")
    monkeypatch.setattr(runtime, "_run", lambda *args: "GDAL 3.12.0")
    with pytest.raises(DatasetError) as error:
        runtime.probe()
    assert error.value.code == "GDAL_VERSION_MISMATCH"


def test_runtime_environment_disables_network_and_foreign_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJ_NETWORK", "ON")
    monkeypatch.setenv("GDAL_DATA", "/unexpected/qgis/share")
    monkeypatch.setenv("SHAPE_RESTORE_SHX", "YES")
    environment = GdalRuntime(tmp_path)._environment()
    assert environment["PROJ_NETWORK"] == "OFF"
    assert environment["SHAPE_RESTORE_SHX"] == "NO"
    assert environment.get("GDAL_DATA") != "/unexpected/qgis/share"
    assert environment["GDAL_PAM_ENABLED"] == "NO"


def test_inspection_handles_chinese_paths_and_known_encoding(tmp_path, monkeypatch):
    source = make_shp(tmp_path)
    info = runtime_for(tmp_path, monkeypatch, vector_metadata()).inspect(source)
    assert not info["blocking_errors"]
    assert info["encoding"] == "UTF-8"
    assert info["crs"] == "EPSG:4326"
    assert info["wgs84_bbox"] == [10, 20, 11, 21]
    assert info["geometry_type"] == "Point"
    assert info["feature_count"] == 2


def test_unknown_encoding_requires_confirmation(tmp_path, monkeypatch):
    source = make_shp(tmp_path, cpg=False)
    runtime = runtime_for(tmp_path, monkeypatch, vector_metadata())
    assert "ENCODING_REQUIRED" in error_codes(runtime.inspect(source))
    assert "ENCODING_REQUIRED" not in error_codes(runtime.inspect(source, encoding="GBK"))
    assert not source.with_suffix(".cpg").exists()


def test_missing_component_blocks_without_gdal_read(tmp_path, monkeypatch):
    source = make_shp(tmp_path)
    source.with_suffix(".shx").unlink()
    runtime = runtime_for(tmp_path, monkeypatch, vector_metadata())
    monkeypatch.setattr(
        runtime, "_json", lambda *args: pytest.fail("Missing SHX should be caught before GDAL")
    )
    assert "MISSING_COMPONENT" in error_codes(runtime.inspect(source))


def test_explicit_crs_only_assigns_prepared_shapefile_copy(tmp_path, monkeypatch):
    source = make_shp(tmp_path, prj=False)
    runtime = runtime_for(tmp_path, monkeypatch, vector_metadata(crs=False))
    monkeypatch.setattr(runtime, "_run", lambda *args, **kwargs: WKT)
    assert "CRS_REQUIRED" in error_codes(runtime.inspect(source))
    info = runtime.inspect(source, declared_crs="EPSG:4326")
    assert not info["blocking_errors"]
    artifact = package_dataset(info, tmp_path / "prepared.zip")
    with zipfile.ZipFile(artifact["path"]) as archive:
        assert archive.read("points.prj").decode() == WKT
    assert not source.with_suffix(".prj").exists()


def test_declared_crs_cannot_override_native_crs(tmp_path, monkeypatch):
    source = make_shp(tmp_path)
    runtime = runtime_for(tmp_path, monkeypatch, vector_metadata())
    assert "CRS_CONFLICT" in error_codes(runtime.inspect(source, declared_crs="EPSG:3857"))


def test_vrt_masquerading_as_tiff_is_not_opened(tmp_path, monkeypatch):
    source = tmp_path / "danger.tif"
    source.write_text(
        "<VRTDataset><SourceFilename>/vsicurl/https://example.invalid/data</SourceFilename></VRTDataset>"
    )
    runtime = runtime_for(tmp_path, monkeypatch, {})
    monkeypatch.setattr(runtime, "_json", lambda *args: pytest.fail("Must reject before GDAL"))
    assert "INVALID_FILE_SIGNATURE" in error_codes(runtime.inspect(source))


def test_raster_sidecars_block_single_file_upload(tmp_path, monkeypatch):
    source = tmp_path / "raster.tif"
    source.write_bytes(b"II*\x00" + b"\0" * 32)
    source.with_name(source.name + ".ovr").write_bytes(b"external-overviews")
    runtime = runtime_for(tmp_path, monkeypatch, {})
    monkeypatch.setattr(runtime, "_json", lambda *args: pytest.fail("Sidecar should block before GDAL"))
    assert "EXTERNAL_RASTER_DEPENDENCY" in error_codes(runtime.inspect(source))


def test_raster_missing_embedded_crs_is_not_fixed_by_service_declaration(tmp_path, monkeypatch):
    source = tmp_path / "raster.tif"
    source.write_bytes(b"II*\x00" + b"\0" * 32)
    metadata = {
        "driverShortName": "GTiff",
        "size": [100, 100],
        "geoTransform": [10, 0.01, 0, 21, 0, -0.01],
        "bands": [{"band": 1, "type": "Byte"}],
    }
    runtime = runtime_for(tmp_path, monkeypatch, metadata)
    assert "RASTER_CRS_EMBED_REQUIRED" in error_codes(runtime.inspect(source, declared_crs="EPSG:4326"))


def test_multiple_tiffs_are_discovered_individually_and_deduplicated(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for name in ("one.tif", "two.TIFF"):
        (data / name).write_bytes(b"x")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "ignored.tif").write_bytes(b"x")
    assert discover_sources([tmp_path, data / "one.tif"]) == [data / "one.tif", data / "two.TIFF"]


def test_explicit_unsupported_input_is_not_silently_ignored(tmp_path):
    path = tmp_path / "file.qml"
    path.write_text("<qgis/>")
    with pytest.raises(DatasetError) as error:
        discover_sources([path])
    assert error.value.code == "UNSUPPORTED_FORMAT"


def test_symlink_sources_rejected_and_directory_links_not_followed(tmp_path):
    source = tmp_path / "real.tif"
    source.write_bytes(b"x")
    linked = tmp_path / "linked.tif"
    try:
        linked.symlink_to(source)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    assert discover_sources([tmp_path]) == [source]
    with pytest.raises(DatasetError) as error:
        discover_sources([linked])
    assert error.value.code == "SYMLINK_NOT_ALLOWED"


def test_package_is_immutable_and_preserves_source_bytes(tmp_path, monkeypatch):
    source = make_shp(tmp_path)
    info = runtime_for(tmp_path, monkeypatch, vector_metadata()).inspect(source, encoding="GBK")
    before = {path: hash_file(path) for path in map(Path, info["components"])}
    artifact = package_dataset(info, tmp_path / "artifacts")
    with zipfile.ZipFile(artifact["path"]) as archive:
        assert archive.read("points.cpg") == b"GBK"
        assert sorted(archive.namelist()) == [
            "points.cpg",
            "points.dbf",
            "points.prj",
            "points.shp",
            "points.shx",
        ]
    assert before == {path: hash_file(path) for path in before}
    assert artifact["sha256"] == hash_file(artifact["path"])
    assert artifact["size"] == Path(artifact["path"]).stat().st_size
    with pytest.raises(DatasetError) as error:
        package_dataset(info, Path(artifact["path"]))
    assert error.value.code == "ARTIFACT_EXISTS"


def test_packaging_detects_source_mutation(tmp_path, monkeypatch):
    source = make_shp(tmp_path)
    info = runtime_for(tmp_path, monkeypatch, vector_metadata()).inspect(source)
    original_hash = datasets.hash_file
    calls = 0

    def mutate_after_read(filename):
        nonlocal calls
        if Path(filename) == source:
            calls += 1
            if calls == 2:
                source.write_bytes(source.read_bytes() + b"changed")
        return original_hash(filename)

    monkeypatch.setattr(datasets, "hash_file", mutate_after_read)
    with pytest.raises(DatasetError) as error:
        package_dataset(info, tmp_path / "prepared.zip")
    assert error.value.code == "SOURCE_CHANGED"
    assert not (tmp_path / "prepared.zip").exists()
    assert not list(tmp_path.glob(".geopub-*.part"))


def test_zip_is_reproducible_despite_source_timestamp_changes(tmp_path, monkeypatch):
    import os

    source = make_shp(tmp_path)
    info = runtime_for(tmp_path, monkeypatch, vector_metadata()).inspect(source)
    first = package_dataset(info, tmp_path / "first.zip")
    for filename in info["components"]:
        os.utime(filename, (946684800, 946684800))
    second = package_dataset(info, tmp_path / "second.zip")
    assert first["sha256"] == second["sha256"]


def test_inspection_results_are_json_serializable(tmp_path, monkeypatch):
    source = make_shp(tmp_path)
    info = runtime_for(tmp_path, monkeypatch, vector_metadata()).inspect(source)
    assert json.loads(json.dumps(info))["kind"] == "shapefile"
