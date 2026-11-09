"""Synthetic release -> isolated Linux-ready GeoServer REST publish logic."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest
from PIL import Image

from geopublisher.geoserver import GeoServerClient, GeoServerError, MapResponse
from geopublisher.server_deploy import LinuxTarget, ReleaseArchive, ServerDeployError
from geopublisher.server_publish import RasterPublishPlan, deploy_rasters


def package(tmp_path: Path):
    name = "synthetic_release"
    path = tmp_path / (name + ".tar")
    raw = b"synthetic tif" * 100
    sha = hashlib.sha256(raw).hexdigest()
    files = {
        name + "/README.md": b"synthetic only\n",
        name + "/data/SHA256SUMS.txt": (sha + "  imagery/synthetic.tif\n").encode(),
        name + "/data/imagery/synthetic.tif": raw,
    }
    with tarfile.open(path, "w") as out:
        for n, bytes_ in files.items():
            t = tarfile.TarInfo(n)
            t.size = len(bytes_)
            out.addfile(t, io.BytesIO(bytes_))
    return ReleaseArchive.inspect(path)


def plan_file(tmp_path: Path, archive: ReleaseArchive, *, kind="geotiff"):
    path = tmp_path / "publish.json"
    profile = {
        "schema_version": 1, "release": archive.name,
        "workspace": "demo", "geoserver_version": "2.24.4",
        "layers": [{
            "store": "synthetic", "layer": "synthetic", "kind": kind,
            "source": "data/imagery/synthetic.tif",
            "bbox": [100, 20, 101, 21], "granules": None,
        }],
    }
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path


class FakeSession:
    def __init__(self, package):
        self.package = package
        self.digests = 0
        self.operations = []
        self.target = LinuxTarget(mode="local", root="/srv/geopublisher/test")

    def assert_root_safe(self):
        pass

    def run(self, op, *args, timeout=90):
        self.operations.append(op)
        if op in ("os", "preflight"):
            return "Linux"
        if op == "java":
            return "openjdk version 17"
        if op == "disk":
            return "200gb free"
        if op == "digest":
            self.digests += 1
            return self.package.sha256 + "  release"
        if op == "verify":
            return "VERIFIED_FILES=1"
        raise AssertionError(f"Unexpected write operation: {op}")

    def exists(self, path):
        return True

    def kind(self, path):
        if path.endswith((".tar", ".tif")):
            return "file"
        return "dir"


class FakeForward:
    geoserver_url = "http://127.0.0.1:8080/geoserver"
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        pass


class FakeGeoServer:
    def __init__(self, *, version="2.24.4", failed=False):
        self.version = version
        self.failed = failed
        self.resources = {}
        self.mutations = []
        image = Image.new("RGB", (512, 512))
        image.putdata([((x * 3) % 256, (y * 3) % 256, (x+y) % 256)
                       for y in range(512) for x in range(512)])
        stream = io.BytesIO()
        image.save(stream, "PNG")
        self.png = stream.getvalue()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def probe(self):
        return {"geoserver_version": self.version}

    def get_workspace(self, workspace):
        return {"name": workspace}

    def get_store(self, workspace, name, kind):
        return self.resources.get(name)

    def get_layer(self, workspace, name):
        return self.resources.get(name)

    def register_external_raster(self, workspace, store, layer, kind, path):
        self.mutations.append((workspace, store, layer, kind, path))
        self.resources[store] = {"name": store}
        if self.failed:
            raise TimeoutError("synthetic lost write response")
        return {"status_code": 201}

    def get_map(self, workspace, layer, **kwargs):
        return MapResponse(200, {"content-type": "image/png"}, self.png)


def test_raster_publish_plan_requires_explicit_archive_and_metadata(tmp_path):
    archive = package(tmp_path)
    profile = RasterPublishPlan.load(plan_file(tmp_path, archive), archive)
    assert profile.layers[0].source == "data/imagery/synthetic.tif"
    config = json.loads((tmp_path / "publish.json").read_text())
    config["layers"][0]["source"] = "data/../secrets"
    (tmp_path / "publish.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ServerDeployError) as err:
        RasterPublishPlan.load(tmp_path / "publish.json", archive)
    assert err.value.code == "UNSAFE_RASTER_PATH"


def test_preflight_only_does_not_mutate_geoserver(tmp_path, monkeypatch):
    archive = package(tmp_path)
    profile = RasterPublishPlan.load(plan_file(tmp_path, archive), archive)
    session = FakeSession(archive)
    geoserver = FakeGeoServer()
    monkeypatch.setattr("geopublisher.server_publish.open_service", lambda *_args, **_kw: FakeForward())
    result = deploy_rasters(session.target, archive, profile, session, geoserver_username="user",
                            geoserver_password="ephemeral", output=tmp_path / "not_created",
                            publish=False, client_factory=lambda *_a, **_kw: geoserver)
    assert result["status"] == "GEOSERVER_READONLY_PREFLIGHT"
    assert result["server_modified"] is False
    assert geoserver.mutations == []
    assert not (tmp_path / "not_created").exists()


def test_synthetic_raster_publish_audit_before_rest_write(tmp_path, monkeypatch):
    archive = package(tmp_path)
    profile = RasterPublishPlan.load(plan_file(tmp_path, archive), archive)
    session = FakeSession(archive)
    geoserver = FakeGeoServer()
    monkeypatch.setattr("geopublisher.server_publish.open_service", lambda *_a, **_kw: FakeForward())
    outdir = tmp_path / "audit"
    result = deploy_rasters(session.target, archive, profile, session, geoserver_username="user",
                            geoserver_password="not-to-log", output=outdir, publish=True,
                            client_factory=lambda *_a, **_kw: geoserver)
    assert result["status"] == "PUBLISHED_NEEDS_HUMAN_WMS_REVIEW"
    assert result["database_accessed"] is False
    assert result["visual_reviewed"] is False
    assert len(geoserver.mutations) == 1
    assert (outdir / "synthetic_wms.png").read_bytes() == geoserver.png
    log = (outdir / "server_raster_audit.jsonl").read_text(encoding="utf-8")
    assert "REST_CREATE_INTENT" in log
    assert log.index("REST_CREATE_INTENT") < log.index("REST_CREATE_CONFIRMED")
    assert "not-to-log" not in log
    assert "DELETE" not in log


def test_unknown_rest_write_stops_and_records_ambiguous_result(tmp_path, monkeypatch):
    archive = package(tmp_path)
    profile = RasterPublishPlan.load(plan_file(tmp_path, archive), archive)
    session = FakeSession(archive)
    fake = FakeGeoServer(failed=True)
    monkeypatch.setattr("geopublisher.server_publish.open_service", lambda *_a, **_kw: FakeForward())
    outdir = tmp_path / "unknown"
    with pytest.raises(ServerDeployError) as err:
        deploy_rasters(session.target, archive, profile, session, geoserver_username="user",
                       geoserver_password="secret", output=outdir, publish=True,
                       client_factory=lambda *_a, **_kw: fake)
    assert err.value.outcome_unknown
    assert len(fake.mutations) == 1
    assert "REST_CREATE_UNKNOWN" in (outdir / "server_raster_audit.jsonl").read_text(encoding="utf-8")
    assert "secret" not in (outdir / "server_raster_audit.jsonl").read_text(encoding="utf-8")


def test_existing_store_or_wrong_version_blocks_before_any_write(tmp_path, monkeypatch):
    archive = package(tmp_path)
    profile = RasterPublishPlan.load(plan_file(tmp_path, archive), archive)
    session = FakeSession(archive)
    monkeypatch.setattr("geopublisher.server_publish.open_service", lambda *_a, **_kw: FakeForward())
    wrong = FakeGeoServer(version="3.0")
    with pytest.raises(ServerDeployError) as exc:
        deploy_rasters(session.target, archive, profile, session, geoserver_username="user",
                       geoserver_password="secret", output=tmp_path / "nope",
                       publish=True, client_factory=lambda *_a, **_kw: wrong)
    assert exc.value.code == "GEOSERVER_VERSION_MISMATCH"
    assert wrong.mutations == []
    existing = FakeGeoServer()
    existing.resources["synthetic"] = {}
    with pytest.raises(ServerDeployError) as exc:
        deploy_rasters(session.target, archive, profile, session, geoserver_username="user",
                       geoserver_password="secret", output=tmp_path / "nope2",
                       publish=True, client_factory=lambda *_a, **_kw: existing)
    assert exc.value.code == "RASTER_ALREADY_REGISTERED"
    assert existing.mutations == []


@pytest.mark.parametrize("kind,path", [
    ("geotiff", "/srv/geo/../secret.tif"),
    ("imagemosaic", "http://remote/data"),
    ("geotiff", "/srv\\mount\\file.tif"),
])
def test_external_rest_rejects_invalid_paths_before_any_put(kind, path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200)
    with GeoServerClient("https://example.invalid/geoserver", "admin", "secret",
                         transport=httpx.MockTransport(handler)) as gs:
        with pytest.raises(GeoServerError) as err:
            gs.register_external_raster("demo", "synthetic", "synthetic", kind, path)
        assert err.value.code == "INVALID_EXTERNAL_PATH"
    assert not calls


def test_external_rest_put_is_metadata_only_not_tiff_upload():
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(201)
    with GeoServerClient("https://example.invalid/geoserver", "admin", "secret",
                         transport=httpx.MockTransport(handler)) as gs:
        result = gs.register_external_raster(
            "demo", "synthetic", "synthetic", "imagemosaic", "/srv/geopublisher/test/data/mosaic"
        )
    assert result["method"] == "PUT"
    assert len(calls) == 3
    req = calls[-1]
    assert req.url.path.endswith("/workspaces/demo/coveragestores/synthetic/external.imagemosaic")
    assert req.content.decode() == "/srv/geopublisher/test/data/mosaic"
    assert req.url.params["configure"] == "all"
    assert req.url.params["coverageName"] == "synthetic"
    assert "secret" not in req.content.decode()
