"""Offline ArcGIS bridge tests: no ArcMap, QGIS or network required."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from geopublisher.arcgis_bridge import resolve_sld_directory, save_arcmap_helper
from geopublisher.config import ConfigError


def test_sld_folder_exact_name_pairing_not_fuzzy(tmp_path):
    folder = tmp_path / "styles"
    folder.mkdir()
    shp = tmp_path / "断层.shp"
    tif = tmp_path / "surface.tiff"
    (folder / "断层.sld").write_text("<StyledLayerDescriptor/>", encoding="utf-8")
    (folder / "surface.sld").write_text("<StyledLayerDescriptor/>", encoding="utf-8")
    selected = resolve_sld_directory([shp, tif], folder)
    assert selected[shp.resolve()] == folder / "断层.sld"
    assert selected[tif.resolve()] == folder / "surface.sld"
    with pytest.raises(ConfigError) as error:
        resolve_sld_directory([tmp_path / "fault.shp"], folder)
    assert error.value.code == "SLD_NOT_FOUND"


def test_sld_folder_collision_symlink_and_large_file_rejected(tmp_path):
    folder = tmp_path / "styles"
    folder.mkdir()
    (folder / "fault.sld").write_bytes(b"x")
    if os.name != "nt":
        (folder / "FAULT.sld").write_bytes(b"y")
        with pytest.raises(ConfigError) as error:
            resolve_sld_directory([tmp_path / "fault.shp"], folder)
        assert error.value.code == "SLD_AMBIGUOUS"
        (folder / "FAULT.sld").unlink()
    (folder / "fault.sld").write_bytes(b"")
    with pytest.raises(ConfigError) as error:
        resolve_sld_directory([tmp_path / "fault.shp"], folder)
    assert error.value.code == "SLD_FILE_SIZE"
    (folder / "fault.sld").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    with pytest.raises(ConfigError) as error:
        resolve_sld_directory([tmp_path / "fault.shp"], folder)
    assert error.value.code == "SLD_FILE_SIZE"


def test_arcmap_helper_export_writes_new_file_with_no_overwrite(tmp_path):
    target = tmp_path / "arcmap_mxd_to_shp.py"
    assert save_arcmap_helper(target) == target
    source = target.read_text(encoding="utf-8")
    assert "CopyFeatures_management" in source
    assert "SaveToLayerFile_management" in source
    assert "MapDocument" in source
    assert "import arcpy" in source
    with pytest.raises(ConfigError) as error:
        save_arcmap_helper(target)
    assert error.value.code == "ARCMAP_HELPER_EXISTS"
    assert target.read_text(encoding="utf-8") == source


def _fake_arcpy(shp_root):
    class Layer:
        isFeatureLayer = True
        isGroupLayer = False
        isBroken = False
        definitionQuery = ""

        def __init__(self, name, source):
            self.name = name
            self.dataSource = source

        def supports(self, field):
            return field == "DATASOURCE"

    frame = SimpleNamespace(name="地图1")
    layers = [Layer("断层", str(shp_root)), Layer("断层", str(shp_root))]
    env = SimpleNamespace(overwriteOutput=True)

    class Mapping:
        @staticmethod
        def MapDocument(path):
            return SimpleNamespace(path=path)

        @staticmethod
        def ListDataFrames(_doc):
            return [frame]

        @staticmethod
        def ListLayers(_doc, _wildcard, _frame):
            return layers

    class Fake:
        mapping = Mapping()
        env = env if False else None

        @staticmethod
        def Exists(path):
            return Path(path).exists()

        @staticmethod
        def CopyFeatures_management(_layer, path):
            Path(path).write_text("fake vector", encoding="utf-8")
            Path(path).with_suffix(".shx").write_bytes(b"index")
            Path(path).with_suffix(".dbf").write_bytes(b"attrs")
            Path(path).with_suffix(".prj").write_bytes(b"crs")
            Path(path).with_suffix(".cpg").write_text("UTF-8")

        @staticmethod
        def SaveToLayerFile_management(_layer, path, _mode):
            Path(path).write_bytes(b"fake layer")

    Fake.env = env
    return Fake(), layers


def test_arcmap_helper_exports_linked_data_and_lyr_with_fake_arcpy(tmp_path):
    input_mxd = tmp_path / "demo.mxd"
    input_mxd.write_bytes(b"synthetic map")
    source = tmp_path / "original.shp"
    source.write_bytes(b"source")
    helper_path = save_arcmap_helper(tmp_path / "helper.py")
    spec = importlib.util.spec_from_file_location("arcmap_helper_for_test", helper_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    fake, layers = _fake_arcpy(source)
    result = module.export_mxd(str(input_mxd), str(tmp_path / "exports"), arc=fake)
    assert [row["status"] for row in result["items"]] == ["EXPORTED", "EXPORTED"]
    assert {row["shp"] for row in result["items"]} == {"断层.shp", "断层_2.shp"}
    assert (tmp_path / "exports" / "断层.lyr").is_file()
    assert json.loads((tmp_path / "exports" / "mxd-export-manifest.json").read_text(encoding="utf-8"))["uploaded"] is False
    assert source.read_bytes() == b"source"
    assert fake.env.overwriteOutput is False
    layers[0].definitionQuery = "OBJECTID > 100"
    with pytest.raises(ValueError, match="输出目录"):
        module.export_mxd(str(input_mxd), str(tmp_path / "exports"), arc=fake)
    second = module.export_mxd(str(input_mxd), str(tmp_path / "exports-2"), arc=fake)
    assert second["items"][0]["status"] == "SKIPPED"
    assert second["items"][1]["status"] == "EXPORTED"


def test_arcmap_helper_rejects_missing_mxd_or_remote_sources(tmp_path):
    script = save_arcmap_helper(tmp_path / "helper.py")
    spec = importlib.util.spec_from_file_location("arcmap_helper_path_validation", script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    fake, layers = _fake_arcpy(tmp_path / "original.shp")
    with pytest.raises(ValueError, match="MXD"):
        helper.export_mxd(str(tmp_path / "missing.mxd"), str(tmp_path / "export"), arc=fake)
    layers[0].dataSource = r"\\server\share\sensitive.gdb\layer"
    ok, _ = helper.eligible(layers[0], fake)
    assert not ok
    assert helper.safe_filename("A:B / C") == "A_B_C"


def test_synthetic_mxd_generator_helper_does_not_fake_mxd_without_arcpy(tmp_path):
    demo = save_arcmap_helper(tmp_path / "arcmap_demo.py", demo=True)
    spec = importlib.util.spec_from_file_location("arcmap_demo_helper", demo)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="模板"):
        module.build_synthetic_mxd(str(tmp_path / "missing.mxd"), str(tmp_path / "new"))

    template = tmp_path / "blank.mxd"
    template.write_bytes(b"synthetic test-only template placeholder")
    used = []
    inserted = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def insertRow(self, item):
            inserted.append(item)

    class Document:
        def saveACopy(self, destination):
            Path(destination).write_bytes(b"FAKE-MXD-ONLY-FROM-MOCK")

    class Mapping:
        @staticmethod
        def MapDocument(_path):
            return Document()

        @staticmethod
        def ListDataFrames(_project):
            return [object()]

        @staticmethod
        def Layer(path):
            return path

        @staticmethod
        def AddLayer(_frame, path, order):
            assert order == "TOP"
            used.append(path)

    class FakeArc:
        mapping = Mapping()
        env = SimpleNamespace(overwriteOutput=True, outputCoordinateSystem=None)
        da = SimpleNamespace(InsertCursor=lambda *_args: Cursor())
        SpatialReference = staticmethod(lambda code: "EPSG:" + str(code))
        Point = staticmethod(lambda x, y: (x, y))
        PointGeometry = staticmethod(lambda value, _crs: value)
        Polyline = staticmethod(lambda value, _crs: value)
        Polygon = staticmethod(lambda value, _crs: value)
        Array = staticmethod(list)

        @staticmethod
        def CreateFeatureclass_management(parent, name, _geom, **kwargs):
            assert kwargs["spatial_reference"] == "EPSG:4326"
            Path(parent, name).write_bytes(b"SYNTHETIC-SHP")

        @staticmethod
        def AddField_management(_path, name, kind, **kwargs):
            assert name == "LABEL" and kind == "TEXT" and kwargs["field_length"] == 32

    result = module.build_synthetic_mxd(str(template), str(tmp_path / "actual"), arc=FakeArc())
    assert result["synthetic"] is True
    assert len(result["layers"]) == len(used) == len(inserted) == 3
    assert (tmp_path / "actual" / "synthetic_map.mxd").exists()
    assert template.read_bytes() == b"synthetic test-only template placeholder"
    # This test verifies the ArcPy call sequence, not binary MXD validity.
    assert (tmp_path / "actual" / "synthetic_map.mxd").read_bytes() == b"FAKE-MXD-ONLY-FROM-MOCK"



def test_arcmap_python27_chinese_stdout_survives_redirect(tmp_path, monkeypatch):
    """ArcMap Python 2 stdout has no encoding under PowerShell redirection."""
    script = save_arcmap_helper(tmp_path / "arcmap_real.py")
    spec = importlib.util.spec_from_file_location("arcmap_py2_console", script)
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    output: list[bytes] = []
    fake_stdout = SimpleNamespace(encoding=None, write=output.append)
    fake_sys = SimpleNamespace(version_info=(2, 7, 16), stdout=fake_stdout)
    monkeypatch.setattr(helper, "sys", fake_sys)
    monkeypatch.setattr(helper.locale, "getpreferredencoding", lambda *_: "cp936")

    helper.print_status("完成：SHP 3 个，清单在新输出目录。")
    assert b"".join(output).decode("cp936").strip() == "完成：SHP 3 个，清单在新输出目录。"
