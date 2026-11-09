"""LAN-only Linux delivery: pinned-host SSH/SFTP, or in-place local Linux.

This adapter transfers an inspected tar package to an *exclusive* new path.
It does not execute scripts from an archive, modify GeoServer, or access a
database. Extraction and REST deployment are separate, explicitly authorized
steps. Remote shell commands are fixed operations with POSIX-quoted paths.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import posixpath
import re
import shlex
import socket
import stat
import subprocess
import sys
import tarfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Protocol

import paramiko


class ServerDeployError(RuntimeError):
    def __init__(self, code: str, message: str, *, outcome_unknown: bool = False):
        self.code = code
        self.outcome_unknown = outcome_unknown
        super().__init__(message)


def _fail(code: str, message: str, *, unknown: bool = False) -> None:
    raise ServerDeployError(code, message, outcome_unknown=unknown)


def _private_ip(host: str) -> str:
    if not isinstance(host, str) or any(char.isspace() for char in host):
        _fail("INVALID_HOST", "服务器地址必须是局域网 IP（不接受域名或公网 IP）。")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        _fail("INVALID_HOST", "请输入明确的局域网 IPv4/IPv6 地址，以免 DNS 错配服务器。")
    blocks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("::1/128"),
    )
    if not any(addr in subnet for subnet in blocks):
        _fail("PUBLIC_HOST_BLOCKED", "仅允许内网私有地址或 loopback；严禁上传地质数据至公网主机。")
    return str(addr)


def _local_regular(path: Path | str, *, suffix: str | None = None) -> Path:
    raw = str(path)
    if raw.startswith(("\\\\", "//")):
        _fail("REMOTE_SOURCE_BLOCKED", "只能使用本机磁盘上的普通文件，不接受网络共享路径。")
    p = Path(path).expanduser().absolute()
    if p.is_symlink() or any(part.is_symlink() for part in p.parents):
        _fail("SYMLINK_BLOCKED", "不允许符号链接形式的本地输入文件。")
    if not p.is_file() or (suffix is not None and p.suffix.lower() != suffix):
        _fail("INVALID_LOCAL_FILE", "文件不存在或扩展名不符合要求。")
    return p


def _folder(root: str) -> str:
    if not isinstance(root, str) or not root.startswith("/") or len(root) > 220:
        _fail("INVALID_REMOTE_ROOT", "Linux 服务器路径必须是已授权的绝对目录。")
    p = PurePosixPath(root)
    if any(part in ("..", ".", "") for part in root.split("/")[1:]):
        _fail("INVALID_REMOTE_ROOT", "服务器路径不能包含 .、.. 或重复分隔符。")
    clean = str(p)
    if clean != root or clean in ("/", "/tmp", "/var", "/home", "/root"):
        _fail("INVALID_REMOTE_ROOT", "不能将服务器系统目录当成数据发布根目录。")
    if not any(root.startswith(prefix + "/") for prefix in ("/zbcx", "/srv", "/opt", "/data", "/mnt")):
        _fail("INVALID_REMOTE_ROOT", "请在 /zbcx、/srv、/opt、/data 或 /mnt 下使用授权发布目录。")
    if "\x00" in root or any(char in root for char in "\r\n"):
        _fail("INVALID_REMOTE_ROOT", "服务器路径包含非法字符。")
    return root


@dataclass(frozen=True)
class LinuxTarget:
    mode: str
    root: str
    host: str = ""
    port: int = 22
    username: str = ""
    auth: str = "key"  # key, password, agent; secret never saved
    known_hosts: str = ""
    key_file: str = ""
    geoserver_port: int = 8080
    postgres_port: int = 5432
    geoserver_context: str = "/geoserver"

    def __post_init__(self) -> None:
        if self.mode not in ("ssh", "local"):
            _fail("INVALID_MODE", "支持 SSH 连接 Linux 或 Linux 本机运行两种方式。")
        _folder(self.root)
        for name, port in (("GeoServer", self.geoserver_port), ("PostgreSQL", self.postgres_port)):
            if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
                _fail("INVALID_SERVICE_PORT", f"{name} 端口必须是 1–65535 的整数。")
        if not re.fullmatch(r"/[A-Za-z][A-Za-z0-9/_-]{0,80}", self.geoserver_context):
            _fail("INVALID_GEOWEB_CONTEXT", "GeoServer 上下文路径不安全。")
        if self.mode == "ssh":
            _private_ip(self.host)
            if isinstance(self.port, bool) or not isinstance(self.port, int) or not (1 <= self.port <= 65535):
                _fail("INVALID_SSH_PORT", "SSH 端口必须是 1–65535 的整数。")
            if not isinstance(self.username, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", self.username):
                _fail("INVALID_SSH_USER", "SSH 用户名格式无效。")
            if self.auth not in ("key", "password", "agent"):
                _fail("INVALID_AUTH_MODE", "仅支持密码、SSH 密钥文件或 SSH Agent。")
            _local_regular(self.known_hosts)
            if self.auth == "key":
                _local_regular(self.key_file)

    @classmethod
    def load(cls, path: Path | str) -> LinuxTarget:
        file = _local_regular(path, suffix=".json")
        if file.stat().st_size > 65_536:
            _fail("INVALID_PROFILE", "服务器连接配置文件过大。")
        try:
            raw = json.loads(file.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise TypeError()
            allowed = set(cls.__dataclass_fields__)
            if set(raw) - allowed or set(raw) & {"password", "passphrase", "token"}:
                raise ValueError()
            return cls(**raw)
        except (OSError, TypeError, ValueError) as exc:
            if isinstance(exc, ServerDeployError):
                raise
            _fail("INVALID_PROFILE", "服务器连接配置有误；不能包含密码或未知设置。")

    def safe_info(self) -> dict:
        return {
            "mode": self.mode,
            "host": self.host if self.mode == "ssh" else "localhost",
            "port": self.port if self.mode == "ssh" else None,
            "username": self.username if self.mode == "ssh" else None,
            "root": self.root,
            "geoserver_port": self.geoserver_port,
            "postgres_port": self.postgres_port,
            "geoserver_context": self.geoserver_context,
            "authentication": self.auth if self.mode == "ssh" else "local",
        }


def _digest(path: Path) -> str:
    hash_ = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hash_.update(block)
    return hash_.hexdigest()


@dataclass(frozen=True)
class ReleaseArchive:
    path: Path
    name: str
    size: int
    sha256: str
    content_names: frozenset[str]
    manifest_count: int

    @classmethod
    def inspect(cls, path: Path | str) -> ReleaseArchive:
        file = _local_regular(path, suffix=".tar")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,100}", file.stem):
            _fail("INVALID_RELEASE_NAME", "发布包文件名只能使用安全的 ASCII 名称。")
        size = file.stat().st_size
        if not 512 <= size <= 80 * 1024**3:
            _fail("INVALID_RELEASE_SIZE", "tar 发布包为空或超过 80 GB 限制。")
        names: set[str] = set()
        manifest = None
        try:
            with tarfile.open(file, mode="r:") as archive:
                for count, member in enumerate(archive):
                    if count >= 12000:
                        _fail("TOO_MANY_ARCHIVE_ITEMS", "tar 包含过多条目。")
                    name = member.name
                    parts = name.split("/")
                    if (
                        name.startswith("/")
                        or "\\" in name
                        or any(part in ("", ".", "..") for part in parts)
                        or parts[0] != file.stem
                        or not (member.isfile() or member.isdir())
                        or name in names
                    ):
                        _fail("UNSAFE_ARCHIVE", "tar 结构含重复、路径穿越或链接，拒绝传输。")
                    names.add(name)
                    if member.isfile() and (member.size < 0 or member.size > 80 * 1024**3):
                        _fail("UNSAFE_ARCHIVE", "文件大小超出范围。")
                    if name == f"{file.stem}/data/SHA256SUMS.txt":
                        if not member.isfile() or member.size > 256 * 1024:
                            _fail("INVALID_ARCHIVE_MANIFEST", "SHA256SUMS 文件无效。")
                        fd = archive.extractfile(member)
                        manifest = fd.read().decode("ascii") if fd else None
        except (tarfile.TarError, EOFError, UnicodeError, OSError) as exc:
            raise ServerDeployError("INVALID_RELEASE_ARCHIVE", "tar 发布包不能安全读取。") from exc
        if manifest is None or f"{file.stem}/README.md" not in names:
            _fail("INVALID_RELEASE_ARCHIVE", "发布包缺少 README 或数据 SHA256SUMS 清单。")
        lines = [line for line in manifest.splitlines() if line]
        if not 1 <= len(lines) <= 12000:
            _fail("INVALID_ARCHIVE_MANIFEST", "数据文件校验清单为空或过大。")
        for line in lines:
            if (not re.fullmatch(r"[0-9a-fA-F]{64}  [^\r\n]+", line)
                or any(ord(char) < 32 for char in line)):
                _fail("INVALID_ARCHIVE_MANIFEST", "数据 SHA256SUMS 清单格式有误。")
            relative = line.split("  ", 1)[1]
            if (relative.startswith(("/", "\\")) or "\\" in relative
                    or any(part in ("", ".", "..") for part in relative.split("/"))
                    or f"{file.stem}/data/{relative}" not in names):
                _fail("INVALID_ARCHIVE_MANIFEST", "清单中的数据文件不在归档中。")
        return cls(file, file.stem, size, _digest(file), frozenset(names), len(lines))


def _quote(value: str) -> str:
    # Shell variable injection is never accepted; only fixed commands with quoted data.
    return shlex.quote(value)


class UnixSession(Protocol):
    def run(self, operation: str, *args: str, timeout: int = 90) -> str: ...
    def exists(self, path: str) -> bool: ...
    def kind(self, path: str) -> str: ...
    def upload_exclusive(self, source: Path, dest: str, report: Callable[[int, int], None] | None) -> None: ...
    def close(self) -> None: ...


_COMMANDS = {
    "os": lambda a: "uname -s",
    "preflight": lambda a: (
        f"test -d {_quote(a[0])} && test -w {_quote(a[0])} && "
        "command -v tar >/dev/null && command -v sha256sum >/dev/null && "
        "command -v python3 >/dev/null && uname -s"
    ),
    "digest": lambda a: f"sha256sum -- {_quote(a[0])}",
    "link": lambda a: f"ln -- {_quote(a[0])} {_quote(a[1])}",
    "create": lambda a: f"mkdir -- {_quote(a[0])}",
    "extract": lambda a: (
        f"tar --extract --file {_quote(a[0])} --directory {_quote(a[1])} "
        "--no-same-owner --no-same-permissions"
    ),
    "verify": lambda a: (
        f"cd -- {_quote(a[0])} && sha256sum --check --status -- SHA256SUMS.txt && "
        "printf 'VERIFIED_FILES=%s\\n' \"$(wc -l < SHA256SUMS.txt)\""
    ),
    "java": lambda a: "java -version 2>&1 | head -3",
    "disk": lambda a: f"df -Pk -- {_quote(a[0])} | tail -1",
}


class SSHSession:
    """Pinned known_hosts SSH with explicit, ephemeral auth and narrow commands."""

    def __init__(
        self, target: LinuxTarget, *, password: str | None = None,
        passphrase: str | None = None, client=None,
    ):
        if target.mode != "ssh":
            _fail("INVALID_MODE", "SSH 会话必须使用 SSH 类型的连接配置。")
        if target.auth == "password" and not password:
            _fail("SSH_PASSWORD_REQUIRED", "请输入 SSH 登录密码，仅用于本次连接。")
        self.target = target
        self._client = client or paramiko.SSHClient()
        try:
            self._client.load_host_keys(target.known_hosts)
            self._client.set_missing_host_key_policy(paramiko.RejectPolicy())
            ident = target.host if target.port == 22 else f"[{target.host}]:{target.port}"
            if not self._client.get_host_keys().lookup(ident):
                _fail("UNKNOWN_SSH_HOST", "服务器指纹尚未登记；请先通过独立渠道确认主机公钥，再导入 known_hosts。")
            opts = {
                "hostname": target.host,
                "port": target.port,
                "username": target.username,
                "timeout": 12,
                "auth_timeout": 15,
                "banner_timeout": 12,
                "allow_agent": target.auth == "agent",
                "look_for_keys": False,
                "password": password if target.auth == "password" else None,
            }
            if target.auth == "key":
                opts["key_filename"] = target.key_file
                opts["passphrase"] = passphrase
            self._client.connect(**opts)
        except ServerDeployError:
            self._client.close()
            raise
        except (paramiko.SSHException, OSError, EOFError, ValueError) as exc:
            self._client.close()
            raise ServerDeployError("SSH_CONNECT_FAILED", "SSH 连接、认证或主机指纹校验失败。") from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SSHSession:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @property
    def transport(self):
        result = self._client.get_transport()
        if result is None or not result.is_active():
            _fail("SSH_DISCONNECTED", "SSH 会话已断开。")
        return result

    def run(self, operation: str, *args: str, timeout: int = 90) -> str:
        if operation not in _COMMANDS:
            _fail("COMMAND_NOT_ALLOWED", "不允许执行工作台以外的自定义服务器命令。")
        command = _COMMANDS[operation](args)
        try:
            _stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout, get_pty=False)
            data = stdout.read(1024 * 1024 + 1)
            error = stderr.read(16384)
            result = stdout.channel.recv_exit_status()
        except (paramiko.SSHException, socket.timeout, EOFError, OSError) as exc:
            raise ServerDeployError("REMOTE_EXEC_UNKNOWN", "SSH 命令中断；如涉及写入必须先核对服务器状态。",
                                    outcome_unknown=operation in {"link", "create", "extract"}) from exc
        if result != 0:
            _fail("REMOTE_COMMAND_FAILED", f"服务器步骤 {operation} 未通过（退出码 {result}）；请核对服务器权限和日志。",
                  unknown=operation in {"link", "create", "extract"})
        if len(data) > 1024 * 1024 or len(error) > 16384:
            _fail("REMOTE_OUTPUT_TOO_LARGE", "服务器返回异常多的内容。")
        return data.decode("utf-8", errors="replace").strip()

    def assert_root_safe(self) -> None:
        try:
            with self._client.open_sftp() as sftp:
                components = self.target.root.split("/")[1:]
                current = ""
                for component in components:
                    current += "/" + component
                    info = sftp.lstat(current)
                    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        _fail("UNSAFE_REMOTE_DIRECTORY", "服务器发布根目录或任一上级目录存在链接/非目录。")
        except ServerDeployError:
            raise
        except (OSError, paramiko.SSHException) as exc:
            raise ServerDeployError("REMOTE_DIRECTORY_NOT_VERIFIED", "无法核验远端目录及其上级目录。") from exc

    def exists(self, path: str) -> bool:
        try:
            with self._client.open_sftp() as sftp:
                sftp.lstat(path)
            return True
        except FileNotFoundError:
            return False
        except (OSError, paramiko.SSHException):
            _fail("REMOTE_STAT_FAILED", "无法安全确认目标路径是否存在；不执行覆盖操作。")

    def kind(self, path: str) -> str:
        try:
            with self._client.open_sftp() as sftp:
                mode = sftp.lstat(path).st_mode
        except FileNotFoundError:
            return "missing"
        except (OSError, paramiko.SSHException) as exc:
            raise ServerDeployError("REMOTE_STAT_FAILED", "不能安全核对服务器数据路径类型。") from exc
        if stat.S_ISLNK(mode):
            return "symlink"
        if stat.S_ISDIR(mode):
            return "dir"
        if stat.S_ISREG(mode):
            return "file"
        return "other"

    def upload_exclusive(self, source: Path, dest: str, report=None) -> None:
        # Exclusive SFTP 'x' must not silently truncate a remote file.
        try:
            with self._client.open_sftp() as sftp:
                with source.open("rb") as reader, sftp.open(dest, "x") as writer:
                    writer.set_pipelined(True)
                    sent = 0
                    while block := reader.read(1024 * 1024):
                        writer.write(block)
                        sent += len(block)
                        if report:
                            report(sent, source.stat().st_size)
        except (OSError, paramiko.SSHException, EOFError) as exc:
            raise ServerDeployError(
                "TRANSFER_UNKNOWN",
                "SFTP 传输中断或服务器拒绝创建；远端可能保留部分文件，请检查后再操作，不会自动覆盖。",
                outcome_unknown=True,
            ) from exc

    def unlink_owned_temp(self, path: str) -> None:
        try:
            with self._client.open_sftp() as sftp:
                sftp.remove(path)
        except (OSError, paramiko.SSHException) as exc:
            raise ServerDeployError("STAGING_CLEANUP_UNKNOWN", "已验证文件但临时文件清理失败，请手工核对。",
                                    outcome_unknown=True) from exc


class LocalLinuxSession:
    """Linux-native equivalent, for a workbench running directly on a Linux host."""

    def __init__(self, target: LinuxTarget):
        if target.mode != "local" or sys.platform != "linux":
            _fail("LOCAL_LINUX_ONLY", "本机运行方式只能在 Linux 上使用。")
        self.target = target

    def close(self) -> None:
        pass

    def __enter__(self) -> LocalLinuxSession:
        return self

    def __exit__(self, *_args) -> None:
        pass

    def run(self, operation: str, *args: str, timeout: int = 90) -> str:
        if operation not in _COMMANDS:
            _fail("COMMAND_NOT_ALLOWED", "不允许任意操作系统命令。")
        # Values are quoted for controlled shell with a fixed command factory;
        # no caller-provided command text and no credentials in these commands.
        try:
            result = subprocess.run(
                ["/bin/sh", "-c", _COMMANDS[operation](args)],
                capture_output=True, timeout=timeout, text=True, errors="replace",
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServerDeployError("LOCAL_EXEC_UNKNOWN", "本机服务器操作未完成；须检查实际目录状态。",
                                    outcome_unknown=operation in {"create", "link", "extract"}) from exc
        if result.returncode != 0:
            _fail("REMOTE_COMMAND_FAILED", f"Linux 本机步骤 {operation} 失败（退出码 {result.returncode}）。",
                  unknown=operation in {"create", "link", "extract"})
        return result.stdout.strip()[:1024 * 1024]

    def assert_root_safe(self) -> None:
        current = Path("/")
        for part in PurePosixPath(self.target.root).parts[1:]:
            current = current / part
            if not current.is_dir() or current.is_symlink():
                _fail("UNSAFE_REMOTE_DIRECTORY", "Linux 本地发布目录或上级目录不是普通目录。")

    def exists(self, path: str) -> bool:
        return Path(path).exists() or Path(path).is_symlink()

    def kind(self, path: str) -> str:
        file = Path(path)
        if file.is_symlink():
            return "symlink"
        if file.is_dir():
            return "dir"
        if file.is_file():
            return "file"
        if file.exists():
            return "other"
        return "missing"

    def upload_exclusive(self, source: Path, dest: str, report=None) -> None:
        try:
            with source.open("rb") as reader, Path(dest).open("xb") as writer:
                sent = 0
                while block := reader.read(1024 * 1024):
                    writer.write(block)
                    sent += len(block)
                    if report:
                        report(sent, source.stat().st_size)
                writer.flush()
                os.fsync(writer.fileno())
        except OSError as exc:
            raise ServerDeployError("TRANSFER_UNKNOWN", "本机数据复制中断；不会覆盖已有文件。",
                                    outcome_unknown=True) from exc

    def unlink_owned_temp(self, path: str) -> None:
        Path(path).unlink()


@contextmanager
def open_target(
    target: LinuxTarget, *, password: str | None = None, passphrase: str | None = None,
) -> Iterator[SSHSession | LocalLinuxSession]:
    if target.mode == "ssh":
        with SSHSession(target, password=password, passphrase=passphrase) as session:
            yield session
    else:
        with LocalLinuxSession(target) as session:
            yield session


def preflight(target: LinuxTarget, session: UnixSession) -> dict:
    if session.run("os") != "Linux":
        _fail("NOT_LINUX", "目标服务器不是 Linux，拒绝执行。")
    session.assert_root_safe()
    if session.run("preflight", target.root) != "Linux":
        _fail("INVALID_SERVER_ROOT", "服务器目录不可写或缺少 tar / sha256sum / python3。")
    # Device type and symlink inspection: lstat via SFTP in SSH mode,
    # but SFTP does not permit arbitrary shell scripts.
    try:
        java_path = session.run("java")
        warnings = ["PATH_JAVA_IS_NOT_PROOF_OF_GEOSERVER_RUNTIME"]
    except ServerDeployError as exc:
        if exc.code != "REMOTE_COMMAND_FAILED":
            raise
        java_path = "NOT_AVAILABLE_ON_PATH"
        warnings = ["JAVA_NOT_ON_PATH", "GEOSERVER_RUNTIME_JVM_NOT_VERIFIED"]
    return {
        "status": "READ_ONLY_PREFLIGHT_OK",
        "target": target.safe_info(),
        "java_on_path": java_path,
        "actual_geoserver_jvm": "UNVERIFIED",
        "warnings": warnings,
        "disk": session.run("disk", target.root),
        "database_accessed": False,
        "geoserver_mutated": False,
    }


def stage_archive(
    target: LinuxTarget, archive: ReleaseArchive, session: UnixSession,
    *, progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Transfer exactly one verified tar, never overwrite; leave explicit evidence."""
    review = preflight(target, session)
    try:
        disk_fields = review["disk"].split()
        available_bytes = int(disk_fields[3]) * 1024
    except (IndexError, TypeError, ValueError) as exc:
        raise ServerDeployError("DISK_SPACE_UNVERIFIED", "服务器可用空间无法可靠读取。") from exc
    required = archive.size * 3 + 512 * 1024 * 1024
    if available_bytes < required:
        _fail("DISK_SPACE_TOO_LOW", "服务器空间不足，无法安全完成上传、解包、索引及临时文件。")
    final = posixpath.join(target.root, archive.path.name)
    destination = posixpath.join(target.root, archive.name)
    if session.exists(final) or session.exists(destination):
        _fail("RELEASE_ALREADY_EXISTS", "同名发布归档或目录已经存在。请核查现状，不自动覆盖或重试。")
    temp = posixpath.join(target.root, f".{archive.name}.{uuid.uuid4().hex}.partial")
    if session.exists(temp):
        _fail("TRANSFER_COLLISION", "传输临时文件冲突。")
    session.upload_exclusive(archive.path, temp, progress)
    remote = session.run("digest", temp, timeout=3600).split()[0].lower()
    if remote != archive.sha256:
        _fail("TRANSFER_HASH_MISMATCH", "服务器 tar SHA256 与本机不一致，远端文件保留供排查。", unknown=True)
    if session.exists(final) or session.exists(destination):
        _fail("RELEASE_ALREADY_EXISTS", "传输完成后发现同名发布对象，停止创建最终归档。")
    session.run("link", temp, final)
    session.unlink_owned_temp(temp)
    return {
        "status": "STAGED_VERIFIED_NOT_EXTRACTED",
        "target": target.safe_info(), "release": archive.name,
        "remote_archive": final, "remote_directory": destination,
        "bytes": archive.size, "sha256": archive.sha256,
        "geoserver_mutated": False, "database_accessed": False,
    }


def extract_staged(
    target: LinuxTarget, archive: ReleaseArchive, session: UnixSession,
) -> dict:
    """Explicit second step; never execute packaged scripts or SQL."""
    preflight(target, session)
    remote_archive = posixpath.join(target.root, archive.path.name)
    destination = posixpath.join(target.root, archive.name)
    if session.kind(remote_archive) != "file" or session.kind(destination) != "missing":
        _fail("STAGE_NOT_READY", "服务器归档不是普通文件或发布目录已经存在，拒绝解包。")
    remote = session.run("digest", remote_archive, timeout=3600).split()[0].lower()
    if remote != archive.sha256:
        _fail("STAGE_HASH_MISMATCH", "服务器归档与本机 SHA256 不一致，禁止解包。")
    session.run("create", destination)
    try:
        session.run("extract", remote_archive, target.root, timeout=3600)
        checked = session.run("verify", posixpath.join(destination, "data"), timeout=3600)
    except ServerDeployError as exc:
        raise ServerDeployError(
            "EXTRACTION_REVIEW_REQUIRED",
            "服务器解包或 TIFF 校验中断；目录可能部分建立，必须现场核对后决定下一步。",
            outcome_unknown=True,
        ) from exc
    if checked != f"VERIFIED_FILES={archive.manifest_count}":
        _fail("RASTER_CHECKSUM_COUNT_MISMATCH", "服务器文件校验数量不一致；需核对本次发布包。", unknown=True)
    return {
        "status": "EXTRACTED_CHECKSUMS_OK_NOT_PUBLISHED",
        "release": archive.name, "remote_directory": destination,
        "sha256": archive.sha256, "checked_lines": archive.manifest_count,
        "geoserver_mutated": False, "database_accessed": False,
    }
