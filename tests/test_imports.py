"""Import grouping, structure markers and frozen-source boundary checks."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from geopublisher import imports
from geopublisher.datasets import DatasetError
from geopublisher.imports import prepare_imports, validate_import_intent, verify_import_intent

MOSAIC = "Levels=1,1\nLocationAttribute=location\nLevelsNum=1\n"
PYRAMID = "Name=synthetic\nLevels=1,1 2,2\nLevelsDirs=high low\nLevelsNum=2\n"


def source(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Synthetic fixture: no GDAL decoding is needed for import classification.")
    return path


def resources(intent):
    return [{"kind": entry["kind"], "inspection": {"source": entry["path"]}} for entry in intent["datasets"]]


def assert_code(code, call):
    with pytest.raises(DatasetError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def test_multiple_rasters_require_explicit_intent_and_keep_original_grouping(tmp_path):
    first = source(tmp_path / "中文 空格" / "nested" / "a.TIFF")
    second = source(first.parent / "b.tif")
    parent = first.parent.parent
    assert_code("RASTER_INTENT_REQUIRED", lambda: prepare_imports([parent]))
    intent = prepare_imports([parent, first, parent], raster_mode="independent")
    assert intent["raster_mode"] == "independent"
    assert intent["raster_count"] == intent["resource_count"] == 2
    assert intent["selections"] == [
        {"path": str(parent), "kind": "directory"},
        {"path": str(first), "kind": "file"},
    ]
    assert intent["datasets"] == [
        {
            "path": str(first),
            "kind": "geotiff",
            "origins": [
                {"selection": 0, "relative_path": "nested/a.TIFF"},
                {"selection": 1, "relative_path": "a.TIFF"},
            ],
        },
        {
            "path": str(second),
            "kind": "geotiff",
            "origins": [
                {"selection": 0, "relative_path": "nested/b.tif"},
            ],
        },
    ]
    validate_import_intent(intent, resources(intent))
    verify_import_intent(intent)


def test_source_discovery_exclusions_do_not_claim_false_parent_origins(tmp_path):
    ordinary = source(tmp_path / "inputs" / "ordinary.shp")
    explicit = source(ordinary.parent / "artifacts" / "tile.tif")
    source(ordinary.parent / ".cache" / "ignored.tif")
    source(ordinary.parent / "build" / "ignored.tif")
    intent = prepare_imports([ordinary.parent, explicit])
    assert intent["resource_count"] == 2
    tile = next(item for item in intent["datasets"] if item["kind"] == "geotiff")
    assert tile["origins"] == [{"selection": 1, "relative_path": "tile.tif"}]
    validate_import_intent(intent, resources(intent))


@pytest.mark.parametrize(
    ("suffix", "argument", "expected"),
    [
        ("tif", None, "single"),
        ("tif", "independent", "independent"),
        ("shp", None, "none"),
        ("shp", "independent", "none"),
    ],
)
def test_single_file_mode_is_explicit_and_vector_only_needs_no_raster_choice(
    tmp_path, suffix, argument, expected
):
    item = source(tmp_path / f"standalone.{suffix}")
    intent = prepare_imports([item], raster_mode=argument)
    assert intent["raster_mode"] == expected
    validate_import_intent(intent, resources(intent))


@pytest.mark.parametrize("selected", ["root", "ancestor", "level", "tile", "index"])
def test_pyramid_cannot_be_bypassed_by_selecting_a_member_or_ancestor(tmp_path, selected):
    root = tmp_path / "input" / "pyramid"
    tile = source(root / "high" / "tile.tif")
    index = source(root / "low" / "index.shp")
    (root / "pyramid.properties").write_text(PYRAMID, encoding="utf-8")
    selection = {"root": root, "ancestor": root.parent, "level": tile.parent, "tile": tile, "index": index}[
        selected
    ]
    assert_code("UNSUPPORTED_PYRAMID", lambda: prepare_imports([selection], raster_mode="independent"))


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("different-name.properties", MOSAIC),
        ("INDEXER.properties", "UseExistingSchema=true\nTypeName=synthetic\nAbsolutePath=true\n"),
        ("indexer.properties", "Schema=*the_geom:Polygon,location:String\n"),
        ("indexer.xml", '<Indexer xmlns="urn:synthetic-indexer"/>'),
    ],
)
def test_mosaic_indices_cannot_be_misclassified_as_business_vectors(tmp_path, filename, content):
    index = source(tmp_path / "input" / "mosaic" / "catalog.shp")
    (index.parent / filename).write_text(content, encoding="utf-8")
    assert_code("UNSUPPORTED_MOSAIC", lambda: prepare_imports([index], raster_mode="independent"))


def test_mosaic_marker_supports_bom_comments_java_separators_and_escaped_keys(tmp_path):
    tile = source(tmp_path / "tile.tif")
    (tmp_path / "arbitrary.PROPERTIES").write_text(
        "\ufeff# synthetic\n! comment\nLeve\\u006cs : 1,1\n"
        "Locati\\\n  onAttribute location\nSuggestedSPI=synthetic.reader\n",
        encoding="utf-8",
    )
    assert_code("UNSUPPORTED_MOSAIC", lambda: prepare_imports([tile]))


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("pyramid.properties", b""),
        ("INDEXER.PROPERTIES", b"Unrelated=value\n"),
        ("indexer.xml", b"<Unrelated/>"),
        (
            "indexer.xml",
            b"<!DOCTYPE a [<!ENTITY secret SYSTEM 'file:///must-not-read'>]><Indexer>&secret;</Indexer>",
        ),
        ("pyramid.properties", b"LevelsDirs=high\nLevels=1,1\nBad\\uXXXY=x\n"),
        ("pyramid.properties", b"x" * (imports._MAX_MARKER_BYTES + 1)),
    ],
)
def test_reserved_marker_must_never_degrade_to_an_independent_file(tmp_path, filename, content):
    tile = source(tmp_path / "tile.tif")
    (tmp_path / filename).write_bytes(content)
    assert_code(
        "RASTER_STRUCTURE_REVIEW_REQUIRED", lambda: prepare_imports([tile], raster_mode="independent")
    )


def test_unreadable_reserved_marker_blocks_but_unrelated_ancestor_properties_do_not(tmp_path, monkeypatch):
    tile = source(tmp_path / "inputs" / "tile.tif")
    unrelated = tmp_path / "unrelated.properties"
    unrelated.write_text("Generic=configuration\n", encoding="utf-8")
    reserved = tile.parent / "indexer.properties"
    real_open = imports.os.open

    def denied(path, flags, *args, **kwargs):
        if Path(path) in {unrelated, reserved}:
            raise PermissionError("Synthetic unreadable marker")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(imports.os, "open", denied)
    assert prepare_imports([tile])["raster_mode"] == "single"
    reserved.write_text("Schema=synthetic\n", encoding="utf-8")
    assert_code("RASTER_STRUCTURE_REVIEW_REQUIRED", lambda: prepare_imports([tile]))


def test_marker_symlink_does_not_hide_a_structure(tmp_path):
    tile = source(tmp_path / "input" / "tile.tif")
    outside = tmp_path / "outside.txt"
    outside.write_text(PYRAMID, encoding="utf-8")
    try:
        (tile.parent / "pyramid.properties").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links require privileges on this platform")
    assert_code("RASTER_STRUCTURE_REVIEW_REQUIRED", lambda: prepare_imports([tile]))


def test_datastore_alone_and_mosaic_in_sibling_directory_do_not_block_independent_data(tmp_path):
    tile = source(tmp_path / "independent" / "tile.tif")
    sibling = source(tmp_path / "mosaic" / "tile.tif")
    (sibling.parent / "indexer.properties").write_text("Schema=synthetic", encoding="utf-8")
    secret = "synthetic-datastore-password-sentinel"
    (tile.parent / "datastore.properties").write_text(
        f"dbtype=postgis\npasswd={secret}\n{MOSAIC}", encoding="utf-8"
    )
    intent = prepare_imports([tile.parent])
    assert intent["raster_mode"] == "single"
    assert secret not in repr(intent)


def test_marked_group_in_mixed_directory_is_not_silently_skipped(tmp_path):
    source(tmp_path / "input" / "ordinary" / "business.shp")
    tile = source(tmp_path / "input" / "mosaic" / "tile.tif")
    (tile.parent / "custom.properties").write_text(MOSAIC, encoding="utf-8")
    assert_code(
        "UNSUPPORTED_MOSAIC", lambda: prepare_imports([tmp_path / "input"], raster_mode="independent")
    )


@pytest.mark.parametrize("change", ["add", "remove", "directory_to_file"])
def test_frozen_selection_membership_and_original_selection_type_cannot_change(tmp_path, change):
    first = source(tmp_path / "inputs" / "first.tif")
    second = source(first.parent / "second.tif")
    intent = prepare_imports([first.parent], raster_mode="independent")
    if change == "add":
        source(first.parent / "third.tif")
    elif change == "remove":
        second.unlink()
    else:
        first.unlink()
        second.unlink()
        first.parent.rmdir()
        first.parent.write_bytes(b"This selected path is no longer a directory.")
    assert_code("IMPORT_SELECTION_CHANGED", lambda: verify_import_intent(intent))


def test_new_marker_invalidates_frozen_single_file_intent_before_execution(tmp_path):
    tile = source(tmp_path / "input" / "tile.tif")
    intent = prepare_imports([tile])
    (tile.parent / "pyramid.properties").write_text(PYRAMID, encoding="utf-8")
    assert_code("UNSUPPORTED_PYRAMID", lambda: verify_import_intent(intent))
    # Loading a plan for inspection/cleanup must remain independent of current sources.
    validate_import_intent(intent, resources(intent))
    tile.unlink()
    validate_import_intent(intent, resources(intent))


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), True),
        (("raster_count",), True),
        (("resource_count",), 2),
        (("raster_mode",), "none"),
        (("raster_mode",), "mosaic"),
        (("selections", 0, "kind"), "folder"),
        (("selections", 0, "path"), "relative/path.tif"),
        (("datasets", 0, "origins", 0, "selection"), True),
        (("datasets", 0, "origins", 0, "selection"), -1),
        (("datasets", 0, "origins", 0, "relative_path"), "../tile.tif"),
        (("datasets", 0, "origins"), []),
        (("datasets", 0, "kind"), "shapefile"),
    ],
)
def test_manifest_semantic_tampering_is_rejected_even_before_hash_validation(tmp_path, path, replacement):
    tile = source(tmp_path / "input" / "tile.tif")
    intent = prepare_imports([tile])
    expected_resources = resources(intent)
    value = intent
    for component in path[:-1]:
        value = value[component]
    value[path[-1]] = replacement
    assert_code("INVALID_IMPORT_INTENT", lambda: validate_import_intent(intent, expected_resources))


def test_all_overlapping_origins_and_resource_order_are_frozen(tmp_path):
    first = source(tmp_path / "input" / "a.tif")
    source(first.parent / "b.tif")
    intent = prepare_imports([first.parent, first], raster_mode="independent")
    wrong = copy.deepcopy(intent)
    wrong["datasets"][0]["origins"].pop()
    assert_code("INVALID_IMPORT_INTENT", lambda: validate_import_intent(wrong, resources(intent)))
    assert_code("INVALID_IMPORT_INTENT", lambda: validate_import_intent(intent, resources(intent)[::-1]))


@pytest.mark.parametrize("mode", [True, 1, [], {}, "single", "mosaic", "pyramid", ""])
def test_unsupported_publication_mode_cannot_enable_imports(tmp_path, mode):
    tile = source(tmp_path / "tile.tif")
    assert_code("INVALID_RASTER_MODE", lambda: prepare_imports([tile], raster_mode=mode))


def test_classifier_does_not_invoke_gdal_or_contact_a_service(tmp_path, monkeypatch):
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("Import classification must stay local and must not invoke a child process.")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    first = source(tmp_path / "first.tif")
    second = source(tmp_path / "second.tif")
    assert_code("RASTER_INTENT_REQUIRED", lambda: prepare_imports([first, second]))
    intent = prepare_imports([first, second], raster_mode="independent")
    verify_import_intent(intent)


def test_reserved_marker_directory_is_not_treated_as_absent(tmp_path):
    tile = source(tmp_path / "tile.tif")
    (tmp_path / "pyramid.properties").mkdir()
    assert_code("RASTER_STRUCTURE_REVIEW_REQUIRED", lambda: prepare_imports([tile]))
