"""Offline inventory of license evidence in a Windows portable build.

This is an evidence collection aid, NOT a legal certification. It performs no
network requests and never copies packages, source data or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from email.parser import Parser
from pathlib import Path, PurePosixPath


def _license_candidate(relative: str) -> bool:
    clean = relative.replace("\\", "/")
    path = PurePosixPath(clean)
    if path.is_absolute() or ".." in path.parts:
        return False
    name = path.name.lower()
    return (
        any(name.startswith(prefix) for prefix in ("license", "licence", "copying", "copyright", "notice"))
        or "licenses" in (component.lower() for component in path.parts)
    )


def _safe_regular_file(root: Path, relative: str) -> Path | None:
    path = PurePosixPath(relative.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return None
    target = root.joinpath(*path.parts)
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != root):
        return None
    if not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
        return None
    return target


def _evidence(path: Path, base: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": path.relative_to(base).as_posix(), "sha256": digest.hexdigest()}


def audit_conda(runtime: Path) -> list[dict]:
    metadata = runtime / "conda-meta"
    if not metadata.is_dir():
        raise ValueError("The portable bundle has no conda-meta package inventory.")
    result = []
    for record in sorted(metadata.glob("*.json")):
        info = json.loads(record.read_text(encoding="utf-8"))
        name, version = info.get("name"), info.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("Conda metadata is missing required package identifiers.")
        candidates = [
            item for item in info.get("files", [])
            if isinstance(item, str) and _license_candidate(item)
        ]
        found = []
        for relative in sorted(set(candidates)):
            file = _safe_regular_file(runtime, relative)
            if file:
                found.append(_evidence(file, runtime))
        result.append(
            {
                "name": name,
                "version": version,
                "declared_license": info.get("license") or "UNKNOWN",
                "candidate_files_listed": len(candidates),
                "included_license_evidence": found,
                "evidence_status": "FOUND" if found else "MISSING_OR_NOT_INDEXED",
            }
        )
    return result


def audit_python_metadata(site_packages: Path) -> list[dict]:
    if not site_packages.is_dir():
        raise ValueError("Site-packages directory not found.")
    results = []
    for directory in sorted(site_packages.glob("*.dist-info")):
        metadata = directory / "METADATA"
        if not metadata.is_file():
            continue
        info = Parser().parsestr(metadata.read_text(encoding="utf-8", errors="replace"), headersonly=True)
        license_files = []
        for path in sorted(directory.rglob("*")):
            if path.is_file() and _license_candidate(path.relative_to(directory).as_posix()):
                license_files.append(_evidence(path, site_packages))
        results.append(
            {
                "name": info.get("Name", directory.stem),
                "version": info.get("Version", "UNKNOWN"),
                "declared_license": info.get("License-Expression") or info.get("License") or "UNKNOWN",
                "local_metadata_evidence": license_files,
                "not_proof_of_inclusion_in_pyinstaller": True,
            }
        )
    return results


def audit(bundle: Path, site_packages: Path | None = None) -> dict:
    bundle = bundle.resolve()
    if not (bundle / "GeoPublisher-Workbench.exe").is_file():
        raise ValueError("The target is not a Windows portable GeoPublisher directory.")
    runtime = bundle / "runtime"
    packages = audit_conda(runtime)
    source_metadata = audit_python_metadata(site_packages) if site_packages else []
    no_text = sum(not package["included_license_evidence"] for package in packages)
    return {
        "schema_version": 1,
        "scope": "local-windows-portable-license-evidence",
        "distribution_status": "NOT_CLEARED",
        "legal_review_required": True,
        "reason": (
            "Metadata and file hashes are not a license clearance. Review each "
            "dependency and PySide6/Qt LGPL/GPL obligations before redistribution."
        ),
        "conda_count": len(packages),
        "conda_with_located_text": len(packages) - no_text,
        "conda_missing_or_unindexed_text": no_text,
        "conda": packages,
        "python_source_metadata_only": source_metadata,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline third-party license evidence inventory")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite an existing license report.")
    result = audit(args.bundle, args.site_packages)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"NOT_CLEARED: {result['conda_count']} conda packages; "
        f"{result['conda_missing_or_unindexed_text']} missing/unindexed license texts; "
        f"{len(result['python_source_metadata_only'])} Python metadata records"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
