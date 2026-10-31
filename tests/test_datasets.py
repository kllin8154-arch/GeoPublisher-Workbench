from __future__ import annotations

import json
import os
import sys
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


@pytest.mark.parametrize("reserved_credentials", [False, True])
def test_runtime_secrets_do_not_reach_real_child_process(tmp_path, monkeypatch, reserved_credentials):
    """Exercise the real _run/subprocess path, using Python as an env observer."""
    marker = "geopub-test-only-secret-sentinel"
    private_names = (
        "GEOPUB_USERNAME",
        "GEOPUB_PASSWORD",
        "gEoPuB_pAsSwOrD",
        "SITE_ACCOUNT",
        "SITE_ACCESS",
        "UNRECOGNIZED_CUSTOM_SETTING",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_CLIENT_SECRET",
        "AZURE_STORAGE_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "https_proxy",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "NETRC",
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "CONDA_PREFIX",
        "GDAL_HTTP_HEADER_FILE",
    )
    for name in private_names:
        monkeypatch.setenv(name, marker)
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(tmp_path))
    credential_names = ("SITE_ACCOUNT", "site_access")
    if reserved_credentials:
        private_names += ("TEMP", "LANG")
        credential_names += ("tEmP", "lAnG")
        monkeypatch.setenv("TEMP", marker)
        monkeypatch.setenv("LANG", marker)
    monkeypatch.setenv("PATH", marker)
    runtime = GdalRuntime(tmp_path, credential_env_names=credential_names)
    monkeypatch.setattr(runtime, "_executable", lambda name: sys.executable)
    # Return booleans only, never an environment dump or a credential value.
    script = (
        "import json, os, tempfile\n"
        f"blocked = {{name.casefold() for name in {private_names!r}}}\n"
        "with tempfile.TemporaryFile() as stream:\n"
        "    stream.write(b'local temporary I/O')\n"
        "    temporary_ok = stream.tell() > 0\n"
        "print(json.dumps({\n"
        "    'private_names_absent': not any(name.casefold() in blocked for name in os.environ),\n"
        f"    'sentinel_absent': not any({marker!r} in value for value in os.environ.values()),\n"
        f"    'controlled_path': os.environ.get('PATH') == {str(runtime.bin_dir)!r},\n"
        f"    'temporary_directory': os.path.normcase(tempfile.gettempdir()) == os.path.normcase({str(tmp_path)!r}),\n"
        "    'temporary_io': temporary_ok,\n"
        "    'offline': os.environ.get('PROJ_NETWORK') == 'OFF',\n"
        "}))\n"
    )
    before = dict(os.environ)
    observed = json.loads(runtime._run("ogrinfo", ["-I", "-c", script]))
    # Assign before asserting so a failure cannot render the parent's secrets.
    unchanged = dict(os.environ) == before
    assert unchanged
    assert observed == {
        "private_names_absent": True,
        "sentinel_absent": True,
        "controlled_path": True,
        "temporary_directory": True,
        "temporary_io": True,
        "offline": True,
    }


@pytest.mark.parametrize(
    "credential_name", ["TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "GDAL_CONFIG_FILE"]
)
def test_custom_credential_names_override_os_allowlist(tmp_path, monkeypatch, credential_name):
    marker = "geopub-test-only-reserved-name-sentinel"
    monkeypatch.setenv(credential_name, marker)
    runtime = GdalRuntime(tmp_path, credential_env_names=(credential_name.swapcase(),))
    environment = runtime._environment()
    assert marker not in environment.values()
    if credential_name not in {"PATH", "GDAL_CONFIG_FILE"}:
        assert credential_name not in environment
    # Parent configuration remains available to the HTTP client.
    assert os.environ[credential_name] == marker


def test_posix_environment_keeps_only_needed_locale_and_temp_settings(tmp_path):
    parent = {
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "LC_CTYPE": "C.UTF-8",
        "LC_CUSTOM_TOKEN": "test-secret",
        "lang": "test-secret",
        "SystemRoot": "test-secret",
        "WINDIR": "test-secret",
        "PATH": "test-secret",
    }
    environment = datasets._gdal_environment(tmp_path, parent, windows=False)
    for key in ("TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"):
        assert environment[key] == parent[key]
    assert "test-secret" not in environment.values()
    assert environment["PATH"] == str(tmp_path)


def test_simulated_windows_environment_normalizes_case_and_excludes_credentials(tmp_path):
    """Windows mapping semantics only; this does not execute Windows software."""
    parent = {
        "sYsTeMrOoT": r"C:\Windows",
        "wInDiR": r"C:\Windows",
        "tMp": str(tmp_path),
        "tEmP": "test-secret",
        "lAnG": "C.UTF-8",
        "gEoPuB_pAsSwOrD": "test-secret",
        "sItE_pAsS": "test-secret",
        "pAtH": "test-secret",
        "hOmE": "test-secret",
        "hTtPs_PrOxY": "test-secret",
    }
    environment = datasets._gdal_environment(
        tmp_path, parent, windows=True, credential_env_names=("TEMP", "SITE_PASS")
    )
    assert environment["SystemRoot"] == r"C:\Windows"
    assert environment["WINDIR"] == r"C:\Windows"
    assert environment["TMP"] == str(tmp_path)
    assert environment["LANG"] == "C.UTF-8"
    assert "TEMP" not in environment
    assert "test-secret" not in environment.values()
    assert environment["GDAL_CONFIG_FILE"] == "NUL"
    assert environment["PATH"] == str(tmp_path)


def test_simulated_windows_optional_names_can_be_missing_from_os_environ(tmp_path, monkeypatch):
    # Unlike a plain dict, os.environ rejects a None lookup key. Exercise its
    # actual mapping semantics while explicitly simulating only Windows casing.
    monkeypatch.setenv("sYsTeMrOoT", r"C:\Windows")
    for name in tuple(os.environ):
        if name.casefold() in {"lang", "lc_all", "lc_ctype", "tmpdir"}:
            monkeypatch.delenv(name)
    environment = datasets._gdal_environment(tmp_path, os.environ, windows=True)
    assert environment["SystemRoot"] == r"C:\Windows"
    assert not {"LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"}.intersection(environment)


@pytest.mark.parametrize(
    "parent, credential_names",
    [
        ({"WINDIR": r"C:\Windows"}, ()),
        ({"SystemRoot": ""}, ()),
        ({"sYsTeMrOoT": "test-secret", "WINDIR": r"C:\Windows"}, ("SYSTEMROOT",)),
    ],
)
def test_simulated_windows_missing_or_credential_systemroot_fails_safely(tmp_path, parent, credential_names):
    with pytest.raises(DatasetError) as error:
        datasets._gdal_environment(tmp_path, parent, windows=True, credential_env_names=credential_names)
    assert error.value.code == "GDAL_SYSTEM_ENV_REQUIRED"
    assert "test-secret" not in str(error.value)


@pytest.mark.parametrize("layout", ["unix", "windows_library_bin", "windows_prefix_bin"])
def test_runtime_data_and_dependency_paths_come_from_selected_installation(tmp_path, layout):
    prefix = tmp_path / "selected"
    windows = layout != "unix"
    bin_dir = prefix / "Library" / "bin" if layout == "windows_library_bin" else prefix / "bin"
    data_prefix = prefix / "Library" if windows else prefix
    gdal_data, proj_data = data_prefix / "share" / "gdal", data_prefix / "share" / "proj"
    for directory in (bin_dir, gdal_data, proj_data):
        directory.mkdir(parents=True, exist_ok=True)
    library_bin = prefix / "Library" / "bin"
    if windows:
        library_bin.mkdir(parents=True, exist_ok=True)
    parent = {
        "SystemRoot": r"C:\Windows",
        "PATH": "foreign-runtime",
        "GDAL_DATA": "foreign-data",
        "PROJ_DATA": "foreign-data",
        "PROJ_LIB": "foreign-data",
        "GDAL_DRIVER_PATH": "foreign-plugins",
        "GDAL_CONFIG_FILE": "foreign-config",
        "PROJ_NETWORK": "ON",
        "GDAL_PAM_ENABLED": "YES",
        "SHAPE_RESTORE_SHX": "YES",
    }
    environment = datasets._gdal_environment(bin_dir, parent, windows=windows)
    assert environment["GDAL_DATA"] == str(gdal_data)
    assert environment["PROJ_DATA"] == str(proj_data)
    assert "PROJ_LIB" not in environment
    assert "GDAL_DRIVER_PATH" not in environment
    expected_path = [str(bin_dir)]
    if layout == "windows_prefix_bin":
        expected_path.append(str(library_bin))
    assert environment["PATH"] == (";" if windows else ":").join(expected_path)
    assert environment["PROJ_NETWORK"] == "OFF"
    assert environment["GDAL_PAM_ENABLED"] == "NO"
    assert environment["SHAPE_RESTORE_SHX"] == "NO"
    assert environment["GDAL_CONFIG_FILE"] == ("NUL" if windows else os.devnull)


def test_selected_native_gdal_still_probes_and_resolves_crs_with_minimal_environment(monkeypatch):
    """Run the installed native pixi runtime; skip rather than simulate if absent."""
    prefix = Path(__file__).parents[1] / ".pixi" / "envs" / "default"
    bin_dir = prefix / "Library" / "bin" if os.name == "nt" else prefix / "bin"
    extension = ".exe" if os.name == "nt" else ""
    if not all((bin_dir / (name + extension)).is_file() for name in GdalRuntime.COMMANDS):
        pytest.skip("Native locked GDAL runtime is not installed on this platform")
    marker = "geopub-test-only-native-gdal-sentinel"
    for name in ("GEOPUB_PASSWORD", "SITE_ACCESS", "AWS_SESSION_TOKEN", "HTTPS_PROXY", "PATH"):
        monkeypatch.setenv(name, marker)
    runtime = GdalRuntime(bin_dir, expected_version="3.13.3", credential_env_names=("SITE_ACCESS",))
    original_run = datasets.subprocess.run
    native_calls = 0

    def checked_native_run(*args, **kwargs):
        nonlocal native_calls
        clean = not any(marker in value for value in kwargs["env"].values())
        assert clean
        native_calls += 1
        return original_run(*args, **kwargs)

    monkeypatch.setattr(datasets.subprocess, "run", checked_native_run)
    before = dict(os.environ)
    capability = runtime.probe()
    epsg = runtime._run("gdalsrsinfo", ["-o", "epsg", "EPSG:4326"])
    projected = runtime._run(
        "gdaltransform", ["-s_srs", "EPSG:4326", "-t_srs", "EPSG:3857"], input_text="10 20\n"
    )
    unchanged = dict(os.environ) == before
    assert unchanged
    assert native_calls == 8
    assert capability["version"] == "3.13.3"
    assert capability["drivers"]["ESRI Shapefile"] is True
    assert capability["drivers"]["GTiff"] is True
    assert "EPSG:4326" in epsg
    assert list(map(float, projected.split()[:2])) == pytest.approx([1113194.9079327357, 2273030.9269876895])
    environment = runtime._environment()
    assert Path(environment["GDAL_DATA"]).is_dir()
    assert (Path(environment["PROJ_DATA"]) / "proj.db").is_file()


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
