"""
M1 P0 安全 — 认证与总线接入测试

覆盖:
1. AuthManager 单元测试（未配置零摩擦 / 单 token / 多 token 角色 / 恒时比较 / 环境变量解析）
2. BusEnvelope.subscribe token 序列化兼容
3. BusServer 认证集成（启用后拒绝无 token / 错误 token；正确 token 放行）
4. BusClient token 传递与 SubscribeRejectedError
5. 认证审计事件记录（denied / ok）
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets
from websockets.asyncio.client import connect as ws_connect

from youmi.bus.broker import InProcessBroker
from youmi.bus.message import BusEnvelope
from youmi.bus.server import BusServer
from youmi.bus.ws_client import BusClient, SubscribeRejectedError
from youmi.observability import AuditLogger, configure_audit_logger, reset_audit_logger
from youmi.security import (
    AuthManager,
    AuthRole,
    AuthToken,
    Principal,
    auth_from_env,
    configure_auth_manager,
    get_auth_manager,
    reset_auth_manager,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """隔离认证 / 审计单例与相关环境变量"""
    for var in ("YOUMI_AUTH_TOKEN", "YOUMI_AUTH_ROLE", "YOUMI_AUTH_TOKENS"):
        monkeypatch.delenv(var, raising=False)
    reset_auth_manager()
    reset_audit_logger()
    yield
    reset_auth_manager()
    reset_audit_logger()


# ===========================================================================
# 1. AuthManager 单元测试
# ===========================================================================

class TestAuthManager:
    """token → Principal 校验器"""

    def test_disabled_when_no_tokens(self):
        auth = AuthManager()
        assert auth.enabled is False
        assert auth.token_count == 0

    def test_disabled_passes_anonymous_admin(self):
        """未配置 token → 零摩擦放行（匿名 admin）"""
        auth = AuthManager()
        p = auth.validate("")
        assert p is not None
        assert p.role == AuthRole.ADMIN
        assert p.name == "anonymous"
        # 未启用时任意 token 同样放行
        assert auth.validate("whatever") is not None

    def test_valid_token(self):
        auth = AuthManager([
            AuthToken(token="s3cret", role=AuthRole.AGENT, name="worker"),
        ])
        assert auth.enabled is True
        p = auth.validate("s3cret")
        assert isinstance(p, Principal)
        assert p.role == AuthRole.AGENT
        assert p.name == "worker"

    def test_invalid_token_rejected(self):
        auth = AuthManager([AuthToken(token="s3cret")])
        assert auth.validate("wrong") is None
        assert auth.validate("") is None
        assert auth.validate(None) is None

    def test_multi_tokens_distinct_roles(self):
        auth = AuthManager([
            AuthToken(token="admin-token", role=AuthRole.ADMIN, name="boss"),
            AuthToken(token="agent-token", role=AuthRole.AGENT),
            AuthToken(token="view-token", role=AuthRole.VIEWER),
        ])
        assert auth.token_count == 3
        assert auth.validate("admin-token").role == AuthRole.ADMIN
        assert auth.validate("agent-token").role == AuthRole.AGENT
        assert auth.validate("view-token").role == AuthRole.VIEWER

    def test_fingerprint_hides_raw_token(self):
        auth = AuthManager([AuthToken(token="top-secret-123")])
        p = auth.validate("top-secret-123")
        assert p.token_fingerprint != "top-secret-123"
        assert len(p.token_fingerprint) == 8

    def test_has_role(self):
        admin = Principal(role=AuthRole.ADMIN)
        viewer = Principal(role=AuthRole.VIEWER)
        assert AuthManager.has_role(admin, AuthRole.ADMIN)
        assert not AuthManager.has_role(viewer, AuthRole.ADMIN)
        # 无角色要求 = 仅需已认证
        assert AuthManager.has_role(viewer)
        assert not AuthManager.has_role(None)


# ===========================================================================
# 2. 环境变量解析
# ===========================================================================

class TestAuthFromEnv:
    """auth_from_env 配置解析"""

    def test_no_env_disabled(self):
        assert auth_from_env().enabled is False

    def test_single_token_with_role(self, monkeypatch):
        monkeypatch.setenv("YOUMI_AUTH_TOKEN", "env-token")
        monkeypatch.setenv("YOUMI_AUTH_ROLE", "admin")
        auth = auth_from_env()
        assert auth.enabled
        assert auth.validate("env-token").role == AuthRole.ADMIN

    def test_single_token_default_role(self, monkeypatch):
        monkeypatch.setenv("YOUMI_AUTH_TOKEN", "env-token")
        assert auth_from_env().validate("env-token").role == AuthRole.AGENT

    def test_invalid_role_fallback_to_agent(self, monkeypatch):
        monkeypatch.setenv("YOUMI_AUTH_TOKEN", "env-token")
        monkeypatch.setenv("YOUMI_AUTH_ROLE", "superhacker")
        assert auth_from_env().validate("env-token").role == AuthRole.AGENT

    def test_multi_tokens(self, monkeypatch):
        monkeypatch.setenv("YOUMI_AUTH_TOKENS", "tok1:admin:alice;tok2:viewer")
        auth = auth_from_env()
        assert auth.token_count == 2
        assert auth.validate("tok1").name == "alice"
        assert auth.validate("tok1").role == AuthRole.ADMIN
        assert auth.validate("tok2").role == AuthRole.VIEWER

    def test_multi_merges_single_without_duplicate(self, monkeypatch):
        monkeypatch.setenv("YOUMI_AUTH_TOKENS", "tok1:admin")
        monkeypatch.setenv("YOUMI_AUTH_TOKEN", "tok2")
        assert auth_from_env().token_count == 2

        # 重复 token 只保留一次
        monkeypatch.setenv("YOUMI_AUTH_TOKENS", "tokX:admin")
        monkeypatch.setenv("YOUMI_AUTH_TOKEN", "tokX")
        assert auth_from_env().token_count == 1

    def test_blank_entries_ignored(self, monkeypatch):
        monkeypatch.setenv("YOUMI_AUTH_TOKENS", " ;tok1:admin;;")
        assert auth_from_env().token_count == 1

    def test_singleton_configure_reset(self):
        assert get_auth_manager().enabled is False  # 懒初始化（环境已清空）
        configure_auth_manager(AuthManager([AuthToken(token="x")]))
        assert get_auth_manager().enabled is True
        assert get_auth_manager().validate("x") is not None
        reset_auth_manager()
        assert get_auth_manager().enabled is False


# ===========================================================================
# 3. BusEnvelope.subscribe token 序列化
# ===========================================================================

class TestSubscribeEnvelope:
    """订阅信封 token 字段"""

    def test_token_included(self):
        env = BusEnvelope.subscribe("a1", "wf-1", token="s3cret")
        assert env.envelope_type == "subscribe"
        assert env.payload["workflow_id"] == "wf-1"
        assert env.payload["token"] == "s3cret"

    def test_token_omitted_when_empty(self):
        env = BusEnvelope.subscribe("a1", "wf-1")
        assert "token" not in env.payload

    def test_roundtrip_json(self):
        env = BusEnvelope.subscribe("a1", token="t")
        restored = BusEnvelope(**json.loads(env.model_dump_json()))
        assert restored.payload["token"] == "t"


# ===========================================================================
# 4. BusServer 认证集成（真实 WebSocket）
# ===========================================================================

_AUTH_PORT = 18770
_NOAUTH_PORT = 18771


class TestBusAuthEnabled:
    """启用认证后的总线行为"""

    async def _serve(self, port: int = _AUTH_PORT) -> BusServer:
        configure_auth_manager(AuthManager([
            AuthToken(token="valid-token", role=AuthRole.AGENT, name="worker"),
        ]))
        server = BusServer(InProcessBroker())
        await server.start(host="localhost", port=port)
        return server

    async def test_rejects_missing_token(self):
        server = await self._serve()
        try:
            async with ws_connect(f"ws://localhost:{_AUTH_PORT}") as ws:
                envelope = BusEnvelope.subscribe("intruder", "wf-1")  # 无 token
                await ws.send(envelope.model_dump_json())

                resp = json.loads(await ws.recv())
                assert resp["envelope_type"] == "error"
                assert resp["payload"]["code"] == 4401
                assert "Unauthorized" in resp["payload"]["error"]

                # 服务端随后以 4401 关闭连接
                with pytest.raises(websockets.ConnectionClosed) as exc_info:
                    await asyncio.wait_for(ws.recv(), timeout=5.0)
                assert exc_info.value.rcvd is not None
                assert exc_info.value.rcvd.code == 4401
        finally:
            await server.stop()

    async def test_rejects_wrong_token(self):
        server = await self._serve()
        try:
            async with ws_connect(f"ws://localhost:{_AUTH_PORT}") as ws:
                envelope = BusEnvelope.subscribe("intruder", "wf-1", token="wrong-token")
                await ws.send(envelope.model_dump_json())

                resp = json.loads(await ws.recv())
                assert resp["envelope_type"] == "error"
                assert resp["payload"]["code"] == 4401
        finally:
            await server.stop()

    async def test_valid_token_connects(self):
        server = await self._serve()
        try:
            client = BusClient(
                agent_id="good-agent",
                url=f"ws://localhost:{_AUTH_PORT}",
                token="valid-token",
            )
            await client.connect(workflow_id="wf-1")
            assert client.is_connected

            await asyncio.sleep(0.1)
            assert "good-agent" in server.connected_agents

            await client.disconnect()
        finally:
            await server.stop()

    async def test_bus_client_without_token_raises(self):
        server = await self._serve()
        try:
            client = BusClient(
                agent_id="bad-agent",
                url=f"ws://localhost:{_AUTH_PORT}",
                max_reconnect_attempts=2,
                reconnect_interval=0.1,
            )
            with pytest.raises(SubscribeRejectedError):
                await client.connect(workflow_id="wf-1")
        finally:
            await server.stop()

    async def test_audit_records_auth_events(self):
        """认证成功/失败均写入审计"""
        audit = AuditLogger()
        configure_audit_logger(audit)
        server = await self._serve()
        try:
            # 失败：无 token
            async with ws_connect(f"ws://localhost:{_AUTH_PORT}") as ws:
                envelope = BusEnvelope.subscribe("intruder", "wf-1")
                await ws.send(envelope.model_dump_json())
                await ws.recv()

            # 成功：正确 token
            client = BusClient(
                agent_id="good-agent",
                url=f"ws://localhost:{_AUTH_PORT}",
                token="valid-token",
            )
            await client.connect(workflow_id="wf-1")
            await client.disconnect()

            events = audit.get_recent(event_type="auth")
            statuses = [e.status for e in events]
            assert "denied" in statuses
            assert "ok" in statuses

            denied = next(e for e in events if e.status == "denied")
            assert denied.error
            # 审计不落原始 token
            assert "valid-token" not in denied.model_dump_json()
            ok_event = next(e for e in events if e.status == "ok")
            assert ok_event.detail.get("role") == "agent"
        finally:
            await server.stop()


class TestBusAuthDisabled:
    """未配置认证 → 零摩擦（回归既有行为）"""

    async def test_no_auth_configured_allows_connect(self):
        server = BusServer(InProcessBroker())
        await server.start(host="localhost", port=_NOAUTH_PORT)
        try:
            client = BusClient(
                agent_id="no-auth-agent",
                url=f"ws://localhost:{_NOAUTH_PORT}",
            )
            await client.connect(workflow_id="wf-1")
            assert client.is_connected
            await client.disconnect()
        finally:
            await server.stop()
