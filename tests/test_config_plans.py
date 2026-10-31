"""Trust-boundary tests for profiles and immutable publication plans.

Only native GDAL inspection is substituted. Source discovery, packaging, SLD
validation, plan serialization, and integrity checks use their real code paths.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from geopublisher import datasets
from geopublisher.config import ConfigError, Environment
from geopublisher.datasets import DatasetError
from geopublisher.plans import (
    build_plan,
    digest_file,
    fingerprint,
    load_plan,
    validate_plan,
    verify_artifacts,
)
from geopublisher.styles import write_default_sld

TASK_ID = "bfb5cbe3-0b6d-4a78-9101-daf747b339b2"
MAX_SLD_BYTES = 5 * 1024 * 1024


def profile(**changes):
    values = {
        "environment_key": "synthetic",
        "base_url": "https://example.invalid/geoserver",
        "workspace": "maps",
        "gdal_bin": "/unused-synthetic-gdal",
    }
    values.update(changes)
    return values


def seal(plan):
    """Recompute the hash so malformed-field tests reach semantic validation."""
    plan.pop("plan_hash", None)
    plan["plan_hash"] = fingerprint(plan)
    return plan


def write_plan(path, plan):
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    source_directory = tmp_path / "inputs"
    source_directory.mkdir()
    source = source_directory / "synthetic.tif"
    source.write_bytes(b"Synthetic raster payload; GDAL inspection is a test substitute.")
    calls = {"probe": 0, "inspect": []}

    class SyntheticGdal:
        def __init__(self, bin_dir, *, expected_version=None, credential_env_names=()):
            assert bin_dir == "/unused-synthetic-gdal"
            assert expected_version == "3.13.3"

        def probe(self):
            calls["probe"] += 1
            return {"gdal_version": "3.13.3", "ready": True}

        def inspect(self, path, *, encoding=None, declared_crs=None):
            path = Path(path)
            calls["inspect"].append(path)
            return {
                "kind": "geotiff",
                "source": str(path),
                "components": [str(path)],
                "crs": "EPSG:4326",
                "native_bbox": [100, 20, 101, 21],
                "wgs84_bbox": [100, 20, 101, 21],
                "feature_count": None,
                "geometry_type": None,
                "fields": [],
                "band_count": 1,
                "warnings": [],
                "blocking_errors": [],
                "gdal_version": "3.13.3",
            }

    monkeypatch.setattr(datasets, "GdalRuntime", SyntheticGdal)
    return SimpleNamespace(
        environment=Environment(**profile()),
        source=source,
        output=tmp_path / "task",
        calls=calls,
        runtime=SyntheticGdal,
    )


@pytest.fixture
def plan(inputs):
    return build_plan(inputs.environment, [inputs.source], inputs.output, task_id=TASK_ID)


@pytest.mark.parametrize("method", ["constructor", "json"])
@pytest.mark.parametrize("value", ["false", "true", "", 0, 1, None, [], {}])
def test_allow_http_requires_boolean_even_when_value_looks_false(tmp_path, method, value):
    values = profile(base_url="http://example.invalid/geoserver", allow_http=value)
    with pytest.raises(ConfigError) as caught:
        if method == "constructor":
            Environment(**values)
        else:
            path = tmp_path / "environment.json"
            path.write_text(json.dumps(values), encoding="utf-8")
            Environment.load(path)
    assert caught.value.code == "INVALID_PROFILE"


def test_remote_plain_http_requires_explicit_true():
    with pytest.raises(ConfigError) as caught:
        Environment(**profile(base_url="http://example.invalid/geoserver", allow_http=False))
    assert caught.value.code == "HTTP_NOT_ALLOWED"
    allowed = Environment(**profile(base_url="http://example.invalid/geoserver", allow_http=True))
    assert allowed.to_dict()["allow_http"] is True


@pytest.mark.parametrize(
    "address", ["http://127.0.0.1:18880/geoserver", "http://localhost/geoserver", "http://[::1]/geoserver"]
)
def test_loopback_can_use_plain_http_without_weakening_remote_policy(address):
    assert Environment(**profile(base_url=address, allow_http=False)).allow_http is False


@pytest.mark.parametrize("version", [True, "1", 1.0, None, 0, 2])
def test_profile_schema_rejects_wrong_type_or_version(version):
    with pytest.raises(ConfigError):
        Environment(**profile(schema_version=version))


@pytest.mark.parametrize(
    "task_id",
    ["", False, 0, 7, [], {}, "-" * 36, "a" * 36, "not-a-uuid", TASK_ID.upper(), TASK_ID.replace("-", "")],
)
def test_build_rejects_explicit_invalid_uuid_before_inspection_or_writes(inputs, task_id):
    with pytest.raises(ConfigError) as caught:
        build_plan(inputs.environment, [inputs.source], inputs.output, task_id=task_id)
    assert caught.value.code == "INVALID_TASK_ID"
    assert inputs.calls == {"probe": 0, "inspect": []}
    assert not inputs.output.exists()


def test_real_plan_pipeline_retains_source_hashes_and_round_trips(inputs):
    before = inputs.source.read_bytes()
    result = build_plan(inputs.environment, [inputs.source], inputs.output, task_id=TASK_ID)
    validate_plan(result)
    verify_artifacts(result)
    assert load_plan(inputs.output / "plan.json") == result
    assert result["ready"] is True
    assert result["task_id"] == TASK_ID
    assert inputs.source.read_bytes() == before
    artifact = result["resources"][0]["artifact"]
    assert Path(artifact["path"]).read_bytes() == before
    assert artifact["source_hashes"] == [
        {"path": str(inputs.source), "sha256": digest_file(inputs.source), "size": len(before)}
    ]


def test_custom_wkt_with_valid_bbox_is_blocked_before_packaging(inputs, monkeypatch):
    original_inspect = inputs.runtime.inspect

    def inspect_custom_crs(self, path, **kwargs):
        info = original_inspect(self, path, **kwargs)
        info["crs"] = (
            'GEOGCS["Synthetic custom geographic CRS",'
            'DATUM["Synthetic datum",SPHEROID["Synthetic sphere",6371000,0]],'
            'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
        )
        return info

    def forbidden_package(*args, **kwargs):
        pytest.fail("An unsupported CRS must block preflight before dataset packaging.")

    monkeypatch.setattr(inputs.runtime, "inspect", inspect_custom_crs)
    monkeypatch.setattr(datasets, "package_dataset", forbidden_package)
    result = build_plan(inputs.environment, [inputs.source], inputs.output, task_id=TASK_ID)
    resource = result["resources"][0]
    assert result["ready"] is False
    assert resource["inspection"]["wgs84_bbox"] == [100, 20, 101, 21]
    assert [issue["code"] for issue in resource["blocking_errors"]] == ["UNSUPPORTED_CRS"]
    assert "artifact" not in resource


def test_new_version_target_is_fixed_by_recorded_uuid(inputs):
    result = build_plan(
        inputs.environment, [inputs.source], inputs.output, strategy="new-version", task_id=TASK_ID
    )
    validate_plan(result)
    name = result["resources"][0]["target_name"]
    assert name == "synthetic__v" + TASK_ID.replace("-", "")[:12]
    assert load_plan(inputs.output / "plan.json")["resources"][0]["target_name"] == name


@pytest.mark.parametrize("value", [None, True, 7, 1.25, "a plan", [], [{}]])
def test_load_plan_rejects_non_object_json_with_structured_error(tmp_path, value):
    with pytest.raises(ConfigError) as caught:
        load_plan(write_plan(tmp_path / "bad-plan.json", value))
    assert caught.value.code == "INVALID_PLAN"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("schema_version",), "1"),
        (("schema_version",), 3),
        (("task_id",), None),
        (("task_id",), "-" * 36),
        (("task_id",), TASK_ID.upper()),
        (("ready",), "true"),
        (("ready",), 1),
        (("enable_wfs",), "false"),
        (("enable_wfs",), 0),
        (("strategy",), "replace"),
        (("strategy",), "create"),
        (("strategy",), None),
        (("strategy",), []),
        (("environment",), []),
        (("environment", "allow_http"), "false"),
        (("output_dir",), "relative/task"),
        (("resources",), []),
        (("resources",), {}),
        (("resources",), None),
        (("resources",), [None]),
        (("resources", 0, "resource_id"), "../escape"),
        (("resources", 0, "resource_id"), "r1"),
        (("resources", 0, "target_name"), "../escape"),
        (("resources", 0, "kind"), "mosaic"),
        (("resources", 0, "blocking_errors"), {}),
        (("resources", 0, "blocking_errors"), [{"code": "BLOCKED", "message": "Synthetic block"}]),
        (("resources", 0, "artifact", "path"), "relative/file.tif"),
        (("resources", 0, "artifact", "sha256"), "a" * 63),
        (("resources", 0, "style", "sha256"), "not-a-hash"),
        (("resources", 0, "artifact", "source_hashes"), []),
        (("resources", 0, "artifact", "source_hashes"), {}),
        (("resources", 0, "artifact", "source_hashes", 0, "path"), "relative/source.tif"),
        (("resources", 0, "artifact", "source_hashes", 0, "sha256"), "invalid"),
        (("resources", 0, "inspection", "wgs84_bbox"), [True, 20, 101, 21]),
        (("resources", 0, "inspection", "wgs84_bbox"), [100, 20, 101]),
        (("resources", 0, "inspection", "wgs84_bbox"), [101, 20, 100, 21]),
        (("resources", 0, "inspection", "wgs84_bbox"), [100, -91, 101, 21]),
    ],
)
def test_semantically_invalid_plans_are_rejected_even_with_matching_hash(tmp_path, plan, path, value):
    target = plan
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value
    seal(plan)
    with pytest.raises(ConfigError):
        validate_plan(plan)
    with pytest.raises(ConfigError):
        load_plan(write_plan(tmp_path / "malformed-plan.json", plan))


@pytest.mark.parametrize("duplicate", ["resource_id", "target_name"])
def test_plan_rejects_duplicate_resource_identity(plan, duplicate):
    second = copy.deepcopy(plan["resources"][0])
    second["resource_id"] = "r0002"
    second["target_name"] = "another_layer"
    second[duplicate] = plan["resources"][0][duplicate]
    plan["resources"].append(second)
    seal(plan)
    with pytest.raises(ConfigError) as caught:
        validate_plan(plan)
    assert caught.value.code == "INVALID_PLAN"


@pytest.mark.parametrize("kind", ["artifact", "style"])
def test_plan_cannot_reference_publication_artifacts_outside_task_directory(tmp_path, plan, kind):
    plan["resources"][0][kind]["path"] = str(tmp_path / "outside-task.file")
    seal(plan)
    with pytest.raises(ConfigError) as caught:
        validate_plan(plan)
    assert caught.value.code == "INVALID_PLAN"


def test_plan_hash_detects_a_changed_strategy_before_semantic_acceptance(tmp_path, plan):
    plan["strategy"] = "skip"
    with pytest.raises(ConfigError) as caught:
        load_plan(write_plan(tmp_path / "changed-plan.json", plan))
    assert caught.value.code == "PLAN_CHANGED"


@pytest.mark.parametrize("linked_part", ["file", "parent"])
def test_build_does_not_resolve_away_explicit_source_symlink(inputs, tmp_path, linked_part):
    linked = tmp_path / "linked-input"
    try:
        if linked_part == "file":
            linked = linked.with_suffix(".tif")
            linked.symlink_to(inputs.source)
            selected = linked
        else:
            linked.symlink_to(inputs.source.parent, target_is_directory=True)
            selected = linked / inputs.source.name
    except OSError:
        pytest.skip("The test filesystem does not permit symlinks.")
    original = inputs.source.read_bytes()
    with pytest.raises(DatasetError) as caught:
        build_plan(inputs.environment, [selected], inputs.output, task_id=TASK_ID)
    assert caught.value.code == "SYMLINK_NOT_ALLOWED"
    assert inputs.calls["inspect"] == []
    assert not inputs.output.exists()
    assert inputs.source.read_bytes() == original


@pytest.mark.parametrize("existing", ["empty-directory", "nonempty-directory", "file"])
def test_build_never_reuses_an_existing_output_location(inputs, existing):
    sentinel = b"Existing task content must remain untouched."
    if existing == "file":
        inputs.output.write_bytes(sentinel)
        existing_file = inputs.output
    else:
        inputs.output.mkdir()
        existing_file = inputs.output / "previous-data.bin"
        if existing == "nonempty-directory":
            existing_file.write_bytes(sentinel)
    with pytest.raises(ConfigError) as caught:
        build_plan(inputs.environment, [inputs.source], inputs.output, task_id=TASK_ID)
    assert caught.value.code == "PLAN_EXISTS"
    assert inputs.calls == {"probe": 0, "inspect": []}
    if existing != "empty-directory":
        assert existing_file.read_bytes() == sentinel
    else:
        assert list(inputs.output.iterdir()) == []


def test_output_created_by_concurrent_actor_is_not_adopted(inputs, monkeypatch):
    original_probe = inputs.runtime.probe
    sentinel = inputs.output / "other-task.txt"

    def competing_probe(self):
        result = original_probe(self)
        inputs.output.mkdir()
        sentinel.write_text("Created after the initial existence check.", encoding="utf-8")
        return result

    monkeypatch.setattr(inputs.runtime, "probe", competing_probe)
    with pytest.raises(ConfigError) as caught:
        build_plan(inputs.environment, [inputs.source], inputs.output, task_id=TASK_ID)
    assert caught.value.code == "PLAN_EXISTS"
    assert inputs.calls["inspect"] == []
    assert list(inputs.output.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "Created after the initial existence check."


def test_small_external_sld_is_copied_and_validated_without_changing_source(inputs, tmp_path):
    style = tmp_path / "input.sld"
    write_default_sld(style, name="synthetic_style", band_count=1)
    before = style.read_bytes()
    result = build_plan(inputs.environment, [inputs.source], inputs.output, task_id=TASK_ID, sld=style)
    assert result["ready"] is True
    copied = Path(result["resources"][0]["style"]["path"])
    assert copied != style
    assert copied.read_bytes() == before
    assert style.read_bytes() == before


def test_oversized_sld_is_blocked_without_reading_or_copying_its_body(inputs, tmp_path, monkeypatch):
    style = tmp_path / "too-large.sld"
    with style.open("wb") as stream:
        stream.truncate(MAX_SLD_BYTES + 1)
    original_open = Path.open

    def guarded_open(path, mode="r", *args, **kwargs):
        if path == style and "r" in mode:
            pytest.fail("The plan builder must reject the oversized SLD before reading it.")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(ConfigError) as caught:
        build_plan(inputs.environment, [inputs.source], inputs.output, task_id=TASK_ID, sld=style)
    assert caught.value.code == "SLD_TOO_LARGE"
    assert not (inputs.output / "r0001" / "style.sld").exists()
    assert not (inputs.output / "plan.json").exists()


def test_sld_growth_after_stat_is_still_bounded_and_rejected(inputs, tmp_path, monkeypatch):
    style = tmp_path / "growing.sld"
    style.write_bytes(b"Initially small.")
    original_open = Path.open
    requested_sizes = []

    class TrackingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()

        def read(self, size=-1):
            requested_sizes.append(size)
            assert 0 < size <= MAX_SLD_BYTES + 1, "Reading user SLD bytes must have a memory bound."
            return self.stream.read(size)

    def growing_open(path, mode="r", *args, **kwargs):
        if path == style and mode == "rb":
            with original_open(path, "wb") as growing:
                growing.truncate(MAX_SLD_BYTES + 1)
            return TrackingStream(original_open(path, mode, *args, **kwargs))
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", growing_open)
    with pytest.raises(ConfigError) as caught:
        build_plan(inputs.environment, [inputs.source], inputs.output, task_id=TASK_ID, sld=style)
    assert caught.value.code == "SLD_TOO_LARGE"
    assert requested_sizes
    assert not (inputs.output / "r0001" / "style.sld").exists()
    assert not (inputs.output / "plan.json").exists()


@pytest.mark.parametrize("selection", ["files", "directory"])
def test_multiple_tiffs_need_intent_before_gdal_or_task_creation(inputs, selection):
    second = inputs.source.with_name("second.tiff")
    second.write_bytes(b"another synthetic raster")
    sources = [inputs.source, second] if selection == "files" else [inputs.source.parent]
    with pytest.raises(DatasetError) as caught:
        build_plan(inputs.environment, sources, inputs.output)
    assert caught.value.code == "RASTER_INTENT_REQUIRED"
    assert inputs.calls == {"probe": 0, "inspect": []}
    assert not inputs.output.exists()


def test_independent_choice_and_deduplicated_origins_survive_plan_restore(inputs):
    second = inputs.source.with_name("second.tiff")
    second.write_bytes(b"another synthetic raster")
    result = build_plan(
        inputs.environment,
        [inputs.source.parent, inputs.source],
        inputs.output,
        raster_mode="independent",
    )
    restored = load_plan(inputs.output / "plan.json")
    assert restored == result
    assert restored["schema_version"] == 2
    intent = restored["import_intent"]
    assert intent["raster_mode"] == "independent"
    assert intent["raster_count"] == intent["resource_count"] == len(restored["resources"]) == 2
    source_entry = next(item for item in intent["datasets"] if item["path"] == str(inputs.source))
    assert {origin["selection"] for origin in source_entry["origins"]} == {0, 1}
    verify_artifacts(restored)


def test_import_directory_cannot_contain_task_output(inputs):
    output = inputs.source.parent / "tasks" / "new-task"
    with pytest.raises(ConfigError) as caught:
        build_plan(inputs.environment, [inputs.source.parent], output)
    assert caught.value.code == "OUTPUT_INSIDE_SOURCE"
    assert inputs.calls == {"probe": 0, "inspect": []}
    assert not output.parent.exists()


@pytest.mark.parametrize(
    "change", ["new-raster", "same-count-rename", "ancestor-marker", "mosaic-marker"]
)
def test_import_context_changes_block_execution_before_client_creation(inputs, monkeypatch, change):
    from geopublisher.engine import run_plan

    result = build_plan(
        inputs.environment, [inputs.source.parent], inputs.output, raster_mode="independent"
    )
    original_targets = [r["target_name"] for r in result["resources"]]
    if change == "new-raster":
        inputs.source.with_name("another.tif").write_bytes(b"new synthetic raster")
    elif change == "same-count-rename":
        inputs.source.rename(inputs.source.with_name("renamed.tif"))
    elif change == "ancestor-marker":
        (inputs.source.parent / "pyramid.properties").write_text(
            "Levels=1,1\nLevelsDirs=full preview\nLevelsNum=2\nGeneralBounds=0,0 1,1\n"
        )
    else:
        (inputs.source.parent / "arbitrary.properties").write_text(
            "Levels=1,1\nLocationAttribute=location\nLevelsNum=1\n"
        )

    def forbidden_client(*args, **kwargs):
        pytest.fail("Import context must be rechecked before any client is created.")

    monkeypatch.setattr(Environment, "client", forbidden_client)
    with pytest.raises(ConfigError) as caught:
        run_plan(result)
    assert caught.value.code in {
        "IMPORT_SELECTION_CHANGED", "UNSUPPORTED_PYRAMID", "UNSUPPORTED_MOSAIC"
    }
    assert [r["target_name"] for r in result["resources"]] == original_targets
    assert not (inputs.output.parent / "geopublisher.sqlite").exists()


@pytest.mark.parametrize("change", ["delete-intent", "wrong-count", "unconfirmed-mode", "wrong-source"])
def test_new_plan_requires_consistent_import_intent_even_after_rehash(plan, change):
    if change == "delete-intent":
        del plan["import_intent"]
    elif change == "wrong-count":
        plan["import_intent"]["raster_count"] = True
    elif change == "unconfirmed-mode":
        plan["import_intent"]["raster_mode"] = None
    else:
        plan["resources"][0]["inspection"]["source"] = str(Path(plan["output_dir"]) / "another.tif")
    seal(plan)
    with pytest.raises(ConfigError) as caught:
        validate_plan(plan)
    assert caught.value.code == "INVALID_PLAN"


def test_legacy_multi_raster_plan_remains_readable_but_cannot_start_network(inputs, monkeypatch):
    from geopublisher.engine import run_plan

    second = inputs.source.with_name("second.tiff")
    second.write_bytes(b"second synthetic raster")
    result = build_plan(
        inputs.environment, [inputs.source.parent], inputs.output, raster_mode="independent"
    )
    result["schema_version"] = 1
    del result["import_intent"]
    seal(result)
    restored = load_plan(write_plan(inputs.output / "legacy-plan.json", result))
    assert restored == result

    def forbidden_client(*args, **kwargs):
        pytest.fail("Legacy multi-TIFF plans must not implicitly acquire independent consent.")

    monkeypatch.setattr(Environment, "client", forbidden_client)
    with pytest.raises(ConfigError) as caught:
        run_plan(restored)
    assert caught.value.code == "LEGACY_RASTER_INTENT_REQUIRED"
    assert not (inputs.output.parent / "geopublisher.sqlite").exists()


def test_legacy_single_raster_still_checks_current_structural_context(plan):
    plan["schema_version"] = 1
    del plan["import_intent"]
    seal(plan)
    validate_plan(plan)
    verify_artifacts(plan)
    (Path(plan["resources"][0]["inspection"]["source"]).parent / "indexer.properties").write_text(
        "UseExistingSchema=true\nTypeName=tiles\nAbsolutePath=false\n"
    )
    with pytest.raises(ConfigError) as caught:
        verify_artifacts(plan)
    assert caught.value.code in {"UNSUPPORTED_MOSAIC", "RASTER_STRUCTURE_REVIEW_REQUIRED"}


def test_cli_explicit_intent_is_required_and_included_in_review_output(inputs, capsys):
    from geopublisher.cli import main

    second = inputs.source.with_name("second.tiff")
    second.write_bytes(b"second synthetic raster")
    config = inputs.output.parent / "environment.json"
    config.write_text(json.dumps(inputs.environment.to_dict()), encoding="utf-8")
    args = [
        "plan", str(inputs.source.parent), "--environment", str(config),
        "--output", str(inputs.output),
    ]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "RASTER_INTENT_REQUIRED"
    assert not inputs.output.exists()
    assert main([*args, "--raster-mode", "independent"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["raster_mode"] == "independent"
    assert summary["raster_count"] == len(summary["resources"]) == 2
