"""Explicit environment profiles; credentials are resolved only at execution time."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_name(value: str, label: str = "名称") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,95}", value):
        raise ConfigError(
            "INVALID_NAME",
            f"{label}必须以英文字母或下划线开头，最多 96 位，只含字母、数字、下划线、点和短横线。",
        )
    return value


@dataclass(frozen=True)
class Environment:
    environment_key: str
    base_url: str
    workspace: str
    gdal_bin: str
    gdal_version: str | None = "3.13.3"
    username_env: str = "GEOPUB_USERNAME"
    password_env: str = "GEOPUB_PASSWORD"
    ca_bundle: str | None = None
    allow_http: bool = False
    timeout: float = 120.0
    schema_version: int = 1

    def __post_init__(self):
        if type(self.allow_http) is not bool:
            raise ConfigError("INVALID_PROFILE", "allow_http 必须是 JSON 布尔值。")
        if type(self.schema_version) is not int:
            raise ConfigError("INVALID_PROFILE", "schema_version 必须是整数。")
        if not isinstance(self.base_url, str) or not isinstance(self.gdal_bin, str):
            raise ConfigError("INVALID_PROFILE", "地址和工具路径必须是文本。")
        if self.ca_bundle is not None and not isinstance(self.ca_bundle, str):
            raise ConfigError("INVALID_PROFILE", "证书路径必须是文本。")
        if self.gdal_version is not None and not isinstance(self.gdal_version, str):
            raise ConfigError("INVALID_PROFILE", "GDAL 版本必须是文本或 null。")
        validate_name(self.environment_key, "环境标识")
        validate_name(self.workspace, "工作空间")
        if self.schema_version != 1:
            raise ConfigError("SCHEMA_VERSION", "不支持此环境配置版本。")
        try:
            parts = urlsplit(self.base_url)
            _ = parts.port
        except ValueError as exc:
            raise ConfigError("INVALID_URL", "GeoServer 地址无效。") from exc
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ConfigError(
                "INVALID_URL", "GeoServer 地址必须是 HTTP(S) 根地址，不得携带凭据、查询参数或片段。"
            )
        loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if parts.scheme == "http" and not loopback and not self.allow_http:
            raise ConfigError(
                "HTTP_NOT_ALLOWED",
                "此环境使用未加密 HTTP；请配置 HTTPS，或在已授权内网配置中明确 allow_http。",
            )
        if (
            type(self.timeout) not in (int, float)
            or not math.isfinite(self.timeout)
            or not 1 <= self.timeout <= 3600
        ):
            raise ConfigError("INVALID_TIMEOUT", "超时必须在 1 到 3600 秒之间。")
        for value in (self.username_env, self.password_env):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                raise ConfigError("INVALID_CREDENTIAL_REF", "凭据引用必须是环境变量名。")
        if not self.gdal_bin:
            raise ConfigError("GDAL_REQUIRED", "必须提供受控 GDAL 工具目录。")

    @classmethod
    def load(cls, path: Path | str) -> "Environment":
        path = Path(path).resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ConfigError("INVALID_PROFILE", "环境配置必须是 JSON 对象。")
            for key in ("gdal_bin", "ca_bundle"):
                if data.get(key):
                    item = Path(data[key]).expanduser()
                    data[key] = str(item.resolve() if item.is_absolute() else (path.parent / item).resolve())
            return cls(**data)
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise ConfigError("INVALID_PROFILE", "环境配置无法读取，或包含不支持的字段。") from exc

    def to_dict(self) -> dict:
        result = asdict(self)
        result["base_url"] = self.base_url.rstrip("/")
        return result

    def credentials(self) -> tuple[str, str]:
        username = os.environ.get(self.username_env, "")
        password = os.environ.get(self.password_env, "")
        if not username or not password:
            raise ConfigError(
                "CREDENTIALS_REQUIRED",
                f"请在本次会话设置 {self.username_env} 和 {self.password_env}；配置文件只保存变量名。",
            )
        return username, password

    def client(self):
        from .geoserver import GeoServerClient

        username, password = self.credentials()
        return GeoServerClient(
            self.base_url.rstrip("/"), username, password, timeout=self.timeout, verify=self.ca_bundle or True
        )
