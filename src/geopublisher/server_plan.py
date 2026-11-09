"""Publish an immutable existing SHP/GeoTIFF plan over an explicit SSH tunnel.

This reuses the main GeoPublisher journaling, WMS/WFS and style adapters.
It never swaps an immutable plan's server target silently. The plan must
already specify the exact authorized Linux host and management port.
"""
from __future__ import annotations

from pathlib import Path

from .engine import run_plan
from .geoserver import GeoServerClient
from .plans import load_plan, verify_artifacts
from .server_deploy import LinuxTarget, ServerDeployError, UnixSession
from .server_forward import open_service


def validate_server_binding(plan: dict, target: LinuxTarget) -> None:
    env = plan["environment"]
    hostname = target.host if target.mode == "ssh" else "127.0.0.1"
    expected = f"http://{hostname}:{target.geoserver_port}{target.geoserver_context}"
    if env["base_url"].rstrip("/") != expected:
        raise ServerDeployError(
            "PLAN_SERVER_MISMATCH",
            "原任务 GeoServer 地址与当前 SSH/Linux 目标不一致。"
            "请为正确的服务器重新生成不可变计划，不能悄悄修改目标地址。",
        )


def publish_plan_via_tunnel(
    target: LinuxTarget,
    plan_path: str | Path,
    session: UnixSession,
    *,
    username: str,
    password: str,
    journal_path: Path | str | None = None,
    on_event=None,
    cancel_event=None,
) -> dict:
    """Publish after explicit user authorization; never executes a database SQL."""
    from .server_deploy import _local_regular

    source = _local_regular(plan_path, suffix=".json")
    if source.name != "plan.json":
        raise ServerDeployError("PLAN_FILE_REQUIRED", "仅接受经过不可变哈希验证的 plan.json。")
    plan = load_plan(source)
    validate_server_binding(plan, target)
    # Fail before opening a REST port if input data has changed or is missing.
    verify_artifacts(plan)
    if session.run("os") != "Linux":
        raise ServerDeployError("NOT_LINUX", "目标 SSH 服务器不是 Linux。")
    if not username or not password:
        raise ServerDeployError("GEOSERVER_AUTH_REQUIRED", "请提供已有 GeoServer REST 用户和会话密码。")
    with open_service(session, service="geoserver") as port:
        with GeoServerClient(port.geoserver_url, username, password, timeout=180) as client:
            result = run_plan(
                plan, journal_path=journal_path, client=client,
                on_event=on_event, cancel_event=cancel_event,
            )
    result["transport"] = "SSH_LOOPBACK" if target.mode == "ssh" else "LINUX_LOOPBACK"
    result["business_sql_executed"] = False
    return result
