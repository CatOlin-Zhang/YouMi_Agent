"""多租户传播测试 (tenant03)

测试覆盖:
1. 审计租户 — AuditEvent.tenant / log 写入 / get_recent 过滤 / 快捷方法
2. 总线消息租户 — WorkflowMessage.tenant / Broker 跨租户阻断 / 广播过滤 / 向后兼容
3. BusServer 租户强制 — 发布消息归属连接认证租户 / 清理
4. GUI 会话租户 — Session 模型兼容 / EngineBridge 跨租户防护 / WS Hub 广播隔离
5. GUI 端点集成 — 会话与审计按 token 租户隔离
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gui.config import GUIConfig
from gui.engine.bridge import EngineBridge
from gui.engine.models import Session
from gui.hub.ws_hub import WebSocketHub
from gui.persistence.store import Store
from gui.server import create_app
from youmi.bus.broker import InProcessBroker
from youmi.bus.message import BusEnvelope, WorkflowMessage, WorkflowMessageType
from youmi.bus.server import BusServer
from youmi.observability import (
    AuditLogger,
    configure_audit_logger,
    reset_audit_logger,
)
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
    for var in (
        "YOUMI_AUTH_TOKEN", "YOUMI_AUTH_ROLE", "YOUMI_AUTH_TOKENS",
        "YOUMI_AUTH_TENANT",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_auth_manager()
    reset_audit_logger()
    yield
    reset_auth_manager()
    reset_audit_logger()


class FakeWS:
    """假 WebSocket 连接 — 记录发送内容"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


# =========================================================================
# 测试1: 审计租户
# =========================================================================

class TestAuditTenant:

    def test_event_default_tenant(self):
        audit = AuditLogger()
        event = audit.log("probe", agent_id="a1")
        assert event is not None
        assert event.tenant == "default"

    def test_explicit_tenant_and_filter(self):
        audit = AuditLogger()
        audit.log("probe", agent_id="a1")
        audit.log("probe", agent_id="a2", tenant="t1")

        all_events = audit.get_recent()
        assert len(all_events) == 2

        t1_events = audit.get_recent(tenant="t1")
        assert len(t1_events) == 1
        assert t1_events[0].agent_id == "a2"

        default_events = audit.get_recent(tenant="default")
        assert len(default_events) == 1
        assert default_events[0].agent_id == "a1"

        # 无该租户事件
        assert audit.get_recent(tenant="ghost") == []

    def test_helper_methods_carry_tenant(self):
        audit = AuditLogger()
        audit.log_tool_call("tool_a", tenant="t1")
        audit.log_llm_call("qwen", tenant="t2")

        assert audit.get_recent(tenant="t1")[0].tool_name == "tool_a"
        llm_events = audit.get_recent(tenant="t2")
        assert len(llm_events) == 1
        assert llm_events[0].event_type == "llm_call"

    def test_event_type_and_tenant_combined_filter(self):
        audit = AuditLogger()
        audit.log("auth", tenant="t1")
        audit.log("tool_call", tenant="t1", tool_name="tool_a")
        audit.log("auth", tenant="t2")

        events = audit.get_recent(event_type="auth", tenant="t1")
        assert len(events) == 1
        assert events[0].event_type == "auth"


# =========================================================================
# 测试2: 总线消息租户
# =========================================================================

class TestBusTenant:

    def test_message_default_tenant(self):
        msg = WorkflowMessage(from_agent_id="a1", content="hi")
        assert msg.tenant == "default"

    async def test_broker_cross_tenant_blocked(self):
        broker = InProcessBroker()
        await broker.subscribe("agent_a", tenant="t1")
        await broker.subscribe("agent_b", tenant="t2")

        # t1 → t2 点对点: 阻断
        msg = WorkflowMessage(
            from_agent_id="agent_a", to_agent_id="agent_b",
            tenant="t1", content="secret",
        )
        await broker.publish(msg)
        assert await broker.wait_for_message("agent_b", timeout=0.1) is None

        # 同租户点对点: 正常投递
        await broker.subscribe("agent_c", tenant="t1")
        msg2 = WorkflowMessage(
            from_agent_id="agent_a", to_agent_id="agent_c",
            tenant="t1", content="same-tenant",
        )
        await broker.publish(msg2)
        got = await broker.wait_for_message("agent_c", timeout=0.5)
        assert got is not None
        assert got.content == "same-tenant"

    async def test_broker_broadcast_filtered_by_tenant(self):
        broker = InProcessBroker()
        await broker.subscribe("a1", "wf1", tenant="t1")
        await broker.subscribe("a2", "wf1", tenant="t2")
        await broker.subscribe("a3", "wf1", tenant="t1")

        msg = WorkflowMessage(
            workflow_id="wf1", from_agent_id="a1",
            tenant="t1", content="bcast",
            msg_type=WorkflowMessageType.STATUS,
        )
        await broker.publish(msg)

        assert await broker.wait_for_message("a2", timeout=0.1) is None
        got = await broker.wait_for_message("a3", timeout=0.5)
        assert got is not None
        assert got.content == "bcast"

    async def test_broker_default_tenant_backward_compat(self):
        """旧调用方式（不传 tenant）→ 全部 default，投递行为不变"""
        broker = InProcessBroker()
        await broker.subscribe("a1")
        await broker.subscribe("a2")

        await broker.publish(WorkflowMessage(
            from_agent_id="a1", to_agent_id="a2", content="legacy",
        ))
        got = await broker.wait_for_message("a2", timeout=0.5)
        assert got is not None
        assert got.content == "legacy"

    async def test_broker_unsubscribe_clears_tenant(self):
        broker = InProcessBroker()
        await broker.subscribe("a1", tenant="t1")
        assert broker._agent_tenants.get("a1") == "t1"
        await broker.unsubscribe("a1")
        assert "a1" not in broker._agent_tenants

    async def test_bus_server_publish_forced_tenant(self):
        """BusServer 强制消息归属连接认证租户（防伪造）"""
        broker = InProcessBroker()
        server = BusServer(broker)
        await broker.subscribe("a1", tenant="t1")
        await broker.subscribe("a2", tenant="t2")
        await broker.subscribe("a3", tenant="t1")
        server._agent_tenants["a1"] = "t1"
        ws = MagicMock()

        # a1 伪造 tenant=t2 发给 t2 的 a2 → 覆盖为 t1 → 阻断
        forged = WorkflowMessage(
            from_agent_id="a1", to_agent_id="a2", tenant="t2", content="forged",
        )
        envelope = BusEnvelope(
            envelope_type="message", agent_id="a1",
            payload=forged.model_dump(mode="json"),
        )
        await server._handle_envelope("a1", envelope, ws)
        assert await broker.wait_for_message("a2", timeout=0.1) is None

        # a1 发给同租户 a3 → 投递成功且 tenant 被修正为 t1
        same = WorkflowMessage(
            from_agent_id="a1", to_agent_id="a3", tenant="t2", content="ok",
        )
        envelope2 = BusEnvelope(
            envelope_type="message", agent_id="a1",
            payload=same.model_dump(mode="json"),
        )
        await server._handle_envelope("a1", envelope2, ws)
        got = await broker.wait_for_message("a3", timeout=0.5)
        assert got is not None
        assert got.tenant == "t1"
        assert got.content == "ok"

    async def test_bus_server_cleanup_tenant(self):
        broker = InProcessBroker()
        server = BusServer(broker)
        server._agent_tenants["a1"] = "t1"
        await server._cleanup_agent("a1")
        assert "a1" not in server._agent_tenants


# =========================================================================
# 测试3: GUI Session 模型与 Bridge 跨租户防护
# =========================================================================

class TestGuiSessionTenant:

    def test_session_model_tenant_compat(self):
        s = Session(session_id="s1", type="single", name="n")
        assert s.tenant == "default"
        assert s.to_dict()["tenant"] == "default"

        # 旧数据（无 tenant 字段）→ default
        s2 = Session.from_dict({"session_id": "s2", "type": "single", "name": "x"})
        assert s2.tenant == "default"

        # 新数据保留 tenant
        s3 = Session.from_dict({
            "session_id": "s3", "type": "group", "name": "g", "tenant": "t9",
        })
        assert s3.tenant == "t9"

    async def test_ws_hub_tenant_broadcast(self):
        hub = WebSocketHub()
        ws_t1, ws_t2 = FakeWS(), FakeWS()
        hub.add(ws_t1, tenant="t1")
        hub.add(ws_t2, tenant="t2")

        await hub.broadcast({"type": "x"}, tenant="t1")
        assert len(ws_t1.sent) == 1
        assert len(ws_t2.sent) == 0

        # 无租户 → 全广播
        await hub.broadcast({"type": "y"})
        assert len(ws_t2.sent) == 1

        # 未登记的连接视为 default
        ws_anon = FakeWS()
        hub.add(ws_anon)
        assert hub.tenant_of(ws_anon) == "default"

        hub.remove(ws_t1)
        assert hub.tenant_of(ws_t1) == "default"  # 移除后回退默认


def _make_bridge(tmp_path) -> EngineBridge:
    """轻量构造 EngineBridge（不初始化 Master/Hooks），仅测试会话租户逻辑"""
    bridge = EngineBridge.__new__(EngineBridge)
    bridge.sessions = {}
    bridge.cards = {}
    bridge.store = Store(str(tmp_path / "gui_data"))
    bridge.hub = None
    bridge.master = SimpleNamespace(agent_id="master")
    bridge.active_session_id = None
    bridge._session_locks = {}
    bridge._open = {}
    emitted: list[dict] = []
    bridge._emit = emitted.append
    bridge._emitted = emitted
    return bridge


async def test_engine_bridge_session_tenant_guard(tmp_path):
    bridge = _make_bridge(tmp_path)

    s1 = await bridge.create_session("single", "A", tenant="t1")
    s2 = await bridge.create_session("single", "B", tenant="t2")
    assert s1["tenant"] == "t1"

    # 列表过滤
    t1_list = bridge.list_sessions(tenant="t1")
    assert [s["session_id"] for s in t1_list] == [s1["session_id"]]
    assert len(bridge.list_sessions()) == 2

    # 详情过滤
    assert bridge.get_session(s1["session_id"], tenant="t2") is None
    assert bridge.get_session(s1["session_id"], tenant="t1") is not None

    # 跨租户删除不生效
    await bridge.delete_session(s1["session_id"], tenant="t2")
    assert len(bridge.sessions) == 2
    await bridge.delete_session(s1["session_id"], tenant="t1")
    assert len(bridge.sessions) == 1

    # 跨租户发消息被拒（error 事件）
    await bridge.send_user_message(s2["session_id"], "hi", tenant="t1")
    assert any(e.get("type") == "error" for e in bridge._emitted)


async def test_engine_bridge_event_tenant(tmp_path):
    bridge = _make_bridge(tmp_path)
    s1 = await bridge.create_session("single", "A", tenant="t1")

    # 无活跃会话 → 广播全部
    assert bridge._event_tenant() is None

    # 活跃会话为 t1 → 事件目标租户 t1
    bridge.active_session_id = s1["session_id"]
    assert bridge._event_tenant() == "t1"
    bridge.active_session_id = None


# =========================================================================
# 测试4: GUI 端点集成（token → 租户）
# =========================================================================

def _make_config(tmp_path) -> GUIConfig:
    config = GUIConfig(host="127.0.0.1", port=0)
    config.data_dir = str(tmp_path / "gui_data")
    return config


def _enable_two_tenant_auth() -> None:
    configure_auth_manager(AuthManager([
        AuthToken(token="tok-t1", role=AuthRole.ADMIN, name="u1", tenant="t1"),
        AuthToken(token="tok-t2", role=AuthRole.ADMIN, name="u2", tenant="t2"),
    ]))


async def _make_client(tmp_path) -> TestClient:
    app = create_app(_make_config(tmp_path), use_mock=True)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_gui_sessions_isolated_by_token_tenant(tmp_path):
    _enable_two_tenant_auth()
    client = await _make_client(tmp_path)
    try:
        # t1 创建会话
        resp = await client.post(
            "/api/sessions?token=tok-t1",
            json={"type": "single", "name": "会话A"},
        )
        assert resp.status == 201
        sid = (await resp.json())["session_id"]

        # t1 可见且归属正确
        resp = await client.get("/api/sessions?token=tok-t1")
        data = await resp.json()
        assert [s["session_id"] for s in data["sessions"]] == [sid]
        assert data["sessions"][0]["tenant"] == "t1"

        # t2 不可见
        resp = await client.get("/api/sessions?token=tok-t2")
        data = await resp.json()
        assert data["sessions"] == []

        # t2 访问详情 → 404
        resp = await client.get(f"/api/sessions/{sid}?token=tok-t2")
        assert resp.status == 404

        # t2 删除不生效（t1 仍可见）
        resp = await client.delete(f"/api/sessions/{sid}?token=tok-t2")
        assert resp.status == 200
        resp = await client.get(f"/api/sessions/{sid}?token=tok-t1")
        assert resp.status == 200
    finally:
        await client.close()


async def test_gui_audit_isolated_by_token_tenant(tmp_path):
    _enable_two_tenant_auth()
    audit = AuditLogger()
    configure_audit_logger(audit)
    audit.log("probe", agent_id="t1-agent", tenant="t1")
    audit.log("probe", agent_id="t2-agent", tenant="t2")

    client = await _make_client(tmp_path)
    try:
        resp = await client.get("/api/audit?token=tok-t1")
        assert resp.status == 200
        events = (await resp.json())["events"]
        assert len(events) == 1
        assert events[0]["agent_id"] == "t1-agent"
        assert events[0]["tenant"] == "t1"

        resp = await client.get("/api/audit?token=tok-t2")
        events = (await resp.json())["events"]
        assert len(events) == 1
        assert events[0]["agent_id"] == "t2-agent"
    finally:
        await client.close()
