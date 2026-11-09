"""No-network tests for guarded LAN delivery and Linux archive staging."""
from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import shutil
import tarfile
from pathlib import Path

import pytest

from geopublisher.server_deploy import (
    LinuxTarget,
    ReleaseArchive,
    ServerDeployError,
    SSHSession,
    extract_staged,
    stage_archive,
)


def build_package(root: Path, *, name="synthetic_maps", bad_member=None) -> Path:
    path = root / (name + ".tar")
    raw = b"synthetic bytes 42" * 50
    digest = hashlib.sha256(raw).hexdigest()
    entries = {
        name + "/README.md": b"# Synthetic GIS release\n",
        name + "/data/SHA256SUMS.txt": (digest + "  overview.tif\n").encode(),
        name + "/data/overview.tif": raw,
    }
    with tarfile.open(path, "w") as tar:
        for filename, content in entries.items():
            meta = tarfile.TarInfo(filename)
            meta.size = len(content)
            tar.addfile(meta, io.BytesIO(content))
        if bad_member:
            item = tarfile.TarInfo(bad_member)
            item.size = 2
            tar.addfile(item, io.BytesIO(b"!x"))
    return path


class FakeSession:
    def __init__(self, root: Path):
        self.disk = root
        self.files = {}
        self.operations = []

    def local(self, remote: str) -> Path:
        assert remote.startswith("/zbcx/geopub-test/")
        return self.disk / remote.removeprefix("/zbcx/geopub-test/")

    def assert_root_safe(self):
        pass

    def exists(self, name):
        return self.local(name).exists()

    def kind(self, name):
        p = self.local(name)
        if p.is_symlink():
            return "symlink"
        if p.is_file():
            return "file"
        if p.is_dir():
            return "dir"
        return "missing"

    def upload_exclusive(self, source, dest, report=None):
        target = self.local(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
        self.operations.append(("upload", dest))
        if report:
            report(source.stat().st_size, source.stat().st_size)

    def unlink_owned_temp(self, name):
        self.local(name).unlink()
        self.operations.append(("unlink", name))

    def run(self, op, *args, timeout=90):
        self.operations.append((op, args))
        if op == "os":
            return "Linux"
        if op == "preflight":
            return "Linux"
        if op == "java":
            return "openjdk version 17.0.21"
        if op == "disk":
            return "/dev/local 9000000 200000 8800000 3% /zbcx"
        if op == "digest":
            return hashlib.sha256(self.local(args[0]).read_bytes()).hexdigest() + "  file"
        if op == "link":
            os.link(self.local(args[0]), self.local(args[1]))
            return ""
        if op == "create":
            self.local(args[0]).mkdir()
            return ""
        if op == "extract":
            source, to_root = args
            with tarfile.open(self.local(source)) as t:
                for member in t:
                    loc = self.local(posixpath.join(to_root, member.name))
                    if member.isdir():
                        loc.mkdir(parents=True, exist_ok=True)
                    else:
                        loc.parent.mkdir(parents=True, exist_ok=True)
                        loc.write_bytes(t.extractfile(member).read())
            return ""
        if op == "verify":
            data = self.local(args[0])
            for line in (data / "SHA256SUMS.txt").read_text().splitlines():
                digest, name = line.split("  ")
                assert hashlib.sha256((data / name).read_bytes()).hexdigest() == digest
            return "VERIFIED_FILES=1"
        raise AssertionError(f"Unexpected op: {op}")

    def close(self):
        pass


def fixture_target(tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("placeholder host public key\n", encoding="utf-8")
    return LinuxTarget(mode="ssh", host="192.168.77.22", username="tester", root="/zbcx/geopub-test",
                       known_hosts=str(known), auth="password")


def test_synthetic_package_is_safe_and_hashable(tmp_path):
    release = ReleaseArchive.inspect(build_package(tmp_path))
    assert release.name == "synthetic_maps"
    assert release.sha256 == hashlib.sha256(release.path.read_bytes()).hexdigest()
    assert len(release.content_names) == 3


def test_stage_and_explicit_unpack_preserve_archive_and_originals(tmp_path):
    target = fixture_target(tmp_path)
    release = ReleaseArchive.inspect(build_package(tmp_path))
    session = FakeSession(tmp_path / "synthetic-linux-root")
    sent = []
    staged = stage_archive(target, release, session, progress=lambda n, size: sent.append((n, size)))
    assert staged["status"] == "STAGED_VERIFIED_NOT_EXTRACTED"
    assert sent[-1] == (release.size, release.size)
    assert not session.exists("/zbcx/geopub-test/synthetic_maps")
    assert session.exists("/zbcx/geopub-test/synthetic_maps.tar")
    assert not any(session.disk.glob("*.partial"))
    unpack = extract_staged(target, release, session)
    assert unpack["status"] == "EXTRACTED_CHECKSUMS_OK_NOT_PUBLISHED"
    assert unpack["checked_lines"] == 1
    assert (session.disk / "synthetic_maps/data/overview.tif").read_bytes() == b"synthetic bytes 42" * 50
    assert release.path.is_file()
    assert all(op[0] not in {"geo", "sql"} for op in session.operations)


def test_staging_refuses_to_overwrite_same_name_archive_or_folder(tmp_path):
    target = fixture_target(tmp_path)
    release = ReleaseArchive.inspect(build_package(tmp_path))
    session = FakeSession(tmp_path / "ssh-root")
    stage_archive(target, release, session)
    before = (session.disk / "synthetic_maps.tar").read_bytes()
    with pytest.raises(ServerDeployError) as err:
        stage_archive(target, release, session)
    assert err.value.code == "RELEASE_ALREADY_EXISTS"
    assert (session.disk / "synthetic_maps.tar").read_bytes() == before


def test_archive_with_traversal_or_links_is_rejected_before_transfer(tmp_path):
    for bad in ("synthetic_maps/../other/file", "synthetic_maps/evil\\file", "/etc/passwd"):
        folder = tmp_path / hashlib.md5(bad.encode()).hexdigest()
        folder.mkdir()
        with pytest.raises(ServerDeployError) as err:
            ReleaseArchive.inspect(build_package(folder, bad_member=bad))
        assert err.value.code == "UNSAFE_ARCHIVE"


def test_public_ip_is_refused_before_ssh_connect(tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("ignored")
    with pytest.raises(ServerDeployError) as err:
        LinuxTarget(mode="ssh", root="/zbcx/local", host="8.8.8.8",
                    username="owner", auth="password", known_hosts=str(known))
    assert err.value.code == "PUBLIC_HOST_BLOCKED"


def test_no_password_persisted_and_no_unknown_profile_keys(tmp_path):
    target = fixture_target(tmp_path)
    assert "SECRET" not in json.dumps(target.safe_info())
    assert "known_hosts" not in target.safe_info()
    assert "key_file" not in target.safe_info()
    profile = tmp_path / "server.json"
    profile.write_text(json.dumps({**target.__dict__, "password": "SECRET"}), encoding="utf-8")
    with pytest.raises(ServerDeployError) as err:
        LinuxTarget.load(profile)
    assert err.value.code == "INVALID_PROFILE"


def test_unknown_host_is_rejected_not_auto_added(tmp_path):
    target = fixture_target(tmp_path)
    class FakeSSHClient:
        def __init__(self):
            self.closed = False
        def load_host_keys(self, path):
            pass
        def set_missing_host_key_policy(self, policy):
            assert policy.__class__.__name__ == "RejectPolicy"
        def get_host_keys(self):
            return self
        def lookup(self, host):
            return None
        def close(self):
            self.closed = True
        def connect(self, **kwargs):
            raise AssertionError("Must not try SSH with unknown host fingerprint")
    client = FakeSSHClient()
    with pytest.raises(ServerDeployError) as err:
        SSHSession(target, password="secret", client=client)
    assert err.value.code == "UNKNOWN_SSH_HOST"
    assert client.closed is True


def test_unauthorized_server_command_is_rejected(tmp_path):
    target = fixture_target(tmp_path)
    with pytest.raises(ServerDeployError):
        from geopublisher.server_deploy import LocalLinuxSession
        LocalLinuxSession(target)


def test_ssh_profile_only_accepts_explicit_host_and_authorized_root(tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("known")
    for root in ("/", "/tmp", "/root", "/etc", "/zbcx/../root", "/zbcx//gis"):
        with pytest.raises(ServerDeployError):
            LinuxTarget(mode="ssh", host="10.0.0.1", username="tester",
                        auth="password", known_hosts=str(known), root=root)



def test_preflight_disk_capacity_blocks_upload_before_remote_file(tmp_path):
    target = fixture_target(tmp_path)
    release = ReleaseArchive.inspect(build_package(tmp_path))
    class SmallDisk(FakeSession):
        def run(self, op, *args, timeout=90):
            if op == "disk":
                return "/dev/low 900 800 100 89% /zbcx/geopub-test"
            return super().run(op, *args, timeout=timeout)
    session = SmallDisk(tmp_path / "low-capacity")
    with pytest.raises(ServerDeployError) as err:
        stage_archive(target, release, session)
    assert err.value.code == "DISK_SPACE_TOO_LOW"
    assert not session.disk.exists()


def test_server_archive_symlink_is_never_extracted(tmp_path):
    target = fixture_target(tmp_path)
    release = ReleaseArchive.inspect(build_package(tmp_path))
    session = FakeSession(tmp_path / "test-server")
    session.disk.mkdir()
    original = session.disk / "original.tar"
    original.write_bytes(release.path.read_bytes())
    try:
        (session.disk / "synthetic_maps.tar").symlink_to(original)
    except OSError:
        pytest.skip("Windows account does not have symlink privileges")
    with pytest.raises(ServerDeployError) as err:
        extract_staged(target, release, session)
    assert err.value.code == "STAGE_NOT_READY"
    assert not (session.disk / "synthetic_maps").exists()


def test_raster_checksum_line_count_must_match_archive_manifest(tmp_path):
    target = fixture_target(tmp_path)
    release = ReleaseArchive.inspect(build_package(tmp_path))
    session = FakeSession(tmp_path / "wrong-checksum-count")
    stage_archive(target, release, session)
    original_run = session.run
    def bad_verify(op, *args, timeout=90):
        if op == "verify":
            return "VERIFIED_FILES=2"
        return original_run(op, *args, timeout=timeout)
    session.run = bad_verify
    with pytest.raises(ServerDeployError) as err:
        extract_staged(target, release, session)
    assert err.value.code == "RASTER_CHECKSUM_COUNT_MISMATCH"
    assert err.value.outcome_unknown
