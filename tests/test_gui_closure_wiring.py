"""GUI 功能闭环三件套接线测试

覆盖（implementation_plan.md Phase 5 收尾项）：
1. GUIConfig — Skill/Guardian 配置项默认值 + load_config 环境变量覆盖
2. MCPService — SkillStore/SkillIngestor 创建、AuditGate/CallPathRouter
   构造、connect_agent 的 AuditGate 注入、skill_enabled=False 优雅降级
3. REST /api/skills — GET 检索 / POST 入库 / mock 引擎降级语义
4. 总线事件转发 — TOOL_REQUEST/TOOL_RESPONSE/FEEDBACK → agent_message
   事件（TASK 等聊天类不转发，避免与 MESSAGE_SENDING 钩子重复渲染）
5. ToolGuardian GUI 集成 — GlobalMemory 注入 master、guardian 总线订阅、
   worker 汇报 → 监听循环自动修复 → BUG_FIX 写回全局记忆 → 前端广播
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from gui.config import GUIConfig, load_config
from gui.engine.bridge import EngineBridge
from gui.engine.mcp_service import MCPService
from gui.hub.events import agent_message
from gui.persistence.store import Store
from gui.server import create_app
from youmi.bus.broker import InProcessBroker
from youmi.bus.message import WorkflowMessage, WorkflowMessageType
from youmi.core.types import MessageRole
from youmi.knowledge import GlobalMemory, KnowledgeCategory
from youmi.mcp.protocol import ToolIssueReport, ToolIssueType
from youmi.mcp.provider import LocalFunctionProvider
from youmi.mcp.server import MCPServer


# ===========================================================================
# 测试桩
# ===========================================================================

class _FakeMaster:
    """MCPService.setup() 所需的最小 MasterAgent 桩"""

    def __init__(self, env: str = ".") -> None:
        self.agent_id = "master-fake-0001"
        self.name = "FakeMaster"
        self.env = env
        self._tool_bridge = None
        self._approval_manager = None
        self._mcp_provider = None
        self._global_memory = None
        self._mcp_connected = False

    def connect_mcp(self, **kwargs) -> None:
        self._mcp_connected = True


class _FakeToolBridge:
    """记录 AuditGate 注入的 ToolBridge 桩"""

    def __init__(self) -> None:
        self.audit_gate = None
        self._allowed_tools = None

    def set_audit_gate(self, audit_gate) -> None:
        self.audit_gate = audit_gate

    def attach_vault(self, vault, essential_names=None) -> None:
        pass


class _FakeAgent:
    """connect_agent() 所需的最小 Agent 桩"""

    def __init__(self) -> None:
        self.agent_id = "sub-agent-0001"
        self.name = "Sub"
        self._tool_bridge = _FakeToolBridge()

    def connect_mcp(self, **kwargs) -> None:
        pass


class _RecordingHub:
    """收集广播事件的 WebSocketHub 桩"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def broadcast(self, event: dict, tenant=None) -> None:
        self.events.append(event)

    async def send(self, ws, event: dict) -> None:
        self.events.append(event)


class _StubMCPService:
    """带最小 shutdown 语义的 MCPService 桩（持有 MCPServer）"""

    def __init__(self, server: MCPServer) -> None:
        self.server = server
        self.embedding_client = None
        self.initialized = True

    async def shutdown(self) -> None:
        await self.server.stop()


def get_weather(city: str, unit: str = "celsius") -> str:
    """获取指定城市的天气"""
    weather_db = {
        "北京": {"celsius": "25°C", "fahrenheit": "77°F"},
        "上海": {"celsius": "28°C", "fahrenheit": "82°F"},
    }
    data = weather_db.get(city)
    if data is None:
        raise ValueError(f"城市 '{city}' 不在支持列表中")
    return data.get(unit, data.get("celsius", "未知"))


async def _make_weather_server() -> MCPServer:
    """搭建带 get_weather 工具的 MCPServer（guardian 修复目标）"""
    server = MCPServer()
    provider = LocalFunctionProvider(provider_id="local")
    provider.register_function(get_weather)
    await server.register_provider(provider)
    return server


def _make_light_bridge(tmp_path, hub: _RecordingHub | None = None) -> EngineBridge:
    """绕过 __init__ 构造最小 EngineBridge（不发 LLM / 不起真实 Master）"""
    bridge = EngineBridge.__new__(EngineBridge)
    bridge.hub = hub or _RecordingHub()
    bridge.config = SimpleNamespace(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
    )
    bridge.store = Store(str(tmp_path / "store"))
    bridge.sessions = {}
    bridge.cards = {}
    bridge.active_session_id = None
    bridge.master = _FakeMaster()
    bridge.mcp_service = None
    bridge.broker = InProcessBroker()
    bridge.global_memory = None
    bridge.guardian = None
    bridge._guardian_workflow_id = ""
    bridge._guardian_listener_task = None
    bridge._session_locks = {}
    bridge._open = {}
    bridge.tracker = None
    bridge.hook_bridge = None
    return bridge


_SKILL_DOC = (
    "---\n"
    "skill_name: weather_query_sop\n"
    "describe: 查询城市天气的标准流程，覆盖参数校验与异常处理\n"
    "bound_tool: get_weather\n"
    "tags: [weather, query]\n"
    "risk_level: low\n"
    "---\n"
    "# 天气查询 SOP\n"
    "1. 校验城市在支持列表（北京、上海）\n"
    "2. 调用 get_weather 获取天气\n"
)


# ===========================================================================
# 1. GUIConfig 配置项
# ===========================================================================

class TestGUIConfig:
    def test_defaults(self):
        cfg = GUIConfig()
        assert cfg.skill_enabled is True
        assert cfg.guardian_enabled is True
        assert cfg.skill_db_path == ""
        assert cfg.knowledge_db_path == ""

    def test_load_config_env_overrides(self, monkeypatch):
        monkeypatch.setenv("YOUMI_GUI_SKILL", "0")
        monkeypatch.setenv("YOUMI_GUI_GUARDIAN", "0")
        monkeypatch.setenv("YOUMI_GUI_SKILL_DB", "x.db")
        monkeypatch.setenv("YOUMI_GUI_KNOWLEDGE_DB", "y.db")
        cfg = load_config()
        assert cfg.skill_enabled is False
        assert cfg.guardian_enabled is False
        assert cfg.skill_db_path == "x.db"
        assert cfg.knowledge_db_path == "y.db"


# ===========================================================================
# 2. MCPService Skill / 召回治理接线
# ===========================================================================

class TestMCPServiceSkillWiring:
    async def test_setup_creates_skill_and_governance(self, tmp_path):
        master = _FakeMaster(env=str(tmp_path))
        svc = MCPService(
            vault_enabled=False,
            skill_enabled=True,
            skill_db_path=str(tmp_path / "skills.db"),
        )
        await svc.setup(master)
        try:
            assert svc.initialized
            assert svc.skill_store is not None
            assert svc.skill_ingestor is not None
            assert svc.audit_gate is not None
            assert svc.version_router is not None
            # CallPathRouter 注入 skill_store（P2 接线语义）
            assert svc.version_router.skill_store is svc.skill_store
            # AuditGate 构造了 master 的审批管理器挂点（此处为 None）
            assert svc.audit_gate.risk_threshold == "high"
        finally:
            await svc.shutdown()

    async def test_skill_disabled_degrades(self, tmp_path):
        master = _FakeMaster(env=str(tmp_path))
        svc = MCPService(
            vault_enabled=False,
            skill_enabled=False,
            skill_db_path=str(tmp_path / "skills.db"),
        )
        await svc.setup(master)
        try:
            assert svc.skill_store is None
            assert svc.skill_ingestor is None
            # version_router 仍创建，但无 skill_store（保持 P1 行为）
            assert svc.version_router is not None
            assert svc.version_router.skill_store is None
            # 入库/检索优雅降级
            result = await svc.ingest_skill(_SKILL_DOC)
            assert result["ok"] is False
            assert await svc.search_skills("天气") == []
            assert await svc.get_skill_stats() == {"enabled": False, "skills": 0}
        finally:
            await svc.shutdown()

    async def test_ingest_and_search_skill(self, tmp_path):
        master = _FakeMaster(env=str(tmp_path))
        svc = MCPService(
            vault_enabled=False,
            skill_enabled=True,
            skill_db_path=str(tmp_path / "skills.db"),
        )
        await svc.setup(master)
        try:
            result = await svc.ingest_skill(_SKILL_DOC)
            assert result["ok"] is True
            skill = result["skill"]
            assert skill["skill_name"] == "weather_query_sop"
            assert skill["bound_tool_name"] == "get_weather"
            assert skill["tags"] == ["weather", "query"]

            stats = await svc.get_skill_stats()
            assert stats["enabled"] is True
            assert stats["skills"] >= 1

            # 无 EmbeddingClient → 关键词检索降级，仍可命中
            results = await svc.search_skills("天气")
            assert any(r["skill_name"] == "weather_query_sop" for r in results)
            assert all(r["score"] >= 0 for r in results)
        finally:
            await svc.shutdown()

    async def test_connect_agent_injects_audit_gate(self, tmp_path):
        master = _FakeMaster(env=str(tmp_path))
        svc = MCPService(
            vault_enabled=False,
            skill_enabled=False,
            skill_db_path=str(tmp_path / "skills.db"),
        )
        await svc.setup(master)
        try:
            agent = _FakeAgent()
            svc.connect_agent(agent)
            # AuditGate 注入子 Agent 的 ToolBridge（召回审计生效）
            assert agent._tool_bridge.audit_gate is svc.audit_gate
        finally:
            await svc.shutdown()

    async def test_shutdown_releases_skill_store(self, tmp_path):
        master = _FakeMaster(env=str(tmp_path))
        svc = MCPService(
            vault_enabled=False,
            skill_enabled=True,
            skill_db_path=str(tmp_path / "skills.db"),
        )
        await svc.setup(master)
        store = svc.skill_store
        await svc.shutdown()
        assert svc.skill_store is None
        assert svc.skill_ingestor is None
        assert svc.version_router is None
        # 底层连接已关闭
        assert store._conn is None


# ===========================================================================
# 3. REST /api/skills 端点（mock 引擎）
# ===========================================================================

def _make_config(tmp_path) -> GUIConfig:
    config = GUIConfig(host="127.0.0.1", port=0)
    config.data_dir = str(tmp_path / "gui_data")
    return config


class TestSkillsRestEndpoints:
    async def test_get_skills_without_query(self, tmp_path):
        app = create_app(_make_config(tmp_path), use_mock=True)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/skills")
            assert resp.status == 200
            data = await resp.json()
            assert data["skills"] == []
            # mock 模式 Skill 库未启用
            assert data["stats"]["enabled"] is False
        finally:
            await client.close()

    async def test_get_skills_with_query(self, tmp_path):
        app = create_app(_make_config(tmp_path), use_mock=True)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/skills?q=weather&top_k=3")
            assert resp.status == 200
            data = await resp.json()
            assert data["skills"] == []
        finally:
            await client.close()

    async def test_post_skill_mock_rejected(self, tmp_path):
        app = create_app(_make_config(tmp_path), use_mock=True)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/api/skills", json={"doc": _SKILL_DOC})
            assert resp.status == 400
            data = await resp.json()
            assert data["ok"] is False

            # 空 doc → 400
            resp = await client.post("/api/skills", json={"doc": "   "})
            assert resp.status == 400

            # 非法 JSON → 400
            resp = await client.post(
                "/api/skills", data="{invalid",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
        finally:
            await client.close()


# ===========================================================================
# 4. agent_message 事件 + 总线转发
# ===========================================================================

class TestBusForwarding:
    def test_agent_message_event_shape(self):
        ev = agent_message(
            from_agent_id="guardian-1",
            from_agent_name="ToolGuardian",
            to_agent_id="master-1",
            msg_type="feedback",
            content="fixed",
            session_id="sess-1",
            metadata={"report_type": "guardian_fix"},
        )
        assert ev["type"] == "agent_message"
        assert ev["session_id"] == "sess-1"
        assert ev["from_agent_id"] == "guardian-1"
        assert ev["from_agent_name"] == "ToolGuardian"
        assert ev["to_agent_id"] == "master-1"
        assert ev["msg_type"] == "feedback"
        assert ev["content"] == "fixed"
        assert ev["metadata"] == {"report_type": "guardian_fix"}
        assert "ts" in ev

    async def test_governance_messages_forwarded(self, tmp_path):
        hub = _RecordingHub()
        bridge = _make_light_bridge(tmp_path, hub)
        try:
            bridge._setup_bus_forwarding()
            await bridge.broker.subscribe(bridge.master.agent_id)
            await bridge.broker.subscribe("worker-001")
            # 模拟 _patch_create_sub_agent 为子 Agent 注册转发
            bridge.broker.on_message("worker-001", bridge._bus_forwarder)

            # TOOL_REQUEST → 转发为 agent_message
            await bridge.broker.publish(WorkflowMessage(
                from_agent_id="worker-001",
                to_agent_id=bridge.master.agent_id,
                msg_type=WorkflowMessageType.TOOL_REQUEST,
                role=MessageRole.AGENT,
                content="需要文件读取工具",
            ))
            await asyncio.sleep(0.05)
            forwarded = [e for e in hub.events if e["type"] == "agent_message"]
            assert len(forwarded) == 1
            assert forwarded[0]["msg_type"] == "tool_request"
            assert forwarded[0]["content"] == "需要文件读取工具"

            # TOOL_RESPONSE → 转发
            await bridge.broker.publish(WorkflowMessage(
                from_agent_id=bridge.master.agent_id,
                to_agent_id="worker-001",
                msg_type=WorkflowMessageType.TOOL_RESPONSE,
                role=MessageRole.AGENT,
                content='{"approved": true}',
            ))
            await asyncio.sleep(0.05)
            forwarded = [e for e in hub.events if e["type"] == "agent_message"]
            assert len(forwarded) == 2
            assert forwarded[1]["msg_type"] == "tool_response"

            # TASK（聊天类）→ 不转发（由 MESSAGE_SENDING 钩子渲染）
            await bridge.broker.publish(WorkflowMessage(
                from_agent_id="worker-001",
                to_agent_id=bridge.master.agent_id,
                msg_type=WorkflowMessageType.TASK,
                role=MessageRole.AGENT,
                content="任务完成",
            ))
            await asyncio.sleep(0.05)
            forwarded = [e for e in hub.events if e["type"] == "agent_message"]
            assert len(forwarded) == 2
        finally:
            await bridge.shutdown()


# ===========================================================================
# 5. ToolGuardian GUI 集成（GlobalMemory + 总线闭环）
# ===========================================================================

class TestGuardianIntegration:
    async def test_setup_guardian_wiring(self, tmp_path):
        hub = _RecordingHub()
        bridge = _make_light_bridge(tmp_path, hub)
        bridge.mcp_service = _StubMCPService(await _make_weather_server())
        try:
            await bridge._setup_guardian()
            assert bridge.guardian is not None, "guardian 集成失败"
            assert bridge.global_memory is not None
            # GlobalMemory 注入 master → PostTaskPipeline 链路激活
            assert bridge.master._global_memory is bridge.global_memory
            # guardian 已登记卡片（前端可见）
            assert bridge.guardian.agent_id in bridge.cards
            assert bridge.cards[bridge.guardian.agent_id].role == "tool_guardian"
            # 总线订阅生效（点对点消息不丢弃）
            assert bridge.guardian.agent_id in bridge.broker._queues
        finally:
            await bridge.shutdown()

    async def test_guardian_end_to_end_via_bus(self, tmp_path):
        hub = _RecordingHub()
        bridge = _make_light_bridge(tmp_path, hub)
        bridge.mcp_service = _StubMCPService(await _make_weather_server())
        try:
            await bridge._setup_guardian()
            assert bridge.guardian is not None, "guardian 集成失败"
            # 强制走规则修复（避免依赖 Ollama 网络）
            bridge.guardian._llm_client = None
            bridge._setup_bus_forwarding()

            # 预置一条未解决的历史经验（PostTaskPipeline 沉淀语义）
            await bridge.global_memory.add_experience(
                tool_name="get_weather",
                content="get_weather 失败根因: 城市参数传入 '天津' 时报错不在支持列表中",
                category=KnowledgeCategory.TOOL_EXPERIENCE,
                source_agent_id="worker_old",
                success_rate=0.4,
            )

            # Worker Agent 汇报工具问题（FEEDBACK + tool_issue）
            report = ToolIssueReport(
                reporter_agent_id="worker-001",
                tool_name="get_weather",
                issue_type=ToolIssueType.PARAMETER_BOUNDARY,
                error_message="城市 '天津' 不在支持列表中",
                call_arguments={"city": "天津"},
                suggestion="建议在描述中列出支持的城市列表",
            )
            await bridge.broker.publish(WorkflowMessage(
                workflow_id=bridge._guardian_workflow_id,
                from_agent_id="worker-001",
                to_agent_id=bridge.guardian.agent_id,
                msg_type=WorkflowMessageType.FEEDBACK,
                role=MessageRole.AGENT,
                content=report.model_dump_json(),
                metadata={"report_type": "tool_issue"},
            ))

            # 等待监听循环消费 → 自动修复 → 写回全局记忆
            bug_fixes: list = []
            for _ in range(50):
                await asyncio.sleep(0.1)
                entries = await bridge.global_memory.list_entries(
                    tool_name="get_weather",
                )
                bug_fixes = [
                    e for e in entries
                    if e.category == KnowledgeCategory.BUG_FIX
                ]
                if bug_fixes:
                    break

            # 闭环断言：BUG_FIX 经验写入且已标记 resolved
            assert bug_fixes, "guardian 未写回 BUG_FIX 经验"
            assert all(e.resolved for e in bug_fixes)
            # 历史未解决条目被标记 resolved
            unresolved = await bridge.global_memory.list_entries(
                tool_name="get_weather", unresolved_only=True,
            )
            assert unresolved == []

            # 前端收到 guardian_fix 广播（agent_message 事件）
            fix_events = [
                e for e in hub.events
                if e["type"] == "agent_message"
                and e.get("metadata", {}).get("report_type") == "guardian_fix"
            ]
            assert fix_events, "未收到 guardian_fix 前端广播"
            assert "get_weather" in fix_events[0]["content"]

            # 工具描述已被修复（含 Guardian 修正标记）
            defn = bridge.mcp_service.server.get_tool_definition("get_weather")
            assert "Guardian 修正" in defn.description

            # 转发链路：worker 的 FEEDBACK 汇报也以 agent_message 到达前端
            feedback_events = [
                e for e in hub.events
                if e["type"] == "agent_message" and e["msg_type"] == "feedback"
            ]
            assert feedback_events
        finally:
            await bridge.shutdown()

    async def test_shutdown_cleans_guardian(self, tmp_path):
        hub = _RecordingHub()
        bridge = _make_light_bridge(tmp_path, hub)
        bridge.mcp_service = _StubMCPService(await _make_weather_server())
        await bridge._setup_guardian()
        assert bridge.guardian is not None
        memory = bridge.global_memory
        task = bridge._guardian_listener_task
        await bridge.shutdown()
        assert bridge.guardian is None
        assert bridge.global_memory is None
        assert bridge._guardian_listener_task is None
        assert task.done()
        # 底层记忆连接已关闭
        assert memory._conn is None
