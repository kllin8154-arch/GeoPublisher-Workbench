"""Restricted REST publication gate on disposable GeoServer and mathematical TIFFs only.

This script deliberately sends REST writes; it refuses non-loopback addresses,
unknown source hashes, reused workspaces and non-disposable server directories.
Never run against a production GeoServer.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image

from geopublisher.config import Environment
from geopublisher.engine import rollback_plan, run_plan
from geopublisher.geoserver import GeoServerClient
from geopublisher.journal import Journal
from geopublisher.plans import build_plan, digest_file, fingerprint, save_json
from geopublisher.verification import verify_image

_FIXTURES = {
    "nodata-gray.tif": "40de4b4f6559e6f23ce0375732e32216e8e2ffcbbd4eb87883e1c3042236104e",
    "alpha-rgba.tif": "68d29780ca2f83e975069baf6d499c4f372a12968a3e0a12d0008a223e9019ca",
}
_ALPHA_COUNTS = {
    "nodata-gray.tif": {"0": 196608, "255": 65536},
    "alpha-rgba.tif": {"0": 196608, "96": 15360, "255": 50176},
}


class AuditedClient(GeoServerClient):
    """Counts writes without storing any authorization, bodies, URLs or secrets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.writes: list[str] = []

    def _request(self, method, path, **kwargs):
        if method not in {"GET", "HEAD"}:
            self.writes.append(kwargs.get("operation", "UNKNOWN_WRITE"))
        return super()._request(method, path, **kwargs)


class LoseTiffUploadResponse(httpx.BaseTransport):
    """Let a synthetic raster PUT reach the test server, then lose its response."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.inner = transport if transport is not None else httpx.HTTPTransport(retries=0)
        self.dropped = False
        self.write_count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method in {"POST", "PUT", "DELETE"}:
            self.write_count += 1
        response = self.inner.handle_request(request)
        if not self.dropped and request.method == "PUT" and request.url.path.endswith("/file.geotiff"):
            response.read()
            response.close()
            self.dropped = True
            raise httpx.ReadTimeout("Synthetic response loss after TIFF upload", request=request)
        return response

    def close(self) -> None:
        self.inner.close()


def validate_lab(catalog: Path, sources: Path, base_url: str, output: Path) -> None:
    root = catalog.resolve(strict=True)
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 18883
        or parsed.path.rstrip("/") != "/geoserver"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or root.name != "geoserver-disposable-rest-20261101"
        or root.parent.name != "GeoPublisher-Workbench-validation"
        or not (root / "DISPOSABLE-REST-ONLY.txt").is_file()
        or not (root / "server.pid").is_file()
    ):
        raise ValueError("The REST integration gate only supports its disposable loopback server.")
    if output.exists() or output.is_relative_to(root) or output.is_relative_to(sources):
        raise ValueError("The result directory must be new and separate from its source/server.")
    source_list = list(sources.iterdir())
    if len(source_list) != 2 or {path.name for path in source_list} != set(_FIXTURES):
        raise ValueError("Exactly two canonical mathematical TIFF files are required.")
    for item in source_list:
        if item.is_symlink() or not item.is_file() or digest_file(item) != _FIXTURES[item.name]:
            raise ValueError("A synthetic input is not the expected deterministic TIFF.")
    pid = int((root / "server.pid").read_text(encoding="utf-8").strip())
    if pid <= 0:
        raise ValueError("Disposable server PID invalid.")
    # Verify the process actually owning the test port. Merely having a PID
    # file beside a folder must never authorize REST mutations on a different
    # service that happens to occupy 127.0.0.1:18883.
    if os.name != "nt":
        raise ValueError("This disposable REST lab gate currently requires Windows.")
    guard = (
        "$ErrorActionPreference='Stop';"
        "$l=@(Get-NetTCPConnection -LocalPort 18883 -State Listen -ErrorAction SilentlyContinue);"
        f"if($l.Count -ne 1 -or $l[0].LocalAddress -ne '127.0.0.1' -or $l[0].OwningProcess -ne {pid})"
        "{exit 3};"
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}';"
        "if(-not $p -or $p.Name -notmatch '^java(w)?[.]exe$' -or "
        "$p.CommandLine -notlike '*geoserver-disposable-rest-20261101*'){exit 4}"
    )
    try:
        checked = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", guard],
            capture_output=True, timeout=15, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("The disposable REST server identity could not be confirmed.") from None
    if checked.returncode:
        raise ValueError("The listening server does not match the disposable GeoServer PID.")


def _map_alpha(client: GeoServerClient, workspace: str, layer: str) -> dict:
    response = client.get_map(
        workspace, layer, bbox=[110, 29, 111, 30],
        crs="EPSG:4326", version="1.1.1", width=512, height=512,
    )
    evidence = verify_image(response.content, response.headers.get("content-type", ""))
    from io import BytesIO

    with Image.open(BytesIO(response.content)) as image:
        rgba = image.convert("RGBA")
        histogram = {
            str(index): count for index, count in enumerate(rgba.getchannel("A").histogram())
            if count
        }
        pixels = {
            "outside": list(rgba.getpixel((32, 32))),
            "edge": list(rgba.getpixel((129, 129))),
            "inside": list(rgba.getpixel((256, 256))),
        }
    return {
        "status": evidence["status"],
        "sha256": evidence["sha256"],
        "visible_pixels": evidence["visible_pixels"],
        "alpha_histogram": histogram,
        "pixels": pixels,
    }


def run_rest_gate(
    catalog: Path, sources: Path, output: Path, *, credentials: tuple[str, str], workspace: str
) -> dict:
    output = output.resolve()
    base_url = "http://127.0.0.1:18883/geoserver"
    validate_lab(catalog, sources, base_url, output)
    if not workspace.startswith("gp_mask_lab_r") or not workspace[-2:].isdigit() or len(workspace) != 15:
        raise ValueError("Disposable workspaces must be named gp_mask_lab_rNN.")
    environment = Environment(
        "isolated_raster_rest_20261101", base_url, workspace,
        str(Path(__file__).resolve().parents[1] / ".pixi/envs/default/Library/bin"),
        gdal_version="3.13.3", timeout=60,
    )
    output.mkdir(parents=True, exist_ok=False)
    record = {
        "status": "STARTED",
        "environment": "disposable loopback only",
        "data_origin": "math synthetic TIFF",
        "workspace": environment.workspace,
        "first": None,
        "second": None,
        "masks": {},
        "cleanup": None,
    }
    with AuditedClient(base_url, *credentials, timeout=environment.timeout) as client:
        info = client.probe()
        record["geoserver_version"] = info.get("geoserver_version")
        if client.get_workspace(environment.workspace) is not None:
            raise RuntimeError("Disposable test workspace already exists; refusing to reuse.")
        unrelated = client.get_layer("topp", "states")
        if unrelated is None:
            raise RuntimeError("The expected unrelated demo layer is missing.")
        unrelated_hash = fingerprint(unrelated)
        client.create_workspace(environment.workspace)
        record["workspace_created_for_test"] = True
        plan = build_plan(
            environment, [sources / name for name in _FIXTURES], output / "publish",
            raster_mode="independent", strategy="new-version", enable_wfs=False,
        )
        if not plan["ready"] or len(plan["resources"]) != 2:
            raise RuntimeError("Synthetic plan did not pass local preflight.")
        record["plan_hash"] = plan["plan_hash"]
        first = run_plan(plan, client=client)
        record["first"] = {
            "status": first["status"],
            "statuses": {r["target_name"]: r["status"] for r in first["resources"]},
            "new_writes": len(client.writes) - 1,
        }
        if first["status"] != "SUCCEEDED":
            record["status"] = "MANUAL_ACTION_REQUIRED"
        else:
            for resource in plan["resources"]:
                source_name = Path(resource["inspection"]["source"]).name
                record["masks"][source_name] = _map_alpha(
                    client, environment.workspace, resource["target_name"]
                )
            prior = len(client.writes)
            second = run_plan(plan, client=client)
            record["second"] = {
                "status": second["status"],
                "additional_writes": len(client.writes) - prior,
            }
            with Journal(output / "geopublisher.sqlite") as journal:
                record["write_operation_count"] = len(journal.operations(plan["task_id"]))
            record["semantic_mask_matches"] = all(
                result["alpha_histogram"] == _ALPHA_COUNTS[name]
                for name, result in record["masks"].items()
            )
            if second["status"] == "SUCCEEDED" and record["second"]["additional_writes"] == 0:
                before = len(client.writes)
                cleanup = rollback_plan(plan, client=client)
                record["cleanup"] = {
                    "status": cleanup["status"],
                    "removed_objects": len(cleanup["removed"]),
                    "delete_writes": len(client.writes) - before,
                    "server_data_retained": cleanup["server_data_retained"],
                }
                record["unrelated_layer_unchanged"] = (
                    fingerprint(client.get_layer("topp", "states")) == unrelated_hash
                )
                record["layer_residue"] = [
                    resource["target_name"] for resource in plan["resources"]
                    if client.get_layer(environment.workspace, resource["target_name"]) is not None
                ]
                record["status"] = (
                    "PASSED" if record["semantic_mask_matches"]
                    and record["cleanup"]["status"] == "ROLLED_BACK"
                    and record["cleanup"]["removed_objects"] == 8
                    and not record["layer_residue"]
                    and record["unrelated_layer_unchanged"]
                    else "REVIEW_REQUIRED"
                )
            else:
                record["status"] = "MANUAL_ACTION_REQUIRED"
        save_json(output / "rest-integration-result.json", record)
    return record


def run_unknown_upload_gate(
    catalog: Path, sources: Path, output: Path, *,
    credentials: tuple[str, str], workspace: str
) -> dict:
    """Record failure evidence; intentionally do NOT retry or delete unknown server resources."""
    output = output.resolve()
    base_url = "http://127.0.0.1:18883/geoserver"
    validate_lab(catalog, sources, base_url, output)
    if not workspace.startswith("gp_mask_lab_r") or not workspace[-2:].isdigit() or len(workspace) != 15:
        raise ValueError("Disposable workspace name invalid.")
    env = Environment(
        "isolated_raster_lost_response_20261101", base_url, workspace,
        str(Path(__file__).resolve().parents[1] / ".pixi/envs/default/Library/bin"),
        gdal_version="3.13.3", timeout=60,
    )
    output.mkdir(parents=True, exist_ok=False)
    with AuditedClient(base_url, *credentials, timeout=60) as setup:
        setup.probe()
        if setup.get_workspace(workspace) is not None:
            raise RuntimeError("Disposable workspace already exists; do not reuse.")
        initial_unrelated = setup.get_layer("topp", "states")
        if initial_unrelated is None:
            raise RuntimeError("Unrelated test fixture layer not found.")
        unrelated_fingerprint = fingerprint(initial_unrelated)
        setup.create_workspace(workspace)
    plan = build_plan(
        env, [sources / "nodata-gray.tif"], output / "publish",
        strategy="new-version", enable_wfs=False,
    )
    if not plan.get("ready") or len(plan["resources"]) != 1:
        raise RuntimeError("Expected one ready synthetic TIFF resource.")
    transport = LoseTiffUploadResponse()
    with AuditedClient(base_url, *credentials, timeout=60, transport=transport) as client:
        first = run_plan(plan, client=client)
        writes_after_first = transport.write_count
        second = run_plan(plan, client=client)
        writes_after_second = transport.write_count
        store = client.get_store(workspace, plan["resources"][0]["store_name"], "geotiff")
        unrelated_unchanged = (
            fingerprint(client.get_layer("topp", "states")) == unrelated_fingerprint
        )
    with Journal(output / "geopublisher.sqlite") as journal:
        operations = journal.operations(plan["task_id"])
        cleanup_events = journal.events(plan["task_id"])
    result = {
        "scenario": "lost-response-after-successful-synthetic-tiff-put",
        "status": "PASSED" if (
            transport.dropped
            and first["status"] == second["status"] == "MANUAL_ACTION_REQUIRED"
            and writes_after_first == 1
            and writes_after_second == writes_after_first
            and store is not None
            and unrelated_unchanged
            and len(operations) == 1
            and operations[0]["action"] == "UPLOAD"
            and operations[0]["outcome"] == "UNKNOWN"
        ) else "FAILED",
        "first_task_status": first["status"],
        "second_task_status": second["status"],
        "upload_response_lost": transport.dropped,
        "writes_first": writes_after_first,
        "writes_repeat": writes_after_second - writes_after_first,
        "store_exists_for_manual_review": store is not None,
        "unrelated_layer_unchanged": unrelated_unchanged,
        "journal_operations": [{"action": row["action"], "outcome": row["outcome"]} for row in operations],
        "journal_event_count": len(cleanup_events),
        "auto_cleanup_performed": False,
        "action_required": "Manually reconcile the unknown upload. No automatic retry or deletion.",
    }
    save_json(output / "lost-response-result.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", default="gp_mask_lab_r02")
    parser.add_argument("--scenario", choices=("standard", "lost-upload"), default="standard")
    args = parser.parse_args()
    from os import environ
    creds = (environ.get("GEOPUB_USERNAME", ""), environ.get("GEOPUB_PASSWORD", ""))
    if not all(creds):
        parser.error("Set both GEOPUB_USERNAME and GEOPUB_PASSWORD for the disposable lab only.")
    if args.scenario == "lost-upload":
        result = run_unknown_upload_gate(
            args.catalog, args.sources, args.output, credentials=creds, workspace=args.workspace
        )
    else:
        result = run_rest_gate(
            args.catalog, args.sources, args.output, credentials=creds, workspace=args.workspace
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "PASSED":
        raise SystemExit("The REST gate did not pass; do not retry blindly or auto-remove objects.")


if __name__ == "__main__":
    main()
