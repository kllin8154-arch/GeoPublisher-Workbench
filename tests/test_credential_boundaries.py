"""Profile credentials must remain excluded at every GDAL entry point."""

from __future__ import annotations

import json
import os

import pytest

from geopublisher import cli, datasets
from geopublisher.config import Environment
from geopublisher.datasets import GdalRuntime
from geopublisher.plans import build_plan


@pytest.mark.parametrize("entry_point", ["offline-doctor", "plan"])
def test_profile_credential_names_override_inheritable_locale_keys(
    tmp_path, monkeypatch, capsys, entry_point
):
    # These are deliberately unusual but valid configured credential references.
    # A default-name denylist or a bare OS allowlist would both miss this case.
    secrets = {"LANG": "synthetic-user-sentinel", "LC_ALL": "synthetic-password-sentinel"}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    observed = []

    class PreflightRuntime(GdalRuntime):
        def probe(self):
            observed.append(self._environment())
            return {"status": "AVAILABLE", "version": "3.13.3"}

        def inspect(self, path, **kwargs):
            # The file is deliberately not real GIS data: this test exercises
            # profile routing and must stop before any publication packaging.
            return {
                "kind": "geotiff",
                "crs": "EPSG:4326",
                "blocking_errors": [{"code": "SYNTHETIC_INPUT", "message": "Test input only"}],
                "warnings": [],
            }

    monkeypatch.setattr(datasets, "GdalRuntime", PreflightRuntime)
    environment = Environment(
        environment_key="credential-boundary-test",
        base_url="http://127.0.0.1:8080/geoserver",
        workspace="test",
        gdal_bin=str(tmp_path),
        username_env="LANG",
        password_env="LC_ALL",
    )
    if entry_point == "offline-doctor":
        profile = tmp_path / "environment.json"
        profile.write_text(json.dumps(environment.to_dict()), encoding="utf-8")
        assert cli.main(["doctor", "--environment", str(profile), "--offline"]) == 0
    else:
        source = tmp_path / "test.tif"
        source.write_bytes(b"deliberately synthetic")
        plan = build_plan(environment, [source], tmp_path / "task")
        assert plan["ready"] is False
        assert "artifact" not in plan["resources"][0]
    assert len(observed) == 1
    assert not set(secrets) & {name.upper() for name in observed[0]}
    assert not set(secrets.values()) & set(observed[0].values())
    assert all(os.environ[name] == value for name, value in secrets.items())
    output = capsys.readouterr()
    assert all(value not in output.out + output.err for value in secrets.values())
