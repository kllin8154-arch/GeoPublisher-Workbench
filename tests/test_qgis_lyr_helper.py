"""Safe offline QGIS helper contract checks, no QGIS installation needed."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from geopublisher.arcgis_bridge import qgis_helper_bytes, save_qgis_helper
from geopublisher.config import ConfigError

HELPER_SOURCE = Path(__file__).parents[1] / "src" / "geopublisher" / "resources" / "qgis_lyr_to_sld.py"
spec = importlib.util.spec_from_file_location("qgis_helper_safety", HELPER_SOURCE)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def fake_pair(folder: Path, stem: str = "TEST_POINT") -> None:
    for suffix in (".lyr", ".shp", ".shx", ".dbf", ".prj", ".cpg"):
        (folder / (stem + suffix)).write_bytes(b"local-synthetic-only")


def test_qgis_helper_bundle_exports_exact_bytes_only_once(tmp_path):
    expected = qgis_helper_bytes()
    assert b"python-qgis.bat" in expected
    target = tmp_path / "qgis_lyr_to_sld.py"
    assert save_qgis_helper(target) == target
    assert target.read_bytes() == expected
    with pytest.raises(ConfigError, match="存在"):
        save_qgis_helper(target)
    with pytest.raises(ConfigError, match=".py"):
        save_qgis_helper(tmp_path / "not-a-script.exe")


def test_qgis_batch_pair_discovery_and_missing_input_fail_closed(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    fake_pair(root, "TEST_POINT")
    fake_pair(root, "TEST_LINE")
    pairs = helper.discover_pairs(root)
    assert [(shp.stem, lyr.stem) for shp, lyr in pairs] == [
        ("TEST_LINE", "TEST_LINE"),
        ("TEST_POINT", "TEST_POINT"),
    ]
    (root / "TEST_LINE.shx").unlink()
    with pytest.raises(helper.ConversionError, match="缺失文件"):
        helper.discover_pairs(root)


def test_qgis_batch_rejects_unmatched_lyr_without_touching_source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "UNMATCHED.lyr").write_bytes(b"fake")
    before = {f.name: f.read_bytes() for f in root.iterdir()}
    with pytest.raises(helper.ConversionError, match="同名 SHP"):
        helper.discover_pairs(root)
    assert {f.name: f.read_bytes() for f in root.iterdir()} == before


def test_qgis_batch_output_is_fresh_and_separate(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    fake_pair(root)
    target = tmp_path / "new-styles"
    assert helper._new_output(target, root.resolve()) == target
    assert not target.exists()
    with pytest.raises(helper.ConversionError, match="不得"):
        helper._new_output(root / "subfolder", root.resolve())
    with pytest.raises(helper.ConversionError, match="不得"):
        helper._new_output(tmp_path / "source_parent", (tmp_path / "source_parent" / "data").resolve())
    target.mkdir()
    with pytest.raises(helper.ConversionError, match="尚不存在"):
        helper._new_output(target, root.resolve())


def test_qgis_sld_local_geometry_and_external_resource_checks(tmp_path):
    sld = tmp_path / "map.sld"
    sld.write_text("<StyledLayerDescriptor><PointSymbolizer/></StyledLayerDescriptor>", encoding="utf-8")
    helper.validate_local_sld(sld, 0)
    with pytest.raises(helper.ConversionError, match="不匹配"):
        helper.validate_local_sld(sld, 1)
    sld.write_text(
        '<StyledLayerDescriptor><PointSymbolizer/><ExternalGraphic href="https://example.org/test.svg"/></StyledLayerDescriptor>',
        encoding="utf-8",
    )
    with pytest.raises(helper.ConversionError, match="外部"):
        helper.validate_local_sld(sld, 0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [((True, "ok"), True), (("ok", True), True), (("error", False), False), (True, True), (None, False)],
)
def test_qgis_style_save_response_versions(value, expected):
    assert helper._style_save_ok(value) is expected


def test_qgis_helper_no_network_or_publishing_dependencies():
    # Import must remain safe even without PyQGIS, GDAL or a GeoServer.
    assert "GeoServer" not in helper.convert.__code__.co_names
    assert "subprocess" not in helper.convert.__code__.co_names
