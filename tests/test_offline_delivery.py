"""Offline deliverables must be self-contained and never use online installers."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_windows_offline_shortcut_uses_only_builtin_local_capabilities():
    command = (ROOT / "scripts" / "Install-Offline-Shortcut.cmd").read_text(
        encoding="utf-8"
    ).casefold()
    assert "%~dp0geopublisher-workbench.exe" in command
    assert "wscript.shell" in command
    assert "desktopdirectory" in command
    assert "executionpolicy" not in command
    for disallowed in ("irm ", "curl ", "wget ", "pip ", "npm ", "https://", "http://"):
        assert disallowed not in command


def test_portable_build_bundles_shortcut_and_pins_runtime():
    script = (ROOT / "scripts" / "build_windows_portable.ps1").read_text(
        encoding="utf-8"
    ).casefold()
    assert "install-offline-shortcut.cmd" in script
    assert "offline_shortcut_sha256" in script
    assert "proj_network" in script
    assert "gdalinfo.exe" in script
    assert "proj.db" in script
    assert "copy-item -literalpath $offlineshortcut" in script
    assert "field-sheet-split-p0b.md" in script
    assert "offline_guide_sha256" in script
    assert "sheet-boundary-p0c.md" in script
    assert "offline_boundary_guide_sha256" in script
    assert "server-shp-persistence.md" in script
    assert "offline_server_guide_sha256" in script
    assert "move-item -literalpath $application -destination $final -erroraction stop" in script
    assert "& robocopy.exe $application $final /e /r:1 /w:1" in script
    assert "if ($copyexit -ge 8)" in script
    assert "portable finalization checksum mismatch" in script
    assert "portable move left a partial/unknown destination" in script
    assert "without disabling security software" in script
