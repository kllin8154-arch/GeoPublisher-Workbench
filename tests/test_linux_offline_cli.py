"""Linux offline CLI wheelhouse is explicit about external GDAL and no online target installs."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_linux_offline_installer_is_hash_gated_and_network_free():
    text = (ROOT / "scripts" / "install_linux_cli_offline.sh").read_text("utf-8")
    assert "sha256sum --check --status SHA256SUMS" in text
    assert "present=" in text and "listed=" in text
    assert "--no-index --only-binary=:all:" in text
    assert "PIP_NO_INDEX=1" in text
    assert "PIP_CONFIG_FILE=/dev/null" in text
    assert "PROJ_NETWORK=OFF" in text
    assert "gdalsrsinfo EPSG:4326" in text
    assert "GDAL >=3.7" in text
    assert "if [[ -e \"$destination\"" in text
    assert "curl " not in text and "wget " not in text
    assert "sudo " not in text
    assert "OFFLINE_LINUX_CLI_INSTALL=COMPLETE" in text


def test_linux_builder_labels_system_gdal_as_external_dependency():
    text = (ROOT / "scripts" / "build_linux_cli_offline.sh").read_text("utf-8")
    assert "python3 -m build --wheel" in text
    assert "python3 -m pip download --only-binary=:all:" in text
    assert "SYSTEM_GDAL_PREINSTALLED_REQUIRED=YES" in text
    assert "SHA256SUMS" in text
    assert "sha256sum" in text
    assert "tar -czf" in text
    assert "SHEET-BOUNDARY-P0C.md" in text
    assert "not a self-contained" in text.lower()
    assert "ssh " not in text
    assert "psql " not in text


def test_ci_smoke_installs_linux_wheelhouse_without_a_production_host():
    flow = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "linux-offline-cli-wheelhouse:" in flow
    assert "bash scripts/build_linux_cli_offline.sh" in flow
    assert 'bash "$unpacked/install.sh"' in flow
    assert "sheet-boundary-check --help" in flow
    assert "ubuntu-24.04" in flow


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                    reason="Requires local POSIX bash; WSL checked separately on Windows")
def test_linux_offline_shell_syntax():
    for name in ("build_linux_cli_offline.sh", "install_linux_cli_offline.sh"):
        script = ROOT / "scripts" / name
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True,
            text=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
