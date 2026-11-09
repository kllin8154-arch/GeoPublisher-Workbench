"""Only loopback ports, with explicit user-configured server endpoints."""
import pytest

from geopublisher.server_deploy import LinuxTarget, ServerDeployError
from geopublisher.server_forward import LocalLoopback, open_service


class FakeLocal:
    def __init__(self):
        self.target = LinuxTarget(mode="local", root="/srv/geopublisher/example",
                                  geoserver_port=19020,
                                  geoserver_context="/gis/geoserver",
                                  postgres_port=15432)


def test_local_service_forward_is_explicit_and_never_exposes_lan():
    with open_service(FakeLocal(), service="geoserver") as server:
        assert server.geoserver_url == "http://127.0.0.1:19020/gis/geoserver"
        assert server.port == 19020
    with open_service(FakeLocal(), service="postgres") as database:
        assert database.port == 15432
        with pytest.raises(ServerDeployError):
            _ = database.geoserver_url


def test_forwards_reject_arbitrary_services():
    with pytest.raises(ServerDeployError) as exc:
        LocalLoopback(target=FakeLocal().target, service="0.0.0.0")
    assert exc.value.code == "FORWARD_SERVICE_NOT_ALLOWED"


@pytest.mark.parametrize("invalid", [0, -1, 65536, True, 8080.5])
def test_invalid_geoserver_port_blocked(invalid):
    with pytest.raises(ServerDeployError) as exc:
        LinuxTarget(mode="local", root="/srv/geopublisher/example", geoserver_port=invalid)
    assert exc.value.code == "INVALID_SERVICE_PORT"


def test_no_tls_bypass_and_no_hostnames():
    profile = FakeLocal().target
    assert profile.geoserver_context.startswith("/")
    with pytest.raises(ServerDeployError):
        LinuxTarget(mode="local", root="/srv/geopublisher/example",
                    geoserver_context="//host/other")
