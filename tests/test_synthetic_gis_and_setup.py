"""Synthetic fixtures and guided GeoServer setup: no live network or site data."""
from __future__ import annotations

import importlib.util
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from geopublisher.desktop import MainWindow

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    form = MainWindow(work_dir=tmp_path / "work")
    yield form
    assert form._thread is None
    form.close()
    app.processEvents()


def _fixture_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "create_synthetic_gis_test_data.py"
    spec = importlib.util.spec_from_file_location("synthetic_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_synthetic_geometries_have_two_fictional_sheets_and_valid_shapes():
    data = _fixture_module().datasets()
    assert {name: len(features) for name, features in data.items()} == {
        "gp_demo_points": 8, "gp_demo_lines": 4, "gp_demo_areas": 4,
    }
    for name, features in data.items():
        assert {x["properties"]["SHEET_ID"] for x in features} == {
            "TEST_A01", "TEST_A02",
        }
        assert all(x["properties"]["GEO_TYPE"] == "MOCK" for x in features)
        assert all(x["geometry"]["type"] == {
            "gp_demo_points": "Point",
            "gp_demo_lines": "LineString",
            "gp_demo_areas": "Polygon",
        }[name] for x in features)
    for feature in data["gp_demo_areas"]:
        ring = feature["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1]


def test_synthetic_sld_is_valid_and_different_for_all_geometry_types(tmp_path):
    module = _fixture_module()
    for stem, geom in (("gp_demo_points", "points"),
                       ("gp_demo_lines", "lines"), ("gp_demo_areas", "polygons")):
        path = tmp_path / f"{stem}.sld"
        module._write_sld(path, stem, geom)
        root = ET.parse(path).getroot()
        assert root.tag.endswith("StyledLayerDescriptor")
        assert stem in path.read_text(encoding="utf-8")
    assert "PointSymbolizer" in (tmp_path / "gp_demo_points.sld").read_text()
    assert "LineSymbolizer" in (tmp_path / "gp_demo_lines.sld").read_text()
    assert "PolygonSymbolizer" in (tmp_path / "gp_demo_areas.sld").read_text()


def test_synthetic_raster_is_generated_without_network(tmp_path):
    path = tmp_path / "grid.ppm"
    _fixture_module()._raster_ppm(path)
    data = path.read_bytes()
    assert data.startswith(b"P6\n256 256\n255\n")
    assert len(data) > 256 * 256 * 3


def test_guided_environment_explains_geoserver_credentials_and_http(window):
    labels = [x.text() for x in window.pages.widget(1).findChildren(type(window.connection_status))]
    assert any("GeoServer 网页管理账号" in x for x in labels)
    assert any("不是 Linux" in x for x in labels)
    assert window.session_username.placeholderText().startswith("例如 admin")
    assert window.base_url.text() == ""
    window.base_url.setText("http://192.0.2.123:8080/geoserver")
    assert "未加密 HTTP" in window.http_security_note.text()
    window.allow_http.setChecked(True)
    assert "已经打开隔离内网例外" in window.http_security_note.text()
    window.base_url.setText("https://geoserver.invalid/geoserver")
    assert "HTTPS" in window.http_security_note.text()


def test_create_test_workspace_rejects_production_names_without_network(window, monkeypatch):
    window.base_url.setText("https://geoserver.invalid/geoserver")
    window.workspace.setText("geopub_acceptance_20261117")
    called = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: called.append(a[2]))
    monkeypatch.setattr(window, "_start_worker", lambda *_: (_ for _ in ()).throw(
        AssertionError("Must not connect or mutate with non-test workspace")))
    window._create_test_workspace()
    assert called and "测试" in called[0]


def test_create_test_workspace_requires_explicit_approval(window, monkeypatch):
    window.base_url.setText("https://geoserver.invalid/geoserver")
    window.workspace.setText("geopub_auto_smoke_20261117")
    window.session_username.setText("synthetic_admin")
    window.session_password.setText("synthetic_secret")
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
    monkeypatch.setattr(window, "_start_worker", lambda *_: (_ for _ in ()).throw(
        AssertionError("Unapproved request must not write anything")))
    window._create_test_workspace()
    assert window._thread is None


def test_explicitly_approved_test_workspace_is_created_once_by_rest_mock(window, monkeypatch):
    window.base_url.setText("https://geoserver.invalid/geoserver")
    window.workspace.setText("geopub_auto_smoke_20261117")
    window.session_username.setText("synthetic_admin")
    window.session_password.setText("synthetic_secret")
    events = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            events.append("connect")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_workspace(self, workspace):
            events.append(("read", workspace))
            return {"name": workspace} if "create" in events else None

        def create_workspace(self, workspace):
            events.append("create")
            assert workspace == "geopub_auto_smoke_20261117"

    monkeypatch.setattr("geopublisher.geoserver.GeoServerClient", FakeClient)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(window, "_start_worker", lambda operation, cb: cb(operation(lambda _: None)))
    window._create_test_workspace()
    assert events.count("create") == 1
    assert events.count(("read", "geopub_auto_smoke_20261117")) == 2
    assert window._connection_ready
    assert window.goto_tasks_button.isEnabled()
    assert "已创建并验证" in window.connection_status.text()
