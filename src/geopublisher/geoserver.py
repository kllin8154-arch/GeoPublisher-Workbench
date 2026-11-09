"""GeoServer REST/WMS adapter with explicit, non-retrying write semantics.

The adapter is not a distributed lock or a rollback engine. The caller journals
intent before mutation and records ownership after verified successful writes.
An unknown response must be reconciled before any repeat write or compensation.
"""

from __future__ import annotations

import json
import math
import re
import ssl
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .verification import VerificationError, wms_bbox


class GeoServerError(RuntimeError):
    """Safe public error: never embeds a URL, credentials, or response body."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        outcome_unknown: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "status_code": self.status_code,
            "outcome_unknown": self.outcome_unknown,
        }


@dataclass(frozen=True)
class MapResponse:
    """A response without HTTP request/authentication objects attached."""

    status_code: int
    headers: dict[str, str]
    content: bytes


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_ENCODING = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_SECRET_KEYS = {
    "authorization",
    "proxyauthorization",
    "password",
    "passwd",
    "pwd",
    "secret",
    "clientsecret",
    "token",
    "accesstoken",
    "refreshtoken",
    "cookie",
    "setcookie",
    "credentials",
    "credential",
    "apikey",
    "privatekey",
}
_STORE_TYPES = {
    "shapefile": ("datastores", "featuretypes", "featureType", "shp", "application/zip"),
    "geotiff": ("coveragestores", "coverages", "coverage", "geotiff", "image/tiff"),
}


def _name(value: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value) or value in {".", ".."}:
        raise GeoServerError(
            "INVALID_NAME", "Names must be safe ASCII identifiers of at most 128 characters."
        )
    return value


def _kind(value: str) -> tuple[str, str, str, str, str]:
    try:
        return _STORE_TYPES[value]
    except (KeyError, TypeError):
        raise GeoServerError(
            "UNSUPPORTED_STORE", "Only Shapefile and individual GeoTIFF stores are supported."
        ) from None


def _is_secret(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SECRET_KEYS or normalized.endswith(("password", "secret", "token"))


def _sanitize(value: Any) -> Any:
    """Strip credentials from server metadata before it can reach a journal."""
    if isinstance(value, dict):
        if _is_secret(str(value.get("@key", ""))):
            return {
                key: ("[REDACTED]" if key in {"$", "value"} else _sanitize(item))
                for key, item in value.items()
            }
        return {
            key: ("[REDACTED]" if _is_secret(str(key)) else _sanitize(item)) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str) and re.match(r"^https?://", value, re.I):
        try:
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                host = parsed.hostname or ""
                if ":" in host:
                    host = f"[{host}]"
                if parsed.port is not None:
                    host = f"{host}:{parsed.port}"
                return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            return "[REDACTED URL]"
    return value


class _FileStream(httpx.SyncByteStream):
    def __init__(self, path: Path):
        self.path = path

    def __iter__(self) -> Iterator[bytes]:
        with self.path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk


class GeoServerClient:
    """Synchronous adapter; use one client per worker, not across threads."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 60,
        verify: bool | str = True,
        transport: httpx.BaseTransport | None = None,
    ):
        try:
            parsed = urlsplit(base_url)
            invalid = (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or bool(parsed.query or parsed.fragment)
                or any(char.isspace() or ord(char) < 32 for char in base_url)
                or any(part in {".", ".."} for part in unquote(parsed.path).split("/"))
            )
            _ = parsed.port
        except (ValueError, TypeError):
            invalid = True
        if invalid:
            raise GeoServerError(
                "INVALID_ENDPOINT",
                "The GeoServer endpoint must be an explicit HTTP(S) base URL without credentials or query parameters.",
            )
        if verify is False:
            raise GeoServerError(
                "TLS_VERIFICATION_REQUIRED", "TLS certificate verification cannot be disabled."
            )
        if not isinstance(username, str) or not isinstance(password, str) or ":" in username:
            raise GeoServerError("INVALID_CREDENTIALS", "Authentication values have an invalid format.")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise GeoServerError("INVALID_TIMEOUT", "The request timeout must be a positive finite number.")
        tls_config: ssl.SSLContext | bool = True
        if isinstance(verify, str):
            try:
                tls_config = ssl.create_default_context(cafile=verify)
            except (OSError, ssl.SSLError):
                raise GeoServerError(
                    "INVALID_CA_BUNDLE", "The configured CA bundle cannot be loaded."
                ) from None
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            auth=httpx.BasicAuth(username, password),
            timeout=httpx.Timeout(timeout, connect=min(timeout, 15)),
            verify=tls_config,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"User-Agent": "GeoPublisher-Workbench/0.1", "Accept": "application/json"},
        )

    def __enter__(self) -> GeoServerClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict | None = None,
        payload: dict | None = None,
        content: Any = None,
        headers: dict | None = None,
        allow_missing: bool = False,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> MapResponse | None:
        mutation = method not in {"GET", "HEAD"}
        try:
            request = self._client.build_request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=payload,
                content=content,
                headers=headers,
            )
            with closing(self._client.send(request, stream=True)) as response:
                status = response.status_code
                if allow_missing and status == 404:
                    return None
                if status in {401, 403}:
                    code = "UNAUTHORIZED" if status == 401 else "FORBIDDEN"
                    raise GeoServerError(
                        code,
                        f"GeoServer refused authentication or authorization for {operation}.",
                        status_code=status,
                    )
                if 300 <= status < 400:
                    raise GeoServerError(
                        "REDIRECT_REJECTED",
                        "GeoServer returned a redirect; configure its exact endpoint explicitly.",
                        status_code=status,
                        outcome_unknown=mutation,
                    )
                if not 200 <= status < 300:
                    codes = {
                        404: "NOT_FOUND",
                        405: "METHOD_NOT_ALLOWED",
                        409: "CONFLICT",
                        413: "UPLOAD_TOO_LARGE",
                        415: "UNSUPPORTED_MEDIA_TYPE",
                        429: "RATE_LIMITED",
                    }
                    code = codes.get(status, "SERVER_ERROR" if status >= 500 else "REQUEST_REJECTED")
                    raise GeoServerError(
                        code,
                        f"GeoServer rejected {operation} (HTTP {status}).",
                        status_code=status,
                        outcome_unknown=mutation and (status >= 500 or status in {408, 429}),
                    )
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise GeoServerError(
                            "RESPONSE_TOO_LARGE",
                            f"GeoServer returned an oversized response for {operation}.",
                            status_code=status,
                            outcome_unknown=mutation,
                        )
                    chunks.append(chunk)
                safe_headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() in {"content-type", "content-length"}
                }
                return MapResponse(status, safe_headers, b"".join(chunks))
        except GeoServerError:
            raise
        except httpx.TimeoutException:
            raise GeoServerError(
                "REQUEST_TIMEOUT",
                f"GeoServer did not complete {operation} within the configured timeout.",
                outcome_unknown=mutation,
            ) from None
        except httpx.RequestError:
            raise GeoServerError(
                "CONNECTION_ERROR", f"The connection failed during {operation}.", outcome_unknown=mutation
            ) from None
        except OSError:
            raise GeoServerError(
                "LOCAL_IO_ERROR",
                f"A local file could not be read during {operation}.",
                outcome_unknown=mutation,
            ) from None

    @staticmethod
    def _json(response: MapResponse, *, wrapper: str | None = None) -> dict:
        try:
            data = json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            raise GeoServerError(
                "INVALID_RESPONSE",
                "GeoServer did not return the expected JSON metadata.",
                status_code=response.status_code,
            ) from None
        if not isinstance(data, dict):
            raise GeoServerError(
                "INVALID_RESPONSE",
                "GeoServer returned an invalid metadata structure.",
                status_code=response.status_code,
            )
        if wrapper:
            data = data.get(wrapper)
            if not isinstance(data, dict):
                raise GeoServerError(
                    "INVALID_RESPONSE",
                    "GeoServer metadata is missing its expected resource object.",
                    status_code=response.status_code,
                )
        return _sanitize(data)

    @staticmethod
    def _evidence(response: MapResponse, method: str, operation: str) -> dict:
        return {"status_code": response.status_code, "method": method, "operation": operation}

    def _get(self, path: str, wrapper: str, operation: str) -> dict | None:
        response = self._request("GET", path, operation=operation, allow_missing=True)
        return None if response is None else self._json(response, wrapper=wrapper)

    def probe(self) -> dict:
        try:
            response = self._request("GET", "/rest/about/version.json", operation="read version")
        except GeoServerError as error:
            if error.status_code != 403:
                raise
            response = self._request("GET", "/rest/workspaces.json", operation="probe REST access")
            self._json(response)
            return {
                "available": True,
                "geoserver_version": None,
                "warnings": ["VERSION_NOT_AUTHORIZED"],
                "status_code": response.status_code,
            }
        data = self._json(response)
        resources = data.get("about", {}).get("resource", []) if isinstance(data.get("about"), dict) else []
        if isinstance(resources, dict):
            resources = [resources]
        version = None
        for item in resources:
            if isinstance(item, dict) and str(item.get("@name", item.get("name", ""))).lower() == "geoserver":
                candidate = str(item.get("Version", item.get("version", "")))
                if re.fullmatch(r"[0-9][A-Za-z0-9._+-]{0,63}", candidate):
                    version = candidate
        return {
            "available": True,
            "geoserver_version": version,
            "warnings": [] if version else ["VERSION_UNRECOGNIZED"],
            "status_code": response.status_code,
        }

    def get_workspace(self, workspace: str) -> dict | None:
        return self._get(f"/rest/workspaces/{_name(workspace)}.json", "workspace", "read workspace")

    def create_workspace(self, workspace: str) -> dict:
        workspace = _name(workspace)
        if self.get_workspace(workspace) is not None:
            raise GeoServerError("CONFLICT", "The target workspace already exists.", status_code=409)
        response = self._request(
            "POST",
            "/rest/workspaces",
            operation="create workspace",
            payload={"workspace": {"name": workspace}},
        )
        return self._evidence(response, "POST", "create_workspace")

    def get_store(self, workspace: str, name: str, kind: str) -> dict | None:
        stores, _, _, _, _ = _kind(kind)
        wrapper = "dataStore" if kind == "shapefile" else "coverageStore"
        return self._get(
            f"/rest/workspaces/{_name(workspace)}/{stores}/{_name(name)}.json", wrapper, "read store"
        )

    def get_layer(self, workspace: str, name: str) -> dict | None:
        qualified = quote(f"{_name(workspace)}:{_name(name)}", safe=":")
        return self._get(f"/rest/layers/{qualified}.json", "layer", "read layer")

    def get_resource(self, workspace: str, store: str, name: str, kind: str) -> dict | None:
        stores, resources, wrapper, _, _ = _kind(kind)
        return self._get(
            f"/rest/workspaces/{_name(workspace)}/{stores}/{_name(store)}/{resources}/{_name(name)}.json",
            wrapper,
            "read resource",
        )

    def get_style(self, workspace: str, name: str) -> dict | None:
        return self._get(
            f"/rest/workspaces/{_name(workspace)}/styles/{_name(name)}.json", "style", "read style"
        )

    def get_style_content(self, workspace: str, name: str) -> bytes:
        """Read the raw SLD at the same explicit endpoint for content hashing.

        Metadata alone cannot reveal an edit to the style file. A missing style, an
        HTML login page, malformed XML, and entity-bearing XML are explicit errors.
        """
        response = self._request(
            "GET",
            f"/rest/workspaces/{_name(workspace)}/styles/{_name(name)}.sld",
            operation="read style content",
            params={"raw": "true"},
            headers={"Accept": "application/vnd.ogc.sld+xml,application/xml,text/xml"},
            max_bytes=5 * 1024 * 1024,
        )
        try:
            root = ElementTree.fromstring(response.content)
            if root.tag not in {
                "{http://www.opengis.net/sld}StyledLayerDescriptor",
                "StyledLayerDescriptor",
            }:
                raise ValueError
        except (ElementTree.ParseError, DefusedXmlException, ValueError):
            raise GeoServerError(
                "INVALID_STYLE_RESPONSE", "GeoServer did not return a valid, safe SLD document."
            ) from None
        return response.content

    def upload_dataset(
        self, workspace: str, name: str, kind: str, path: Path | str, *, encoding: str | None = None
    ) -> dict:
        stores, _, _, extension, media_type = _kind(kind)
        workspace, name = _name(workspace), _name(name)
        path = Path(path)
        if not path.is_file() or path.is_symlink():
            raise GeoServerError("INVALID_UPLOAD", "The upload must be a regular packaged data file.")
        if (kind == "shapefile" and path.suffix.lower() != ".zip") or (
            kind == "geotiff" and path.suffix.lower() not in {".tif", ".tiff"}
        ):
            raise GeoServerError("INVALID_UPLOAD", "The upload file extension does not match its store type.")
        if path.stat().st_size <= 0:
            raise GeoServerError("INVALID_UPLOAD", "The upload file is empty.")
        params = {"configure": "none"}
        if encoding is not None:
            if not _ENCODING.fullmatch(encoding):
                raise GeoServerError("INVALID_ENCODING", "The Shapefile encoding name has an invalid format.")
            if kind == "shapefile":
                params["charset"] = encoding
        # This is an additional guard, not a server-side compare-and-swap. The
        # caller must still protect the execution window from external writers.
        if self.get_store(workspace, name, kind) is not None:
            raise GeoServerError(
                "CONFLICT", "Dataset upload refuses to overwrite an existing store.", status_code=409
            )
        response = self._request(
            "PUT",
            f"/rest/workspaces/{workspace}/{stores}/{name}/file.{extension}",
            operation="upload dataset",
            params=params,
            content=_FileStream(path),
            headers={"Content-Type": media_type, "Content-Length": str(path.stat().st_size)},
        )
        return self._evidence(response, "PUT", "upload_dataset")

    def register_external_raster(
        self, workspace: str, store: str, layer: str,
        kind: str, server_path: str,
    ) -> dict:
        """Create only a *new* server-local GeoTIFF/ImageMosaic coverage store.

        Input is a path *already resident* on the GeoServer host. The caller
        must verify that path and archive integrity via SSH/local preflight.
        No dataset bytes pass through REST; the operation may be UNKNOWN on
        timeouts and must never be retried without reconciliation.
        """
        workspace, store, layer = _name(workspace), _name(store), _name(layer)
        if kind not in {"geotiff", "imagemosaic"}:
            raise GeoServerError("UNSUPPORTED_EXTERNAL_RASTER", "External raster only supports geotiff/imagemosaic.")
        if (
            not isinstance(server_path, str) or not server_path.startswith("/")
            or len(server_path) > 4096 or "\\" in server_path
            or any(part in {"", ".", ".."} for part in server_path.split("/")[1:])
            or any(ord(char) < 32 for char in server_path)
        ):
            raise GeoServerError("INVALID_EXTERNAL_PATH", "Server-local raster path is not a safe absolute Linux path.")
        if self.get_store(workspace, store, "geotiff") is not None:
            raise GeoServerError("CONFLICT", "Existing raster store must never be replaced.", status_code=409)
        if self.get_layer(workspace, layer) is not None:
            raise GeoServerError("CONFLICT", "Existing raster layer must never be replaced.", status_code=409)
        response = self._request(
            "PUT",
            f"/rest/workspaces/{workspace}/coveragestores/{store}/external.{kind}",
            operation="register server-local raster",
            params={"configure": "all", "coverageName": layer},
            content=server_path.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=UTF-8"},
        )
        return self._evidence(response, "PUT", "register_external_raster")

    def publish_resource(
        self,
        workspace: str,
        store: str,
        name: str,
        kind: str,
        *,
        native_name: str | None = None,
        crs: str | None = None,
    ) -> dict:
        stores, resources, wrapper, _, _ = _kind(kind)
        workspace, store, name = _name(workspace), _name(store), _name(name)
        if self.get_layer(workspace, name) is not None:
            raise GeoServerError("CONFLICT", "The target layer already exists.", status_code=409)
        resource: dict[str, Any] = {"name": name, "title": name, "enabled": True}
        if native_name is not None:
            if (
                not isinstance(native_name, str)
                or not native_name
                or len(native_name) > 255
                or any(ord(char) < 32 for char in native_name)
                or any(char in native_name for char in "/\\")
            ):
                raise GeoServerError("INVALID_NATIVE_NAME", "The source layer name is invalid.")
            resource["nativeName"] = native_name
        if crs is not None:
            if not isinstance(crs, str) or not re.fullmatch(r"EPSG:[0-9]{1,8}", crs, re.I):
                raise GeoServerError(
                    "UNSUPPORTED_CRS",
                    "Publication requires a verified EPSG code; custom CRS publication is not supported yet.",
                )
            resource["srs"] = crs.upper()
            resource["projectionPolicy"] = "FORCE_DECLARED"
        response = self._request(
            "POST",
            f"/rest/workspaces/{workspace}/{stores}/{store}/{resources}",
            operation="publish resource",
            payload={wrapper: resource},
        )
        return self._evidence(response, "POST", "publish_resource")

    def upload_style(self, workspace: str, name: str, path: Path | str) -> dict:
        workspace, name = _name(workspace), _name(name)
        path = Path(path)
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 5 * 1024 * 1024:
            raise GeoServerError(
                "INVALID_STYLE", "The style must be a regular SLD file within the size limit."
            )
        if self.get_style(workspace, name) is not None:
            raise GeoServerError(
                "CONFLICT", "Style creation refuses to overwrite an existing style.", status_code=409
            )
        response = self._request(
            "POST",
            f"/rest/workspaces/{workspace}/styles",
            operation="create style",
            params={"name": name},
            content=_FileStream(path),
            headers={
                "Content-Type": "application/vnd.ogc.sld+xml",
                "Content-Length": str(path.stat().st_size),
            },
        )
        return self._evidence(response, "POST", "upload_style")

    def bind_style(self, workspace: str, layer: str, style: str) -> dict:
        workspace, layer, style = _name(workspace), _name(layer), _name(style)
        response = self._request(
            "PUT",
            f"/rest/layers/{workspace}:{layer}",
            operation="bind default style",
            payload={"layer": {"defaultStyle": {"name": style, "workspace": workspace}}},
        )
        return self._evidence(response, "PUT", "bind_style")

    def get_map(
        self,
        workspace: str,
        layer: str,
        *,
        bbox: Any,
        crs: str = "EPSG:4326",
        width: int = 512,
        height: int = 512,
        version: str = "1.1.1",
    ) -> MapResponse:
        workspace, layer = _name(workspace), _name(layer)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 4096
            for value in (width, height)
        ):
            raise GeoServerError("INVALID_MAP_SIZE", "Map dimensions must be integers between 1 and 4096.")
        try:
            envelope = wms_bbox(bbox, crs, version)
        except VerificationError as error:
            raise GeoServerError(error.code, str(error)) from None
        params = {
            "service": "WMS",
            "version": version,
            "request": "GetMap",
            "layers": f"{workspace}:{layer}",
            "styles": "",
            "format": "image/png",
            "transparent": "true",
            "bgcolor": "0xFFFFFF",
            "width": str(width),
            "height": str(height),
            "bbox": ",".join(format(value, ".17g") for value in envelope),
            "exceptions": "application/vnd.ogc.se_xml" if version == "1.1.1" else "XML",
            "srs" if version == "1.1.1" else "crs": crs.upper(),
        }
        return self._request(
            "GET",
            f"/{workspace}/wms",
            operation="verify WMS map",
            params=params,
            headers={"Accept": "image/png"},
            max_bytes=32 * 1024 * 1024,
        )

    def get_features(self, workspace: str, layer: str, *, limit: int = 5) -> dict:
        workspace, layer = _name(workspace), _name(layer)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise GeoServerError("INVALID_FEATURE_LIMIT", "The WFS sample limit must be between 1 and 100.")
        response = self._request(
            "GET",
            f"/{workspace}/ows",
            operation="verify WFS features",
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": f"{workspace}:{layer}",
                "count": str(limit),
                "outputFormat": "application/json",
                "srsName": "EPSG:4326",
            },
        )
        data = self._json(response)
        if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
            raise GeoServerError("INVALID_WFS_RESPONSE", "WFS did not return a GeoJSON FeatureCollection.")
        if len(data["features"]) > limit:
            raise GeoServerError(
                "WFS_LIMIT_IGNORED", "WFS returned more features than the requested sample limit."
            )
        return data

    def delete_layer(self, workspace: str, name: str) -> dict:
        response = self._request(
            "DELETE",
            f"/rest/layers/{_name(workspace)}:{_name(name)}",
            operation="delete owned layer",
            params={"recurse": "false"},
        )
        return self._evidence(response, "DELETE", "delete_layer")

    def delete_resource(self, workspace: str, store: str, name: str, kind: str) -> dict:
        stores, resources, _, _, _ = _kind(kind)
        response = self._request(
            "DELETE",
            f"/rest/workspaces/{_name(workspace)}/{stores}/{_name(store)}/{resources}/{_name(name)}",
            operation="delete owned resource",
            params={"recurse": "false"},
        )
        return self._evidence(response, "DELETE", "delete_resource")

    def delete_store(self, workspace: str, name: str, kind: str) -> dict:
        stores, _, _, _, _ = _kind(kind)
        params = {"recurse": "false"}
        if kind == "geotiff":
            params["purge"] = "none"
        response = self._request(
            "DELETE",
            f"/rest/workspaces/{_name(workspace)}/{stores}/{_name(name)}",
            operation="delete owned store",
            params=params,
        )
        return self._evidence(response, "DELETE", "delete_store")

    def delete_style(self, workspace: str, name: str) -> dict:
        response = self._request(
            "DELETE",
            f"/rest/workspaces/{_name(workspace)}/styles/{_name(name)}",
            operation="delete owned style",
            params={"recurse": "false", "purge": "false"},
        )
        return self._evidence(response, "DELETE", "delete_style")
