"""Synthetic, private-LAN server-first SHP release and REST external-store tests."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import httpx
import pytest
from test_engine import StatefulGeoServer, make_plan
from test_server_deploy import FakeSession, fixture_target

from geopublisher.engine import run_plan
from geopublisher.geoserver import GeoServerClient, GeoServerError
from geopublisher.plans import digest_file, fingerprint
from geopublisher.server_deploy import ReleaseArchive, ServerDeployError, extract_staged, stage_archive
from geopublisher.server_vector import bundle_vector_plan, verify_vector_release


def synthetic_plan(tmp_path, *, count=2):
    plan = make_plan(tmp_path, count=count)
    for i, resource in enumerate(plan["resources"]):
        artifact = Path(resource["artifact"]["path"])
        native = f"synthetic_{i}"
        with zipfile.ZipFile(artifact, "w") as archive:
            for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                archive.writestr(native + suffix, ("synthetic" + suffix) .encode() * 10)
        resource["artifact"]["native_name"] = native
        resource["artifact"]["sha256"] = digest_file(artifact)
        resource["artifact"]["size"] = artifact.stat().st_size
    plan.pop("plan_hash")
    plan["plan_hash"] = fingerprint(plan)
    planpath = Path(plan["output_dir"]) / "plan.json"
    planpath.write_text(json.dumps(plan), encoding="utf-8")
    return planpath, plan


class VectorFakeSession(FakeSession):
    def run(self, op, *args, timeout=90):
        if op == "verify":
            folder = self.local(args[0])
            lines = (folder / "SHA256SUMS.txt").read_text().splitlines()
            for line in lines:
                digest, name = line.split("  ")
                import hashlib
                if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
                    raise ServerDeployError("REMOTE_COMMAND_FAILED", "Synthetic SHA256 mismatch")
            self.operations.append((op, args))
            return f"VERIFIED_FILES={len(lines)}"
        return super().run(op, *args, timeout=timeout)


def test_staged_server_zip_has_only_verified_shp_components(tmp_path):
    planpath, plan = synthetic_plan(tmp_path)
    bundle = bundle_vector_plan(planpath, tmp_path / "release")
    assert bundle["status"] == "LOCAL_VECTOR_RELEASE_READY"
    archive = ReleaseArchive.inspect(bundle["archive"])
    assert archive.manifest_count == 10
    assert bundle["resources"] == 2
    assert not bundle["target_server_uploaded"]
    assert all(not name.endswith(".exe") for name in archive.content_names)
    assert "README.md" in "/".join(archive.content_names)

    target = fixture_target(tmp_path)
    session = VectorFakeSession(tmp_path / "linux-synthetic")
    uploaded = stage_archive(target, archive, session)
    assert uploaded["status"] == "STAGED_VERIFIED_NOT_EXTRACTED"
    extracted = extract_staged(target, archive, session)
    assert extracted["status"] == "EXTRACTED_CHECKSUMS_OK_NOT_PUBLISHED"

    bindings = verify_vector_release(
        planpath, bundle["archive"], bundle["release_manifest"], target, session
    )
    assert set(bindings) == {r["resource_id"] for r in plan["resources"]}
    assert all(path.endswith(".shp") for path in bindings.values())
    assert all(path.startswith(target.root + "/") for path in bindings.values())
    assert all(session.kind(path) == "file" for path in bindings.values())
    assert all(kind not in {"geo", "sql"} for kind, _ in session.operations)


def test_remote_changed_component_cannot_enter_rest_publish(tmp_path):
    planpath, _ = synthetic_plan(tmp_path, count=1)
    bundle = bundle_vector_plan(planpath, tmp_path / "release")
    archive = ReleaseArchive.inspect(bundle["archive"])
    target = fixture_target(tmp_path)
    session = VectorFakeSession(tmp_path / "linux-synthetic")
    stage_archive(target, archive, session)
    extract_staged(target, archive, session)
    paths = verify_vector_release(
        planpath, bundle["archive"], bundle["release_manifest"], target, session
    )
    path = session.local(next(iter(paths.values())))
    path.write_bytes(b"tampered")
    with pytest.raises(ServerDeployError) as ex:
        verify_vector_release(planpath, bundle["archive"], bundle["release_manifest"], target, session)
    assert ex.value.code in {"REMOTE_COMMAND_FAILED", "VECTOR_REMOTE_COMPONENT_CHANGED"}


def test_modified_plan_or_release_manifest_blocks_server_file_bindings(tmp_path):
    planpath, plan = synthetic_plan(tmp_path, count=1)
    bundle = bundle_vector_plan(planpath, tmp_path / "release")
    archive = ReleaseArchive.inspect(bundle["archive"])
    target = fixture_target(tmp_path)
    session = VectorFakeSession(tmp_path / "linux-synthetic")
    stage_archive(target, archive, session)
    extract_staged(target, archive, session)

    marker = Path(bundle["release_manifest"])
    payload = json.loads(marker.read_text("utf-8"))
    payload["resources"][0]["target_name"] = "unauthorized"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ServerDeployError) as error:
        verify_vector_release(planpath, bundle["archive"], marker, target, session)
    assert error.value.code == "VECTOR_RESOURCE_MISMATCH"


def test_existing_or_unreviewed_plan_cannot_build_server_package(tmp_path):
    planpath, _ = synthetic_plan(tmp_path, count=1)
    dest = tmp_path / "release"
    bundle_vector_plan(planpath, dest)
    with pytest.raises(ServerDeployError) as err:
        bundle_vector_plan(planpath, dest)
    assert err.value.code == "VECTOR_BUNDLE_OUTPUT_UNSAFE"


@pytest.mark.parametrize("server_path", [
    r"D:\Desktop\synthetic.shp", "file:///zbcx/unreviewed.shp",
    "/tmp/synthetic.shp", "/zbcx/data/../outside.shp", "/zbcx/data/not-shp",
    "/zbcx/data/foo\nbar.shp",
])
def test_external_shp_refuses_nonserver_or_unsafe_paths(server_path):
    def handler(_request):
        pytest.fail("Unsafe server path must not trigger REST")
    with GeoServerClient(
        "https://example.invalid/geoserver", "admin", "synthetic",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(GeoServerError) as ex:
            client.register_external_shapefile("maps", "store1", server_path)
        assert ex.value.code == "INVALID_EXTERNAL_PATH"


def test_external_shp_registration_uses_metadata_only_and_never_uploads_bytes():
    methods = []
    def handler(request):
        methods.append(request)
        return httpx.Response(404 if request.method == "GET" else 201)

    with GeoServerClient(
        "https://example.invalid/geoserver", "admin", "synthetic",
        transport=httpx.MockTransport(handler),
    ) as client:
        response = client.register_external_shapefile(
            "maps", "store1", "/zbcx/geopublisher/releases/gpv_123/data/r0001/synthetic.shp",
        )
    assert response["operation"] == "register_external_shapefile"
    assert [r.method for r in methods] == ["GET", "PUT"]
    request = methods[-1]
    assert request.url.path.endswith("/datastores/store1/external.shp")
    assert request.url.params["configure"] == "none"
    assert request.content == (
        b"file:///zbcx/geopublisher/releases/gpv_123/data/r0001/synthetic.shp"
    )
    assert len(request.content) < 200
    assert "synthetic" not in str(request.headers.get("Authorization", "")).lower()


def test_external_server_store_uses_existing_journaled_engine(tmp_path):
    planpath, plan = synthetic_plan(tmp_path, count=1)
    # The engine's fake has no reliance on remote GIS files; register source
    # metadata only, while all later resource/style/WMS operations are reused.
    class ExternalClient(StatefulGeoServer):
        def __init__(self):
            super().__init__()
            self.paths = []

        def register_external_shapefile(self, workspace, store, path, *, encoding=None):
            self.paths.append(path)
            self._begin("UPLOAD", store)
            self.stores[(workspace, store, "shapefile")] = {
                "name": store, "enabled": True, "type": "shapefile",
                "workspace": {"name": workspace},
                "url": "file://" + path,
            }
            return self._end("UPLOAD")

    server = ExternalClient()
    remote = "/zbcx/geopublisher/releases/gpv_synthetic/data/r0001/synthetic_0.shp"
    first = run_plan(plan, client=server, staged_paths={"r0001": remote})
    assert first["status"] in {"SUCCEEDED", "REVIEW_REQUIRED"}
    assert server.paths == [remote]
    assert not server.uploaded_files
    after = run_plan(plan, client=server, staged_paths={"r0001": remote})
    assert after["status"] in {"SUCCEEDED", "REVIEW_REQUIRED"}
    assert server.paths == [remote]

    # Mixing REST upload and server-staged source in one journal is refused.
    switched = run_plan(plan, client=server)
    assert switched["status"] in {"MANUAL_ACTION_REQUIRED", "PARTIAL_SUCCESS", "FAILED"}
    assert server.paths == [remote]


def test_untrusted_extra_staged_path_rejected_before_any_network(tmp_path):
    _, plan = synthetic_plan(tmp_path, count=1)
    with pytest.raises(Exception) as err:
        run_plan(plan, client=object(), staged_paths={
            "r0001": "/zbcx/gp/r0001/a.shp", "r9999": "/zbcx/gp/r9999/b.shp",
        })
    assert getattr(err.value, "code", "") == "STAGED_PLAN_MISMATCH"
