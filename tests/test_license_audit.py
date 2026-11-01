"""Offline license inventory is evidence, never automatic distribution clearance."""

import json

from scripts.audit_portable_licenses import _license_candidate, audit, audit_conda, audit_python_metadata


def _bundle(tmp_path):
    bundle = tmp_path / "portable"
    bundle.mkdir()
    (bundle / "GeoPublisher-Workbench.exe").write_bytes(b"synthetic")
    (bundle / "runtime" / "conda-meta").mkdir(parents=True)
    return bundle


def test_audit_uses_bundle_relative_files_and_marks_unlocated_text(tmp_path):
    bundle = _bundle(tmp_path)
    runtime = bundle / "runtime"
    license_path = runtime / "Library" / "share" / "doc" / "proj" / "COPYING"
    license_path.parent.mkdir(parents=True)
    license_path.write_text("synthetic MIT text only", encoding="utf-8")
    records = [
        {"name": "proj", "version": "9.9.0", "license": "MIT",
         "files": ["Library/share/doc/proj/COPYING"]},
        {"name": "geos", "version": "3.14.1", "license": "LGPL-2.1-only", "files": []},
    ]
    for record in records:
        (runtime / "conda-meta" / f"{record['name']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    inventory = audit(bundle)
    assert inventory["distribution_status"] == "NOT_CLEARED"
    assert inventory["legal_review_required"] is True
    assert inventory["conda_count"] == 2
    assert inventory["conda_with_located_text"] == 1
    assert inventory["conda_missing_or_unindexed_text"] == 1
    assert inventory["conda"][0]["evidence_status"] == "MISSING_OR_NOT_INDEXED"
    proj = next(item for item in inventory["conda"] if item["name"] == "proj")
    assert proj["included_license_evidence"][0]["path"] == "Library/share/doc/proj/COPYING"
    assert len(proj["included_license_evidence"][0]["sha256"]) == 64


def test_traversal_and_absolute_paths_do_not_count_as_license_evidence(tmp_path):
    bundle = _bundle(tmp_path)
    runtime = bundle / "runtime"
    (runtime / "conda-meta" / "unsafe.json").write_text(
        json.dumps({
            "name": "unsafe", "version": "1", "license": "MIT",
            "files": ["../../LICENSE", "C:/LICENSE", "Library/../LICENSE"],
        }),
        encoding="utf-8",
    )
    assert _license_candidate("../../LICENSE") is False
    assert _license_candidate("Library/../LICENSE") is False
    assert audit_conda(runtime)[0]["included_license_evidence"] == []


def test_python_source_license_is_not_misrepresented_as_included(tmp_path):
    bundle = _bundle(tmp_path)
    packages = tmp_path / "site-packages"
    metadata = packages / "pyside6_essentials-6.12.0.dist-info"
    text_path = metadata / "licenses" / "LICENSE"
    text_path.parent.mkdir(parents=True)
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: PySide6-Essentials\nVersion: 6.12.0\nLicense-Expression: LGPL-3.0-only\n",
        encoding="utf-8",
    )
    text_path.write_text("synthetic LGPL text", encoding="utf-8")
    installed = audit_python_metadata(packages)
    assert installed[0]["not_proof_of_inclusion_in_pyinstaller"] is True
    assert len(installed[0]["local_metadata_evidence"]) == 1
    inventory = audit(bundle, packages)
    assert inventory["python_source_metadata_only"] == installed
    assert inventory["distribution_status"] == "NOT_CLEARED"
