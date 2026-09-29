"""
M1 P0 安全 — GUI 认证中间件 / 健康检查 / 审计端点测试

覆盖:
1. 未配置认证 → 零摩擦放行（/api/*、/ws、/api/audit 均可用）
2. 配置认证 → /api/* 无 token 401 / 错误 token 401 / 正确 token（header 与 ?token=）200
3. /healthz 免认证探活
4. /api/audit 需要 admin 角色（agent 角色 403、admin 200）
5. /ws 握手认证（无 token / 错误 token 被拒；带 token 成功且 hello 携带 auth_enabled）
6. 认证失败写入审计（auth / denied，且不含原始 token）
"""

from __future__ import annotations

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from gui.config import GUIConfig
from gui.server import create_app
from youmi.observability import AuditLogger, configure_audit_logger, reset_audit_logger
from youmi.security import (
    AuthManager,
    AuthRole,
    AuthToken,
    configure_auth_manager,
    reset_auth_manager,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """隔离认证 / 审计单例与环境变量"""
    for var in ("YOUMI_AUTH_TOKEN", "YOUMI_AUTH_ROLE", "YOUMI_AUTH_TOKENS"):
        monkeypatch.delenv(var, raising=False)
    reset_auth_manager()
    reset_audit_logger()
    yield
    reset_auth_manager()
    reset_audit_logger()


def _make_config(tmp_path) -> GUIConfig:
    config = GUIConfig(host="127.0.0.1", port=0)
    # 数据目录重定向到临时目录，避免污染真实 mock 数据
    config.data_dir = str(tmp_path / "gui_data")
    return config


async def _make_client(tmp_path) -> TestClient:
    app = create_app(_make_config(tmp_path), use_mock=True)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _enable_auth() -> None:
    configure_auth_manager(AuthManager([
        AuthToken(token="admin-token", role=AuthRole.ADMIN, name="ops"),
        AuthToken(token="agent-token", role=AuthRole.AGENT, name="worker"),
    ]))


# ===========================================================================
# 1. 未配置认证 → 零摩擦
# ===========================================================================

class TestAuthDisabled:
    async def test_api_open_without_auth(self, tmp_path):
        client = await _make_client(tmp_path)
        try:
            resp = await client.get("/api/agents")
            assert resp.status == 200
            resp = await client.get("/api/sessions")
            assert resp.status == 200
        finally:
            await client.close()

    async def test_audit_open_without_auth(self, tmp_path):
        client = await _make_client(tmp_path)
        try:
            resp = await client.get("/api/audit")
            assert resp.status == 200
            data = await resp.json()
            assert "events" in data
        finally:
            await client.close()

    async def test_ws_connect_without_token(self, tmp_path):
        client = await _make_client(tmp_path)
        try:
            ws = await client.ws_connect("/ws")
            msg = await ws.receive_json(timeout=5)
            assert msg["type"] == "hello"
            assert msg["auth_enabled"] is False
            await ws.close()
        finally:
            await client.close()


# ===========================================================================
# 2. /healthz 健康检查
# ===========================================================================

class TestHealthz:
    async def test_healthz_without_auth(self, tmp_path):
        client = await _make_client(tmp_path)
        try:
            resp = await client.get("/healthz")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["auth_enabled"] is False
            assert "engine_ready" in data
            assert "connected_clients" in data
        finally:
            await client.close()

    async def test_healthz_stays_open_with_auth(self, tmp_path):
        """启用认证后 /healthz 仍免认证（探活）"""
        _enable_auth()
        client = await _make_client(tmp_path)
        try:
            resp = await client.get("/healthz")
            assert resp.status == 200
            data = await resp.json()
            assert data["auth_enabled"] is True
        finally:
            await client.close()


# ===========================================================================
# 3. 启用认证 → REST / WS 保护
# ===========================================================================

class TestAuthEnabled:
    async def test_api_rejects_missing_or_wrong_token(self, tmp_path):
        _enable_auth()
        client = await _make_client(tmp_path)
        try:
            resp = await client.get("/api/agents")
            assert resp.status == 401

            resp = await client.get(
                "/api/agents", headers={"Authorization": "Bearer wrong-token"}
            )
            assert resp.status == 401

            # 静态页面与首页不受中间件影响
            resp = await client.get("/")
            assert resp.status == 200
        finally:
            await client.close()

    async def test_api_accepts_valid_token(self, tmp_path):
        _enable_auth()
        client = await _make_client(tmp_path)
        try:
            # Authorization header 路径
            resp = await client.get(
                "/api/agents", headers={"Authorization": "Bearer agent-token"}
            )
            assert resp.status == 200

            # ?token= 兼容路径（浏览器场景）
            resp = await client.get("/api/sessions?token=agent-token")
            assert resp.status == 200
        finally:
            await client.close()

    async def test_ws_handshake_rejected(self, tmp_path):
        _enable_auth()
        client = await _make_client(tmp_path)
        try:
            with pytest.raises(WSServerHandshakeError):
                await client.ws_connect("/ws")
            with pytest.raises(WSServerHandshakeError):
                await client.ws_connect("/ws?token=wrong-token")
        finally:
            await client.close()

    async def test_ws_connect_with_token(self, tmp_path):
        _enable_auth()
        client = await _make_client(tmp_path)
        try:
            ws = await client.ws_connect("/ws?token=agent-token")
            msg = await ws.receive_json(timeout=5)
            assert msg["type"] == "hello"
            assert msg["auth_enabled"] is True
            await ws.close()
        finally:
            await client.close()


# ===========================================================================
# 4. 审计端点（admin 角色）
# ===========================================================================

class TestAuditEndpoint:
    async def test_audit_requires_admin_role(self, tmp_path):
        _enable_auth()
        client = await _make_client(tmp_path)
        try:
            # agent 角色 → 403
            resp = await client.get(
                "/api/audit", headers={"Authorization": "Bearer agent-token"}
            )
            assert resp.status == 403

            # admin → 200
            resp = await client.get(
                "/api/audit", headers={"Authorization": "Bearer admin-token"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert "events" in data and "count" in data
        finally:
            await client.close()

    async def test_audit_event_type_filter(self, tmp_path):
        audit = AuditLogger()
        configure_audit_logger(audit)
        audit.log("tool_call", tool_name="file_read", status="ok")
        audit.log("llm_call", status="ok")

        client = await _make_client(tmp_path)
        try:
            resp = await client.get("/api/audit?event_type=tool_call")
            assert resp.status == 200
            data = await resp.json()
            assert data["count"] == 1
            assert data["events"][0]["tool_name"] == "file_read"
        finally:
            await client.close()

    async def test_denied_attempt_audited_without_raw_token(self, tmp_path):
        audit = AuditLogger()
        configure_audit_logger(audit)
        _enable_auth()
        client = await _make_client(tmp_path)
        try:
            resp = await client.get(
                "/api/agents", headers={"Authorization": "Bearer super-secret-wrong"}
            )
            assert resp.status == 401

            events = audit.get_recent(event_type="auth")
            assert any(e.status == "denied" for e in events)
            for e in events:
                payload = e.model_dump_json()
                assert "super-secret-wrong" not in payload
                assert "admin-token" not in payload
                assert "agent-token" not in payload
        finally:
            await client.close()
