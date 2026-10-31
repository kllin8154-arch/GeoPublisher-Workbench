"""Run only against a disposable loopback GeoServer, using generated samples."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from geopublisher.config import Environment
from geopublisher.engine import rollback_plan, run_plan
from geopublisher.geoserver import GeoServerClient
from geopublisher.journal import Journal
from geopublisher.plans import build_plan, fingerprint, save_json


class LoseUploadResponse(httpx.BaseTransport):
    """Complete one real server write, then emulate loss of its response."""

    def __init__(self):
        self.transport = httpx.HTTPTransport(retries=0)
        self.dropped = False
        self.write_count = 0

    def handle_request(self, request):
        if request.method in {"POST", "PUT", "DELETE"}:
            self.write_count += 1
        response = self.transport.handle_request(request)
        if not self.dropped and request.method == "PUT" and request.url.path.endswith("/file.shp"):
            response.read()
            response.close()
            self.dropped = True
            raise httpx.ReadTimeout("Synthetic response loss", request=request)
        return response

    def close(self):
        self.transport.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env = Environment.load(args.environment)
    if urlsplit(env.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Integration checks are restricted to disposable loopback servers.")
    creds = env.credentials()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with env.client() as client:
        probe = client.probe()
        if client.get_workspace(env.workspace) is None:
            client.create_workspace(env.workspace)
        unrelated_before = client.get_layer("topp", "states")
        plan = build_plan(env, [args.samples], output / "publish", strategy="new-version", enable_wfs=True)
        assert plan["ready"], "Synthetic sample preflight blocked"
        first = run_plan(plan, credentials=creds)
        assert first["status"] == "SUCCEEDED", first
        with Journal(output / "geopublisher.sqlite") as journal:
            writes_before = len(journal.operations(plan["task_id"]))
        second = run_plan(plan, credentials=creds)
        assert second["status"] == "SUCCEEDED", second
        with Journal(output / "geopublisher.sqlite") as journal:
            writes_after = len(journal.operations(plan["task_id"]))
        assert writes_before == writes_after, "Retry performed duplicate writes"
        cleanup = rollback_plan(plan, credentials=creds)
        assert cleanup["status"] == "ROLLED_BACK"
        assert all(client.get_layer(env.workspace, item["target_name"]) is None for item in plan["resources"])
        assert fingerprint(client.get_layer("topp", "states")) == fingerprint(unrelated_before)

        interrupted = build_plan(
            env, [args.samples / "points_utf8.shp"], output / "unknown-upload", strategy="new-version"
        )
        transport = LoseUploadResponse()
        with GeoServerClient(env.base_url, *creds, transport=transport) as lossy:
            unknown = run_plan(interrupted, client=lossy)
            assert unknown["status"] == "MANUAL_ACTION_REQUIRED", unknown
            count = transport.write_count
            retried = run_plan(interrupted, client=lossy)
            assert retried["status"] == "MANUAL_ACTION_REQUIRED", retried
            assert count == transport.write_count, "Unknown upload was replayed"
            assert (
                client.get_store(env.workspace, interrupted["resources"][0]["store_name"], "shapefile")
                is not None
            )
    result = {
        "geoserver": probe["geoserver_version"],
        "gdal": plan["capabilities"]["version"],
        "resource_count": len(first["resources"]),
        "wms_successes": sum(r["status"] == "SUCCEEDED" for r in first["resources"]),
        "wfs_vector_successes": sum(
            bool(r.get("wfs", {}).get("sample_count")) for r in first["resources"] if r.get("wfs")
        ),
        "first_run_write_operations": writes_before,
        "repeat_run_additional_writes": writes_after - writes_before,
        "cleanup": cleanup["status"],
        "cleanup_removed_objects": len(cleanup["removed"]),
        "unrelated_layer_unchanged": True,
        "lost_upload_response": unknown["status"],
        "lost_upload_response_retry_writes": 0,
        "server_data_retained": True,
        "data_origin": "synthetic-only",
    }
    save_json(output / "integration-results.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
