# MVP implementation contract — v0.2

## Scope

Independent Python core and CLI, then a Windows-first PySide6 UI. SHP and individual GeoTIFF resources can be published in batches. SLD or generated simple styles only. QML, Mosaic/Pyramid, original-data replacement, database writes, arbitrary scripts, and direct GeoServer data-directory edits are out of this release.

All samples are synthetic. Repository content must not include the uploaded PRD, real database schemas, real endpoints, project names, or site data.

## Module ownership

- Root: config/model/plan, engine, CLI, integration, repository, documentation, packaging.
- GeoServer agent: `geoserver.py`, `verification.py`, relevant tests.
- Data agent: `datasets.py`, `styles.py`, `scripts/generate_samples.py`, relevant tests.
- Journal agent: `journal.py`, relevant tests.

## Common conventions

Python 3.11+, pathlib paths, immutable source inputs. Result values are JSON-serializable dictionaries or dataclasses with `to_dict()`. Exceptions carry a stable `code` and safe message; never include Authorization, passwords or full server response bodies. No shell=True.

## GeoServer adapter

`GeoServerClient(base_url: str, username: str, password: str, *, timeout: float = 60, verify: bool | str = True, transport=None)` supports context manager / close. Exact endpoint and no redirects, trust_env=False. The root validates allowed base URL.

Required methods (workspace always explicit):

- `probe() -> dict`
- `get_workspace(workspace) -> dict | None`, `create_workspace(workspace) -> dict`
- `get_store(workspace, name, kind) -> dict | None` (`kind`: shapefile/geotiff)
- `get_layer(workspace, name) -> dict | None`
- `get_style(workspace, name) -> dict | None`
- `upload_dataset(workspace, name, kind, path, *, encoding=None) -> dict`; SHP zip / TIFF; configure=none, which still may create store. No automatic retries.
- `publish_resource(workspace, store, name, kind, *, native_name=None, crs=None) -> dict`
- `upload_style(workspace, name, path) -> dict`; create only, no update existing.
- `bind_style(workspace, layer, style) -> dict`
- `get_map(workspace, layer, *, bbox, crs="EPSG:4326", width=512, height=512, version="1.1.1") -> response-like object`
- `get_features(workspace, layer, *, limit=5) -> dict`
- exact deletes `delete_layer(workspace,name)`, `delete_resource(workspace,store,name,kind)`, `delete_store(workspace,name,kind)`, `delete_style(workspace,name)`; never recursive, never purge=all. Only root calls after ownership verification.

`GeoServerError(code, message, *, status_code=None, outcome_unknown=False)`. Unknown write outcomes include timeouts, connection loss, and 5xx. Read 404 returns None only on resource getters. 401/403 are explicit errors.

Verification module: `verify_image(content: bytes, content_type: str, *, expected_size=(512,512), allow_empty=False) -> dict`, checks PNG decoding, dimensions, alpha/non-background, XML errors. Return status (`PASSED`/`REVIEW_REQUIRED`), metrics and sha256. Transparent results are not automatic success. `wms_bbox(bbox,crs,version)` handles EPSG:4326 axis order. No hidden fallback enabling WFS.

## Dataset / style adapter

`GdalRuntime(bin_dir: Path | str | None, *, expected_version: str | None = None, credential_env_names=())`; bin_dir explicit or packaged runtime only, never silently switch to system PATH. Methods `probe() -> dict`, `inspect(path, *, encoding=None, declared_crs=None) -> dict`. subprocess timeout, argument arrays, PROJ_NETWORK=OFF. Structured errors.

GDAL subprocesses receive an explicit minimal OS environment rather than a copy of the parent environment. Default publishing credential names and configured `username_env` / `password_env` references are excluded case-insensitively, including references that overlap an otherwise allowed OS variable. Do not mutate the parent's environment: its credentials remain available to the HTTP client. Runtime PATH/data directories are controlled; inherited cloud credentials, proxy credentials, user profiles and arbitrary executable search paths are not forwarded. The plan builder and CLI doctor must pass the environment profile's credential reference names; the desktop uses the same plan builder. Windows compatibility of any environment-policy change needs a targeted native recheck.

Inspection dict keys: `kind`, `source`, `components` (absolute path list), `crs` (EPSG or WKT), `native_bbox` [xmin,ymin,xmax,ymax], `wgs84_bbox` or null, `feature_count`, `geometry_type`, `fields` list, `band_count`, `warnings` list, `blocking_errors` list, `gdal_version`. Missing CRS or unconfirmed unknown encoding blocks; never infer from default. `.shp/.shx/.dbf` mandatory, `.prj` or explicit declared_crs; `.cpg` or explicit encoding. No automatic repair/reprojection in initial release.

`discover_sources(paths: list[Path]) -> list[Path]` is the low-level supported-file walker. Publication must use `imports.prepare_imports(sources, raster_mode=None)` first: known Mosaic/Pyramid structure contexts block even for explicit child files or index SHP, and multiple TIFFs require `raster_mode="independent"`. The resulting JSON preserves selections, per-dataset relative origins, deduplicated resources, mode and counts. Marker checks never initialize indexes, follow external configuration references or disclose configuration values.

New plans use schema version 2 and require `import_intent`. `validate_import_intent(intent, resources)` validates provenance and resource correspondence without filesystem access, so reading a plan or cleaning up owned objects does not require original inputs. `verify_import_intent(intent)` is execution-only: repeat the same local selection checks and reject any difference; never expand a restored plan. Plan output must be outside selected directories. Legacy schema 1 remains readable/cleanable; execution rejects unconfirmed multiple TIFFs and rechecks source contexts for single-TIFF/vector tasks before client creation. Detailed supported markers and detection limits are in `docs/RASTER-IMPORTS.md`.

`package_dataset(info: dict, destination: Path) -> dict` creates immutable copy/zip with components; safe names; returns `path`, `sha256`, `source_hashes` list (`path`,`sha256`,`size`), and size. Does not modify source files. Avoid losing TIFF sidecars: initially fail/report unsupported external dependencies unless safely embedded/packaged.

`validate_sld(path: Path, *, fields=None) -> dict` defused XML; only SLD 1.0/1.1 roots; reports field references; rejects external graphics/remote resource references for MVP.
`write_default_sld(path: Path, *, name: str, geometry_type: str | None = None, band_count: int | None = None) -> Path` deterministic point/line/polygon or raster style. All XML generated safely.

## SQLite journal

`Journal(path: Path | str)` context manager / close; schema version migration, WAL, full synchronous, foreign keys, busy timeout. Accepts JSON dicts, no dependency on root models.

- `create_task(task_id, plan: dict)`, `get_task(task_id) -> dict`, `list_tasks() -> list[dict]`
- `set_task_status(task_id,status)`
- `set_resource(task_id,resource_id, *, status, stage, details: dict | None=None)` / `get_resources(task_id)`
- `begin_operation(task_id,resource_id,action,target: dict,expected: dict | None=None) -> operation_id`; persists INTENT before request
- `finish_operation(operation_id, *, outcome, evidence: dict | None=None)`; outcome SUCCEEDED/FAILED/UNKNOWN
- `operations(task_id, resource_id=None) -> list[dict]`
- `record_object(task_id,resource_id,kind,name, *, fingerprint: str, details: dict | None=None)` / `objects(task_id,resource_id=None)`
- `add_event(task_id,level,code,message, *, resource_id=None,details=None)` / `events(task_id)`
- `acquire_lock(task_id, lock_key)` / `release_locks(task_id)`; cross-process SQLite atomic locks, never steal another task's lock silently.

Task getter includes `task_id`,`status`,`plan`, timestamps. Owned object getter includes `kind`,`name`,`resource_id`,`fingerprint`,`details`. Intent getter includes `operation_id`,`resource_id`,`action`,`target`,`expected`,`outcome`,`evidence`. Secrets rejected recursively by sensitive keys rather than stored; root also supplies sanitized values only.

## Integration principles

Plan persists absolute resolved input references internally, hashes, fixed target names, expected resources, and unique ownership markers. New versions get a deterministic task-derived suffix fixed once. Verify target absence again before create. Workspaces should be pre-existing for first usable release; explicit workspace creation may be added as a controlled option. One resource → one store and independently named style to avoid shared dependencies.

For unknown create/update response, compare remote state with recorded intent. Without positive ownership/content evidence, MANUAL_ACTION_REQUIRED rather than replay/delete. Existing skipped resources remain SKIPPED, not PUBLISHED. Report WMS success separately from SQL generation and manual catalog registration.


## Optional immutable conversion lineage in plan v2

A resource prepared from an explicitly recorded offline GDB/MXD SHP output may
carry conversion_origin: batch_id, source_format, source_name, source_layer,
source_frame, manifest (local absolute path), manifest_sha256 and conversion_status.
The plan hash freezes these values. build_plan reads and validates the matching
local GDB/MXD manifest; verify_artifacts verifies the manifest and membership
again before connecting to GeoServer. An unrecorded, failed or modified SHP in
a conversion directory cannot silently join the batch.

The catalog draft groups these resources by source file into database-node
parent candidates, each layer pointing to the new parent's key. This is a
pending PostgreSQL INSERT profile, not an in-memory UI-only tree or an executed
database mutation. The site schema, actual topic/scale parent, WMS visual
acceptance, conversion/style acceptance and reviewed SQL remain mandatory.
