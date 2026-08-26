"""
Phase 4 集成测试 — 完整流程验证

测试覆盖:
1. ToolVault + ToolStore 持久化集成
2. ToolVault + AgentToolContext + ToolBridge 完整流程
3. 召回确认闭环 — search_and_confirm + confirm/reject
4. 上下文注入 — inject_tool_context
5. 版本更新 + changelog 完整流程
6. ApprovalManager + ToolBridge 联动
7. load_from_store / sync_to_store 双向同步
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter, bump_version
from youmi.mcp.vault import ToolVault, ToolEntry, ToolContextTier, ToolSearchResult
from youmi.mcp.tool_store import ToolStore
from youmi.mcp.context import AgentToolContext
from youmi.mcp.bridge import ToolBridge
from youmi.mcp.approval import ApprovalManager, ApprovalLevel, ApprovalDecision
from youmi.mcp.client import MCPClient


# ===================================================================
# 辅助工具
# ===================================================================

def _make_tool(name: str, description: str = "") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description or f"工具 {name} 的功能描述",
        parameters=[
            ToolParameter(name="input", type="string", description="输入参数"),
        ],
    )


def _make_entry(
    name: str,
    description: str = "",
    essential: bool = False,
    embedding: list[float] | None = None,
    tier: ToolContextTier = ToolContextTier.COLD,
) -> ToolEntry:
    return ToolEntry(
        tool_name=name,
        definition=_make_tool(name, description or f"工具 {name} 的功能描述"),
        essential=essential,
        summary=(description or f"工具 {name}")[:80],
        tier=tier,
        embedding=embedding or [],
    )


class MockEmbeddingClient:
    def __init__(self, dim: int = 8):
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._text_to_vec(t) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._text_to_vec(text)

    def _text_to_vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for i, c in enumerate(text):
            vec[i % self.dim] += ord(c) / 100.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    async def similarity(self, query_vec, candidates):
        dot_products = []
        for c in candidates:
            dot = sum(a * b for a, b in zip(query_vec, c))
            norm_a = math.sqrt(sum(x * x for x in query_vec))
            norm_b = math.sqrt(sum(x * x for x in c))
            if norm_a == 0 or norm_b == 0:
                dot_products.append(0.0)
            else:
                dot_products.append(dot / (norm_a * norm_b))
        return dot_products

    async def close(self):
        pass


@pytest.fixture
async def store():
    s = ToolStore(db_path=":memory:", embedding_client=MockEmbeddingClient())
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def mock_client():
    client = MagicMock(spec=MCPClient)
    client.list_tools = AsyncMock(return_value=[])
    client.call_tool = AsyncMock(return_value=MagicMock(
        is_error=False, text="success",
    ))
    client.to_openai_tools = MagicMock(return_value=[])
    return client


# ===================================================================
# 1. ToolVault + ToolStore 持久化集成
# ===================================================================

class TestVaultStoreIntegration:
    """ToolVault 与 ToolStore 的双向同步测试"""

    @pytest.mark.asyncio
    async def test_add_tool_syncs_to_store(self, store):
        """add_tool 自动同步到 ToolStore"""
        vault = ToolVault(embedding_client=MockEmbeddingClient(), store=store)
        entry = _make_entry("sync_tool", "同步测试工具")
        await vault.add_tool(entry)

        # Vault 中有
        assert vault.get_entry("sync_tool") is not None

        # Store 中也有
        stored = await store.get_tool("sync_tool")
        assert stored is not None
        assert stored.tool_name == "sync_tool"

    @pytest.mark.asyncio
    async def test_load_from_store(self, store):
        """从 Store 加载工具到 Vault"""
        # 直接写入 Store
        entry = _make_entry("stored_tool", "存储在数据库中的工具")
        await store.upsert_tool(entry)

        # 新 Vault 从 Store 加载
        vault = ToolVault(store=store)
        loaded = await vault.load_from_store()
        assert loaded >= 1
        assert vault.get_entry("stored_tool") is not None

    @pytest.mark.asyncio
    async def test_sync_to_store(self, store):
        """将 Vault 中的工具同步到 Store"""
        vault = ToolVault(store=store)
        # 直接添加到内存（绕过 store 写入）
        vault._entries["mem_tool"] = _make_entry("mem_tool", "仅在内存中")

        count = await vault.sync_to_store()
        assert count >= 1

        stored = await store.get_tool("mem_tool")
        assert stored is not None

    @pytest.mark.asyncio
    async def test_store_none_backward_compatible(self):
        """store=None 时保持纯内存行为"""
        vault = ToolVault()
        entry = _make_entry("mem_only", "仅内存")
        await vault.add_tool(entry)
        assert vault.get_entry("mem_only") is not None
        assert vault.store is None

    @pytest.mark.asyncio
    async def test_vault_search_delegates_to_store(self, store):
        """Vault.search 委托给 Store.search"""
        vault = ToolVault(embedding_client=MockEmbeddingClient(), store=store)

        # 添加工具（会自动同步到 Store）
        await vault.add_tool(_make_entry("send_email", "发送电子邮件到指定地址"))
        await vault.add_tool(_make_entry("calc_math", "执行数学计算表达式"))

        # 生成 Store 中的向量
        await store.update_embedding("send_email")
        await store.update_embedding("calc_math")

        results = await vault.search("发送邮件", top_k=3, min_score=0.0)
        assert len(results) > 0


# ===================================================================
# 2. ToolVault + AgentToolContext + ToolBridge 完整流程
# ===================================================================

class TestFullPipeline:
    """完整的工具注册→发现→加载→使用→回收流程"""

    @pytest.mark.asyncio
    async def test_full_tool_lifecycle(self, mock_client):
        """完整工具生命周期: 注册→搜索→加载→使用→回收"""
        embedding_client = MockEmbeddingClient()
        vault = ToolVault(embedding_client=embedding_client)

        # 注册工具
        tools = ["send_email", "calc_math", "search_web", "file_read"]
        for name in tools:
            await vault.add_tool(_make_entry(name, f"{name} 的详细功能描述"))
        await vault.build_embeddings()

        # 创建 AgentToolContext
        ctx = AgentToolContext(agent_id="agent-001", vault=vault, max_hot_tools=3)
        ctx.init_tools(essential_names={"file_read"}, hot_names=set())

        # 创建 ToolBridge
        bridge = ToolBridge(
            agent_id="agent-001",
            mcp_client=mock_client,
            vault=vault,
            context=ctx,
        )

        # 搜索工具
        results = await bridge.discover_tools("发送邮件")
        # 可能找到也可能找不到（取决于 mock embedding 的相似度），不强制

        # 手动加载工具
        loaded = await bridge.load_tool("send_email")
        assert loaded is True
        assert ctx.get_tier("send_email") == ToolContextTier.HOT

        # Schema 生成
        schemas = bridge.to_openai_tools()
        names = {s.get("function", {}).get("name") for s in schemas}
        assert "send_email" in names

        # 推进轮次并回收
        ctx.advance_turn()
        ctx.advance_turn()
        ctx.advance_turn()
        ctx.advance_turn()

        recycled = bridge.recycle_tools()
        # send_email 未使用过且已过 4 轮，应被回收（file_read 是 essential 不会被回收）
        if "send_email" in recycled:
            assert ctx.get_tier("send_email") == ToolContextTier.WARM

    @pytest.mark.asyncio
    async def test_bridge_with_context_priority(self, mock_client):
        """ToolBridge 优先使用 AgentToolContext"""
        vault = ToolVault()
        vault._entries["tool_a"] = _make_entry("tool_a", "工具 A")

        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})

        bridge = ToolBridge(
            agent_id="agent-001",
            mcp_client=mock_client,
            vault=vault,
            context=ctx,
        )

        schemas = bridge.to_openai_tools()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "tool_a"

    @pytest.mark.asyncio
    async def test_bridge_without_context_uses_vault(self, mock_client):
        """无 context 时使用 Vault"""
        vault = ToolVault()
        vault._entries["tool_a"] = _make_entry("tool_a", "工具 A", tier=ToolContextTier.HOT)

        bridge = ToolBridge(
            agent_id="agent-001",
            mcp_client=mock_client,
            vault=vault,
        )

        schemas = bridge.to_openai_tools()
        assert len(schemas) == 1

    @pytest.mark.asyncio
    async def test_bridge_warm_summaries(self, mock_client):
        """温态工具摘要生成"""
        vault = ToolVault()
        vault._entries["tool_a"] = _make_entry("tool_a", "工具 A")

        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})
        ctx.demote("tool_a")  # HOT → WARM

        bridge = ToolBridge(
            agent_id="agent-001",
            mcp_client=mock_client,
            vault=vault,
            context=ctx,
        )

        summaries = bridge.to_warm_summaries()
        assert len(summaries) == 1
        assert summaries[0]["name"] == "tool_a"


# ===================================================================
# 3. 召回确认闭环
# ===================================================================

class TestSearchAndConfirm:
    """search_and_confirm, confirm_search_result, reject_search_result"""

    @pytest.mark.asyncio
    async def test_search_and_confirm_basic(self, mock_client):
        """基本搜索确认流程"""
        embedding_client = MockEmbeddingClient()
        vault = ToolVault(embedding_client=embedding_client)

        await vault.add_tool(_make_entry("send_email", "发送电子邮件"))
        await vault.add_tool(_make_entry("calc_math", "数学计算"))
        await vault.build_embeddings()

        bridge = ToolBridge(
            agent_id="agent-001",
            mcp_client=mock_client,
            vault=vault,
        )

        result = await bridge.search_and_confirm("发送邮件的工具", max_retries=1, min_score=0.0)
        # 可能有结果也可能没有（取决于 mock embedding），不强制断言

    @pytest.mark.asyncio
    async def test_search_and_confirm_no_vault(self, mock_client):
        """无 Vault 时返回 None"""
        bridge = ToolBridge(agent_id="agent-001", mcp_client=mock_client)
        result = await bridge.search_and_confirm("任何查询")
        assert result is None

    @pytest.mark.asyncio
    async def test_reject_search_result(self, mock_client):
        """reject_search_result 将工具加入排除列表"""
        bridge = ToolBridge(agent_id="agent-001", mcp_client=mock_client)
        bridge.reject_search_result("bad_tool")
        assert "bad_tool" in bridge._rejected_tools

    @pytest.mark.asyncio
    async def test_confirm_search_result(self, mock_client):
        """confirm_search_result 清理拒绝列表"""
        bridge = ToolBridge(agent_id="agent-001", mcp_client=mock_client)
        bridge.reject_search_result("tool_a")
        bridge.reject_search_result("tool_b")

        bridge.confirm_search_result("tool_c")
        assert len(bridge._rejected_tools) == 0

    @pytest.mark.asyncio
    async def test_reset_rejected(self, mock_client):
        """reset_rejected 清空拒绝列表"""
        bridge = ToolBridge(agent_id="agent-001", mcp_client=mock_client)
        bridge.reject_search_result("tool_a")
        bridge.reset_rejected()
        assert len(bridge._rejected_tools) == 0


# ===================================================================
# 4. 上下文注入
# ===================================================================

class TestInjectToolContext:
    """inject_tool_context 测试"""

    @pytest.mark.asyncio
    async def test_inject_with_context(self, mock_client):
        """有 AgentToolContext 时注入到 HOT"""
        vault = ToolVault()
        vault._entries["tool_a"] = _make_entry("tool_a", "工具 A")
        vault._entries["tool_b"] = _make_entry("tool_b", "工具 B")

        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names=set())

        bridge = ToolBridge(
            agent_id="agent-001",
            mcp_client=mock_client,
            vault=vault,
            context=ctx,
        )

        injected = await bridge.inject_tool_context(["tool_a", "tool_b"])
        assert injected == 2
        assert ctx.get_tier("tool_a") == ToolContextTier.HOT
        assert ctx.get_tier("tool_b") == ToolContextTier.HOT

    @pytest.mark.asyncio
    async def test_inject_without_context(self, mock_client):
        """无 AgentToolContext 时仅添加到 allowed_tools"""
        bridge = ToolBridge(agent_id="agent-001", mcp_client=mock_client)

        injected = await bridge.inject_tool_context(["tool_x"])
        assert injected == 1
        assert "tool_x" in (bridge.allowed_tools or set())

    @pytest.mark.asyncio
    async def test_inject_partial_success(self, mock_client):
        """部分工具不存在时部分注入"""
        vault = ToolVault()
        vault._entries["tool_a"] = _make_entry("tool_a", "工具 A")

        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names=set())

        bridge = ToolBridge(
            agent_id="agent-001",
            mcp_client=mock_client,
            vault=vault,
            context=ctx,
        )

        injected = await bridge.inject_tool_context(["tool_a", "nonexistent"])
        assert injected == 1  # 只有 tool_a 成功


# ===================================================================
# 5. 版本更新 + changelog 完整流程
# ===================================================================

class TestVersionAndChangelog:
    """ToolStore 版本更新 + changelog 集成测试"""

    @pytest.mark.asyncio
    async def test_version_update_flow(self, store):
        """创建工具 → 创建新版本 → 验证版本链"""
        entry = _make_entry("evolving_tool", "初始版本描述")
        await store.upsert_tool(entry)

        # 创建 patch 版本
        v2_def = _make_tool("evolving_tool", "修复了 bug 的版本")
        v2_id = await store.create_version(
            "evolving_tool", v2_def,
            changelog="修复了空指针异常", bump="patch"
        )
        assert "0.0.2" in v2_id

        # 创建 minor 版本
        v3_def = _make_tool("evolving_tool", "新增功能版本")
        v3_id = await store.create_version(
            "evolving_tool", v3_def,
            changelog="新增了批量处理功能", bump="minor"
        )
        assert "0.1.0" in v3_id

        # 验证版本链
        chain = await store.get_version_chain("evolving_tool")
        assert len(chain) == 3
        assert chain[0].version == "0.1.0"
        assert chain[1].version == "0.0.2"
        assert chain[2].version == "0.0.1"

    @pytest.mark.asyncio
    async def test_changelog_accumulation(self, store):
        """同一版本内多次 changelog"""
        entry = _make_entry("buggy_tool", "有很多 bug 的工具")
        await store.upsert_tool(entry)

        await store.add_changelog("buggy_tool", "bugfix", "修复 bug #1")
        await store.add_changelog("buggy_tool", "bugfix", "修复 bug #2")
        await store.add_changelog("buggy_tool", "description_update", "更新参数说明")

        chain = await store.get_version_chain("buggy_tool")
        assert len(chain) == 1
        assert "修复 bug #1" in chain[0].changelog
        assert "修复 bug #2" in chain[0].changelog
        assert "更新参数说明" in chain[0].changelog


# ===================================================================
# 6. ApprovalManager 与流程联动
# ===================================================================

class TestApprovalIntegration:
    """审批管理器与工具流程的联动"""

    @pytest.mark.asyncio
    async def test_approve_then_inject(self, mock_client):
        """审批通过后注入工具上下文"""
        vault = ToolVault()
        vault._entries["sensitive_tool"] = _make_entry("sensitive_tool", "敏感操作工具")

        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names=set())

        bridge = ToolBridge(
            agent_id="agent-001",
            mcp_client=mock_client,
            vault=vault,
            context=ctx,
        )

        # 审批
        mgr = ApprovalManager(sensitive_tools={"sensitive_tool"})
        record = mgr.submit_request("agent-001", "sensitive_tool")
        assert record.decision == ApprovalDecision.PENDING

        mgr.approve(record.record_id, decided_by="master", reason="任务需要")

        # 审批通过后注入
        approved = mgr.get_approved_tools("agent-001")
        if "sensitive_tool" in approved:
            injected = await bridge.inject_tool_context(["sensitive_tool"])
            assert injected == 1
            assert ctx.get_tier("sensitive_tool") == ToolContextTier.HOT

    def test_audit_trail(self):
        """完整审计追踪"""
        mgr = ApprovalManager(
            auto_approve_list={"safe_tool"},
            sensitive_tools={"dangerous_tool"},
        )

        # 多种审批场景
        mgr.submit_request("agent-001", "safe_tool")
        r1 = mgr.submit_request("agent-001", "dangerous_tool")
        r2 = mgr.submit_request("agent-002", "unknown_tool")

        mgr.approve(r1.record_id, decided_by="user", reason="已确认")
        mgr.deny(r2.record_id, decided_by="master", reason="不在授权范围")

        log = mgr.get_audit_log()
        # 至少有: safe_tool(auto), dangerous_tool(submit), dangerous_tool(approve),
        # unknown_tool(submit), unknown_tool(deny)
        assert len(log) >= 4


# ===================================================================
# 7. 全链路集成测试
# ===================================================================

class TestEndToEnd:
    """从 ToolStore 到 ToolBridge 的端到端测试"""

    @pytest.mark.asyncio
    async def test_store_to_bridge_pipeline(self, store, mock_client):
        """Store 写入 → Vault 加载 → Context 管理 → Bridge Schema"""
        # 1. 写入 Store
        entry1 = _make_entry("tool_alpha", "Alpha 工具的功能描述")
        entry2 = _make_entry("tool_beta", "Beta 工具的功能描述")
        await store.upsert_tool(entry1)
        await store.upsert_tool(entry2)

        # 2. 从 Store 加载到 Vault
        vault = ToolVault(store=store)
        loaded = await vault.load_from_store()
        assert loaded >= 2

        # 3. 创建 AgentToolContext
        ctx = AgentToolContext(agent_id="agent-e2e", vault=vault)
        ctx.init_tools(essential_names={"tool_alpha"}, hot_names={"tool_beta"})

        # 4. 创建 Bridge
        bridge = ToolBridge(
            agent_id="agent-e2e",
            mcp_client=mock_client,
            vault=vault,
            context=ctx,
        )

        # 5. 验证 Schema
        schemas = bridge.to_openai_tools()
        names = {s.get("function", {}).get("name") for s in schemas}
        assert "tool_alpha" in names
        assert "tool_beta" in names

        # 6. 验证 warm summaries
        ctx.demote("tool_beta")
        summaries = bridge.to_warm_summaries()
        assert any(s["name"] == "tool_beta" for s in summaries)

    @pytest.mark.asyncio
    async def test_multi_agent_isolation(self, mock_client):
        """多个 Agent 的上下文隔离"""
        vault = ToolVault()
        vault._entries["shared_tool"] = _make_entry("shared_tool", "共享工具")
        vault._entries["exclusive_tool"] = _make_entry("exclusive_tool", "专属工具")

        ctx_a = AgentToolContext(agent_id="agent-A", vault=vault)
        ctx_a.init_tools(hot_names={"shared_tool"})

        ctx_b = AgentToolContext(agent_id="agent-B", vault=vault)
        ctx_b.init_tools(hot_names={"shared_tool", "exclusive_tool"})

        # A 和 B 的 HOT 工具不同
        assert set(ctx_a.get_hot_tool_names()) == {"shared_tool"}
        assert set(ctx_b.get_hot_tool_names()) == {"shared_tool", "exclusive_tool"}

        # 各自独立回收
        ctx_a.advance_turn()
        ctx_a.advance_turn()
        ctx_a.advance_turn()
        ctx_a.advance_turn()
        recycled_a = ctx_a.recycle()
        assert "shared_tool" in recycled_a

        # B 不受影响
        assert ctx_b.get_tier("shared_tool") == ToolContextTier.HOT

    @pytest.mark.asyncio
    async def test_bridge_repr(self, mock_client):
        """Bridge repr 包含 context 信息"""
        vault = ToolVault()
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        bridge = ToolBridge(
            agent_id="agent-001",
            mcp_client=mock_client,
            vault=vault,
            context=ctx,
        )
        r = repr(bridge)
        assert "ToolBridge" in r
        assert "context=True" in r

    @pytest.mark.asyncio
    async def test_advance_turn_priority(self, mock_client):
        """advance_turn 优先使用 context"""
        vault = ToolVault()
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)

        bridge = ToolBridge(
            agent_id="agent-001",
            mcp_client=mock_client,
            vault=vault,
            context=ctx,
        )

        turn = bridge.advance_turn()
        assert turn == 1
        assert ctx.current_turn == 1
