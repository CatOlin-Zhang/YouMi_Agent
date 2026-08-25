"""
ToolVault 工具发现与动态上下文管理 测试

测试覆盖:
1. EmbeddingClient — 向量生成、相似度计算
2. ToolVault 基础 — add_tool, search, load_tool, record_usage, recycle
3. 三级状态流转 — HOT→WARM, WARM→HOT, COLD→HOT
4. 必备工具保护 — essential=True 永不回收
5. LRU 策略 — 多轮未使用自动降级
6. MCPServer + Vault 联动
7. ToolBridge + Vault 联动
8. 回归测试 — 无 Vault 时行为不变
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter
from youmi.llm.embeddings import EmbeddingClient, EmbeddingError
from youmi.mcp.vault import ToolVault, ToolEntry, ToolContextTier, ToolSearchResult


# ===================================================================
# 辅助工具
# ===================================================================

def _make_tool(name: str, description: str = "") -> ToolDefinition:
    """创建测试用 ToolDefinition"""
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
    tier: ToolContextTier = ToolContextTier.COLD,
) -> ToolEntry:
    """创建测试用 ToolEntry"""
    return ToolEntry(
        tool_name=name,
        definition=_make_tool(name, description),
        essential=essential,
        summary=description[:80] if description else f"工具 {name}",
        tier=tier,
    )


class MockEmbeddingClient:
    """Mock EmbeddingClient: 根据文本内容生成确定性向量"""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self._call_count = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._call_count += 1
        return [self._text_to_vec(t) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        self._call_count += 1
        return self._text_to_vec(text)

    def _text_to_vec(self, text: str) -> list[float]:
        """确定性文本→向量: 基于字符哈希"""
        vec = [0.0] * self.dim
        for i, c in enumerate(text):
            vec[i % self.dim] += ord(c) / 100.0
        # 归一化
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        return EmbeddingClient.cosine_similarity(a, b)

    async def similarity(self, query_vec, candidates):
        return [self.cosine_similarity(query_vec, c) for c in candidates]

    async def close(self):
        pass


# ===================================================================
# 1. EmbeddingClient 测试
# ===================================================================

class TestEmbeddingClientUtils:
    """EmbeddingClient 工具函数测试 (无需 HTTP)"""

    def test_cosine_similarity_identical(self):
        v = [1.0, 2.0, 3.0]
        score = EmbeddingClient.cosine_similarity(v, v)
        assert abs(score - 1.0) < 1e-6

    def test_cosine_similarity_orthogonal(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        score = EmbeddingClient.cosine_similarity(a, b)
        assert abs(score) < 1e-6

    def test_cosine_similarity_opposite(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        score = EmbeddingClient.cosine_similarity(a, b)
        assert abs(score + 1.0) < 1e-6

    def test_cosine_similarity_empty(self):
        assert EmbeddingClient.cosine_similarity([], [1.0]) == 0.0
        assert EmbeddingClient.cosine_similarity([1.0], []) == 0.0

    def test_cosine_similarity_mismatched_length(self):
        assert EmbeddingClient.cosine_similarity([1.0, 2.0], [1.0]) == 0.0


# ===================================================================
# 2. ToolVault 基础测试
# ===================================================================

class TestToolVaultBasic:
    """ToolVault 基础功能测试"""

    def test_create_empty_vault(self):
        vault = ToolVault()
        assert vault.tool_count == 0
        assert vault.hot_count == 0
        assert vault.warm_count == 0
        assert vault.cold_count == 0

    @pytest.mark.asyncio
    async def test_add_tool(self):
        vault = ToolVault()
        entry = _make_entry("test_tool", "测试工具")
        await vault.add_tool(entry)
        assert vault.tool_count == 1
        assert "test_tool" in vault

    @pytest.mark.asyncio
    async def test_add_tool_auto_summary(self):
        vault = ToolVault()
        entry = ToolEntry(
            tool_name="test",
            definition=_make_tool("test", "这是一个很长的工具描述用来测试自动摘要功能"),
            summary="",  # 空摘要
        )
        await vault.add_tool(entry)
        stored = vault.get_entry("test")
        assert stored is not None
        assert stored.summary != ""

    @pytest.mark.asyncio
    async def test_get_entry(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("a"))
        assert vault.get_entry("a") is not None
        assert vault.get_entry("b") is None

    def test_tool_names(self):
        vault = ToolVault()
        # sync add for property test
        vault._entries["x"] = _make_entry("x")
        vault._entries["y"] = _make_entry("y")
        assert set(vault.tool_names) == {"x", "y"}


# ===================================================================
# 3. 三级状态流转测试
# ===================================================================

class TestToolVaultTierFlow:
    """三级状态 HOT/WARM/COLD 流转测试"""

    @pytest.mark.asyncio
    async def test_initial_tier_is_cold(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1"))
        entry = vault.get_entry("tool1")
        assert entry is not None
        assert entry.tier == ToolContextTier.COLD

    @pytest.mark.asyncio
    async def test_cold_to_hot(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1"))
        result = await vault.load_tool("tool1")
        assert result is not None
        assert result.tier == ToolContextTier.HOT
        assert vault.hot_count == 1

    @pytest.mark.asyncio
    async def test_hot_to_warm_via_unload(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1", tier=ToolContextTier.HOT))
        assert vault.hot_count == 1
        assert vault.unload_tool("tool1")
        assert vault.warm_count == 1
        assert vault.hot_count == 0

    @pytest.mark.asyncio
    async def test_warm_to_hot(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1", tier=ToolContextTier.WARM))
        result = await vault.load_tool("tool1")
        assert result is not None
        assert result.tier == ToolContextTier.HOT

    @pytest.mark.asyncio
    async def test_hot_already_hot(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1", tier=ToolContextTier.HOT))
        result = await vault.load_tool("tool1")
        assert result is not None
        assert result.tier == ToolContextTier.HOT

    @pytest.mark.asyncio
    async def test_load_nonexistent(self):
        vault = ToolVault()
        result = await vault.load_tool("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_tier_lists(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("hot1", tier=ToolContextTier.HOT))
        await vault.add_tool(_make_entry("hot2", tier=ToolContextTier.HOT))
        await vault.add_tool(_make_entry("warm1", tier=ToolContextTier.WARM))
        await vault.add_tool(_make_entry("cold1", tier=ToolContextTier.COLD))

        assert vault.hot_count == 2
        assert vault.warm_count == 1
        assert vault.cold_count == 1
        assert len(vault.get_hot_tools()) == 2
        assert len(vault.get_warm_tools()) == 1
        assert len(vault.get_cold_tools()) == 1


# ===================================================================
# 4. 必备工具保护测试
# ===================================================================

class TestEssentialProtection:
    """essential=True 的工具永不回收"""

    @pytest.mark.asyncio
    async def test_essential_not_recycled(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("essential_tool", essential=True, tier=ToolContextTier.HOT))
        await vault.add_tool(_make_entry("normal_tool", essential=False, tier=ToolContextTier.HOT))

        # 推进轮次
        for _ in range(10):
            vault.advance_turn()

        recycled = vault.recycle(idle_threshold=2)
        assert "essential_tool" not in recycled
        assert "normal_tool" in recycled

    @pytest.mark.asyncio
    async def test_essential_not_unloaded(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("essential", essential=True, tier=ToolContextTier.HOT))
        assert not vault.unload_tool("essential")
        assert vault.hot_count == 1


# ===================================================================
# 5. LRU 回收策略测试
# ===================================================================

class TestRecycleStrategy:
    """LRU 回收策略测试"""

    @pytest.mark.asyncio
    async def test_recycle_after_idle(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1", tier=ToolContextTier.HOT))

        # 记录使用
        vault.record_usage("tool1", turn=0)
        vault.advance_turn()  # turn=1
        vault.advance_turn()  # turn=2
        vault.advance_turn()  # turn=3

        # 3 轮未使用, threshold=3 → 回收
        recycled = vault.recycle(idle_threshold=3)
        assert "tool1" in recycled
        assert vault.get_entry("tool1").tier == ToolContextTier.WARM

    @pytest.mark.asyncio
    async def test_no_recycle_if_recently_used(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1", tier=ToolContextTier.HOT))

        vault.advance_turn()  # turn=1
        vault.record_usage("tool1", turn=1)  # 刚使用过
        vault.advance_turn()  # turn=2

        # 只过了 1 轮, threshold=3 → 不回收
        recycled = vault.recycle(idle_threshold=3)
        assert "tool1" not in recycled

    @pytest.mark.asyncio
    async def test_recycle_never_used(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1", tier=ToolContextTier.HOT))

        for _ in range(5):
            vault.advance_turn()

        recycled = vault.recycle(idle_threshold=3)
        assert "tool1" in recycled

    @pytest.mark.asyncio
    async def test_recycle_returns_list(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("a", tier=ToolContextTier.HOT))
        await vault.add_tool(_make_entry("b", tier=ToolContextTier.HOT))

        for _ in range(5):
            vault.advance_turn()

        recycled = vault.recycle(idle_threshold=2)
        assert len(recycled) == 2

    @pytest.mark.asyncio
    async def test_advance_turn(self):
        vault = ToolVault()
        assert vault.current_turn == 0
        vault.advance_turn()
        assert vault.current_turn == 1
        vault.advance_turn()
        assert vault.current_turn == 2


# ===================================================================
# 6. 语义搜索测试 (Mock Embedding)
# ===================================================================

class TestVaultSearch:
    """ToolVault 语义搜索测试"""

    @pytest.mark.asyncio
    async def test_search_with_mock_embedding(self):
        mock_client = MockEmbeddingClient()
        vault = ToolVault(embedding_client=mock_client)

        await vault.add_tool(_make_entry("file_read", "读取文件内容"))
        await vault.add_tool(_make_entry("send_email", "发送电子邮件"))
        await vault.add_tool(_make_entry("web_search", "搜索网页内容"))

        # 确保向量已生成
        assert all(e.embedding for e in vault._entries.values())

        # 搜索
        results = await vault.search("读取一个文件", top_k=3)
        assert len(results) > 0
        assert all(isinstance(r, ToolSearchResult) for r in results)

    @pytest.mark.asyncio
    async def test_search_excludes_hot_tools(self):
        mock_client = MockEmbeddingClient()
        vault = ToolVault(embedding_client=mock_client)

        await vault.add_tool(_make_entry("hot_tool", tier=ToolContextTier.HOT))
        await vault.add_tool(_make_entry("cold_tool"))

        results = await vault.search("工具", top_k=5)
        # HOT 工具不应出现在搜索结果中
        assert all(r.tool_name != "hot_tool" for r in results)

    @pytest.mark.asyncio
    async def test_search_no_embedding_client(self):
        vault = ToolVault()  # 无 embedding client
        await vault.add_tool(_make_entry("tool1", "搜索文件"))

        # 应该 fallback 到关键词搜索
        results = await vault.search("搜索文件", top_k=5)
        # 关键词匹配可能返回结果
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_keyword_search_fallback(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("file_search", "搜索文件"))
        await vault.add_tool(_make_entry("web_fetch", "抓取网页"))

        results = vault._keyword_search("搜索 文件", top_k=5)
        assert len(results) >= 1
        # file_search 应该匹配
        names = [r.tool_name for r in results]
        assert "file_search" in names

    @pytest.mark.asyncio
    async def test_search_min_score_filter(self):
        mock_client = MockEmbeddingClient()
        vault = ToolVault(embedding_client=mock_client)

        await vault.add_tool(_make_entry("tool_a", "完全不相关的描述"))

        # 设置极高的 min_score
        results = await vault.search("发送电子邮件", top_k=5, min_score=0.99)
        # 很可能被过滤掉
        assert len(results) == 0 or all(r.score >= 0.99 for r in results)


# ===================================================================
# 7. OpenAI schema 生成测试
# ===================================================================

class TestSchemaGeneration:
    """to_openai_tools 和 to_warm_summaries 测试"""

    @pytest.mark.asyncio
    async def test_openai_tools_hot_only(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("hot_tool", tier=ToolContextTier.HOT))
        await vault.add_tool(_make_entry("warm_tool", tier=ToolContextTier.WARM))
        await vault.add_tool(_make_entry("cold_tool", tier=ToolContextTier.COLD))

        schemas = vault.to_openai_tools()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "hot_tool"

    @pytest.mark.asyncio
    async def test_warm_summaries(self):
        vault = ToolVault()
        await vault.add_tool(_make_entry("warm1", "发送邮件的工具", tier=ToolContextTier.WARM))
        await vault.add_tool(_make_entry("warm2", "搜索文件", tier=ToolContextTier.WARM))
        await vault.add_tool(_make_entry("hot1", tier=ToolContextTier.HOT))

        summaries = vault.to_warm_summaries()
        assert len(summaries) == 2
        assert all("name" in s and "description" in s for s in summaries)


# ===================================================================
# 8. MCPServer + Vault 集成测试
# ===================================================================

class TestMCPServerVaultIntegration:
    """MCPServer 与 ToolVault 联动"""

    @pytest.mark.asyncio
    async def test_server_with_vault(self):
        from youmi.mcp.server import MCPServer
        from youmi.mcp.provider import LocalFunctionProvider

        mock_client = MockEmbeddingClient()
        vault = ToolVault(embedding_client=mock_client)
        server = MCPServer(vault=vault)

        assert server.vault is vault

        # 注册 Provider
        provider = LocalFunctionProvider(provider_id="test")
        provider.register(
            _make_tool("tool_a", "工具 A"),
            handler=lambda: "result_a",
        )
        provider.register(
            _make_tool("tool_b", "工具 B"),
            handler=lambda: "result_b",
        )

        await server.register_provider(provider, essential_names={"tool_a"})

        # tool_a 是必备 → HOT, tool_b 是普通 → COLD
        assert vault.get_entry("tool_a").tier == ToolContextTier.HOT
        assert vault.get_entry("tool_b").tier == ToolContextTier.COLD

        # to_openai_tools 只返回 hot 工具
        schemas = server.to_openai_tools()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "tool_a"

    @pytest.mark.asyncio
    async def test_server_without_vault(self):
        """无 Vault 时行为不变"""
        from youmi.mcp.server import MCPServer
        from youmi.mcp.provider import LocalFunctionProvider

        server = MCPServer()  # 无 vault
        assert server.vault is None

        provider = LocalFunctionProvider(provider_id="test")
        provider.register(_make_tool("tool_a"), handler=lambda: "ok")
        await server.register_provider(provider)

        schemas = server.to_openai_tools()
        assert len(schemas) == 1

    @pytest.mark.asyncio
    async def test_server_search_tools(self):
        from youmi.mcp.server import MCPServer
        from youmi.mcp.provider import LocalFunctionProvider

        mock_client = MockEmbeddingClient()
        vault = ToolVault(embedding_client=mock_client)
        server = MCPServer(vault=vault)

        provider = LocalFunctionProvider(provider_id="test")
        provider.register(_make_tool("email_sender", "发送电子邮件"), handler=lambda: "ok")
        provider.register(_make_tool("file_reader", "读取文件内容"), handler=lambda: "ok")

        await server.register_provider(provider)

        results = await server.search_tools("发送邮件", top_k=2)
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_server_warm_summaries(self):
        from youmi.mcp.server import MCPServer

        vault = ToolVault()
        await vault.add_tool(_make_entry("warm_tool", "温态工具", tier=ToolContextTier.WARM))
        server = MCPServer(vault=vault)

        summaries = server.to_warm_summaries()
        assert len(summaries) == 1


# ===================================================================
# 9. ToolBridge + Vault 集成测试
# ===================================================================

class TestToolBridgeVaultIntegration:
    """ToolBridge 与 ToolVault 联动"""

    @pytest.mark.asyncio
    async def test_bridge_with_vault(self):
        from youmi.mcp.bridge import ToolBridge

        vault = ToolVault()
        mock_client = MagicMock()
        bridge = ToolBridge(
            agent_id="test-agent",
            mcp_client=mock_client,
            vault=vault,
        )
        assert bridge.vault is vault

    @pytest.mark.asyncio
    async def test_bridge_discover_tools(self):
        from youmi.mcp.bridge import ToolBridge

        mock_embed = MockEmbeddingClient()
        vault = ToolVault(embedding_client=mock_embed)
        await vault.add_tool(_make_entry("search_tool", "搜索网页内容"))
        await vault.add_tool(_make_entry("email_tool", "发送电子邮件"))

        mock_client = MagicMock()
        bridge = ToolBridge(
            agent_id="agent-1",
            mcp_client=mock_client,
            vault=vault,
        )

        results = await bridge.discover_tools("搜索网页", top_k=2)
        assert isinstance(results, list)
        assert all("tool_name" in r and "score" in r for r in results)

    @pytest.mark.asyncio
    async def test_bridge_load_tool(self):
        from youmi.mcp.bridge import ToolBridge

        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1"))

        mock_client = MagicMock()
        bridge = ToolBridge(agent_id="a1", mcp_client=mock_client, vault=vault)

        assert await bridge.load_tool("tool1")
        assert vault.get_entry("tool1").tier == ToolContextTier.HOT

        assert not await bridge.load_tool("nonexistent")

    @pytest.mark.asyncio
    async def test_bridge_recycle_tools(self):
        from youmi.mcp.bridge import ToolBridge

        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1", tier=ToolContextTier.HOT))

        mock_client = MagicMock()
        bridge = ToolBridge(agent_id="a1", mcp_client=mock_client, vault=vault)

        for _ in range(5):
            bridge.advance_turn()

        recycled = bridge.recycle_tools(idle_threshold=2)
        assert "tool1" in recycled

    @pytest.mark.asyncio
    async def test_bridge_call_records_usage(self):
        from youmi.mcp.bridge import ToolBridge
        from youmi.mcp.protocol import MCPToolResult

        vault = ToolVault()
        await vault.add_tool(_make_entry("tool1", tier=ToolContextTier.HOT))

        mock_client = MagicMock()
        mock_client.call_tool = AsyncMock(return_value=MCPToolResult.success("ok"))

        bridge = ToolBridge(agent_id="a1", mcp_client=mock_client, vault=vault)
        await bridge.call_tool("tool1", {"input": "test"})

        entry = vault.get_entry("tool1")
        assert entry is not None
        assert entry.use_count == 1

    @pytest.mark.asyncio
    async def test_bridge_to_openai_tools_with_vault(self):
        from youmi.mcp.bridge import ToolBridge

        vault = ToolVault()
        await vault.add_tool(_make_entry("hot_tool", tier=ToolContextTier.HOT))
        await vault.add_tool(_make_entry("cold_tool", tier=ToolContextTier.COLD))

        mock_client = MagicMock()
        bridge = ToolBridge(agent_id="a1", mcp_client=mock_client, vault=vault)

        schemas = bridge.to_openai_tools()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "hot_tool"

    @pytest.mark.asyncio
    async def test_bridge_warm_summaries_with_vault(self):
        from youmi.mcp.bridge import ToolBridge

        vault = ToolVault()
        await vault.add_tool(_make_entry("warm1", "温态工具", tier=ToolContextTier.WARM))

        mock_client = MagicMock()
        bridge = ToolBridge(agent_id="a1", mcp_client=mock_client, vault=vault)

        summaries = bridge.to_warm_summaries()
        assert len(summaries) == 1

    @pytest.mark.asyncio
    async def test_bridge_without_vault(self):
        """无 Vault 时 ToolBridge 行为不变"""
        from youmi.mcp.bridge import ToolBridge

        mock_client = MagicMock()
        mock_client.to_openai_tools.return_value = [
            {"type": "function", "function": {"name": "test"}}
        ]
        bridge = ToolBridge(agent_id="a1", mcp_client=mock_client)

        assert bridge.vault is None
        schemas = bridge.to_openai_tools()
        assert len(schemas) == 1

        # discover/load/recycle 均返回空
        assert await bridge.discover_tools("query") == []
        assert not await bridge.load_tool("test")
        assert bridge.recycle_tools() == []


# ===================================================================
# 10. ToolDiscoveryConfig 测试
# ===================================================================

class TestToolDiscoveryConfig:
    """ToolDiscoveryConfig 配置测试"""

    def test_default_config(self):
        from youmi.core.types import ToolDiscoveryConfig
        config = ToolDiscoveryConfig()
        assert config.enabled is False
        assert config.embedding_model == "nomic-embed-text"
        assert config.max_hot_tools == 15
        assert config.recycle_after_turns == 3
        assert config.search_top_k == 5
        assert config.min_similarity == 0.3

    def test_custom_config(self):
        from youmi.core.types import ToolDiscoveryConfig
        config = ToolDiscoveryConfig(
            enabled=True,
            embedding_model="text-embedding-3-small",
            max_hot_tools=20,
            recycle_after_turns=5,
        )
        assert config.enabled is True
        assert config.max_hot_tools == 20


# ===================================================================
# 11. 回归测试
# ===================================================================

class TestToolVaultRegression:
    """确保无 Vault 时现有功能不受影响"""

    @pytest.mark.asyncio
    async def test_mcp_server_without_vault(self):
        from youmi.mcp.server import MCPServer
        from youmi.mcp.provider import LocalFunctionProvider

        server = MCPServer()
        provider = LocalFunctionProvider(provider_id="test")
        provider.register(_make_tool("tool_a"), handler=lambda: "ok")
        await server.register_provider(provider)

        tools = await server.list_tools()
        assert len(tools) == 1
        schemas = server.to_openai_tools()
        assert len(schemas) == 1

    @pytest.mark.asyncio
    async def test_toolbridge_without_vault(self):
        from youmi.mcp.bridge import ToolBridge

        mock_client = MagicMock()
        mock_client.to_openai_tools.return_value = []
        bridge = ToolBridge(agent_id="a1", mcp_client=mock_client)

        assert bridge.vault is None
        assert bridge.to_warm_summaries() == []

    def test_imports(self):
        """所有新类可从包级别导入"""
        from youmi import ToolVault, ToolEntry, ToolContextTier, ToolSearchResult
        from youmi import EmbeddingClient, EmbeddingError
        assert ToolVault is not None
        assert ToolEntry is not None
        assert EmbeddingClient is not None

    def test_mcp_module_imports(self):
        from youmi.mcp import ToolVault, ToolEntry, ToolContextTier
        assert ToolVault is not None


# ===================================================================
# 12. 完整工作流测试
# ===================================================================

class TestFullWorkflow:
    """完整工作流: 注册→搜索→加载→使用→回收"""

    @pytest.mark.asyncio
    async def test_complete_lifecycle(self):
        mock_embed = MockEmbeddingClient()
        vault = ToolVault(embedding_client=mock_embed)

        # 1. 注册工具 (必备 + 普通)
        await vault.add_tool(_make_entry("file_read", "读取文件", essential=True, tier=ToolContextTier.HOT))
        await vault.add_tool(_make_entry("send_email", "发送电子邮件"))
        await vault.add_tool(_make_entry("web_search", "搜索网页"))
        await vault.add_tool(_make_entry("calc", "数学计算"))

        assert vault.hot_count == 1  # 只有 file_read
        assert vault.cold_count == 3

        # 2. 搜索工具
        results = await vault.search("发送邮件给同事", top_k=2)
        assert len(results) > 0

        # 3. 加载搜索结果
        top_result = results[0]
        loaded = await vault.load_tool(top_result.tool_name)
        assert loaded is not None
        assert loaded.tier == ToolContextTier.HOT
        assert vault.hot_count == 2

        # 4. 使用工具
        vault.record_usage("file_read", turn=0)
        vault.record_usage(top_result.tool_name, turn=0)

        # 5. 推进轮次并回收
        for _ in range(5):
            vault.advance_turn()

        recycled = vault.recycle(idle_threshold=3)
        # file_read 是必备的, 不会被回收
        assert "file_read" not in recycled
        # 另一个工具应该被回收
        assert top_result.tool_name in recycled

        # 6. 回收后状态
        assert vault.get_entry("file_read").tier == ToolContextTier.HOT
        assert vault.get_entry(top_result.tool_name).tier == ToolContextTier.WARM

        # 7. 温态工具快速重载
        reloaded = await vault.load_tool(top_result.tool_name)
        assert reloaded is not None
        assert reloaded.tier == ToolContextTier.HOT
