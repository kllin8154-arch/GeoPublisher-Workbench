"""SSH-forwarded loopback access for GeoServer REST and PostgreSQL.

Only a pinned SSH session can open the forwarding channel. Both ends are
strictly loopback; the server-side target port is configured by a fixed,
explicit target service. No HTTP listener is opened on the user's LAN.
"""
from __future__ import annotations

import select
import socket
import socketserver
import threading
from contextlib import AbstractContextManager

from .server_deploy import ServerDeployError, SSHSession


class _ForwardHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        ssh_transport = server.ssh_session.transport
        try:
            channel = ssh_transport.open_channel(
                "direct-tcpip", ("127.0.0.1", server.target_port),
                ("127.0.0.1", 0), timeout=15,
            )
            if channel is None:
                return
            with channel:
                self.request.settimeout(None)
                while server.active.is_set():
                    readable, _, _ = select.select([self.request, channel], [], [], 1)
                    if self.request in readable:
                        data = self.request.recv(65536)
                        if not data:
                            return
                        channel.sendall(data)
                    if channel in readable:
                        data = channel.recv(65536)
                        if not data:
                            return
                        self.request.sendall(data)
        except (OSError, EOFError, socket.timeout):
            # HTTP client sees a connection failure; publishing controller
            # must reconcile an ambiguous write before attempting another.
            pass


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, ssh_session: SSHSession, target_port: int):
        self.ssh_session = ssh_session
        self.target_port = target_port
        self.active = threading.Event()
        self.active.set()
        super().__init__(("127.0.0.1", 0), _ForwardHandler)


class SSHLoopbackTunnel(AbstractContextManager):
    """Forward a single explicitly authorized service via encrypted SSH."""

    def __init__(self, ssh_session: SSHSession, *, service: str):
        if service not in {"geoserver", "postgres"}:
            raise ServerDeployError("FORWARD_SERVICE_NOT_ALLOWED", "仅允许转发 GeoServer 或 PostgreSQL。")
        self._service = service
        self._ssh_session = ssh_session
        self._server: _ForwardServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self):
        profile = self._ssh_session.target
        port = profile.geoserver_port if self._service == "geoserver" else profile.postgres_port
        self._server = _ForwardServer(self._ssh_session, port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def port(self) -> int:
        if not self._server:
            raise ServerDeployError("TUNNEL_NOT_STARTED", "SSH 隧道尚未建立。")
        return int(self._server.server_address[1])

    @property
    def geoserver_url(self) -> str:
        if self._service != "geoserver":
            raise ServerDeployError("INVALID_TUNNEL_KIND", "这个 SSH 隧道不是 GeoServer 连接。")
        return f"http://127.0.0.1:{self.port}{self._ssh_session.target.geoserver_context}"

    def __exit__(self, *_args):
        if self._server:
            self._server.active.clear()
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        return False


class LocalLoopback:
    """In-place Linux service URL with no tunnel and no external exposure."""

    def __init__(self, *, service: str, target):
        if service not in {"geoserver", "postgres"}:
            raise ServerDeployError("FORWARD_SERVICE_NOT_ALLOWED", "仅允许 GeoServer / PostgreSQL 回环端口。")
        self.service = service
        self.target = target

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    @property
    def geoserver_url(self):
        if self.service != "geoserver":
            raise ServerDeployError("INVALID_TUNNEL_KIND", "此连接不是 GeoServer。")
        return f"http://127.0.0.1:{self.target.geoserver_port}{self.target.geoserver_context}"

    @property
    def port(self):
        return self.target.geoserver_port if self.service == "geoserver" else self.target.postgres_port


def open_service(session, *, service: str):
    if isinstance(session, SSHSession):
        return SSHLoopbackTunnel(session, service=service)
    return LocalLoopback(service=service, target=session.target)
