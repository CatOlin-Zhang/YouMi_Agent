"""
ConeRetriever 锥形检索融合层 测试

测试覆盖:
1. permissions_satisfied — 权限子集判定纯函数
2. Store 路径 — SQL 锥形 + Python 层权限裁剪
3. Vault 路径 — 内存锥形 (风险/lineage/权限过滤)
4. Bridge 集成 — allowed_tools 白名单升为锥形权限边界
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from youmi.core.tool import RiskLevel, ToolDefinition, ToolParameter
from youmi.mcp.cone import (
    ConeQuery,
    ConeRetriever,
    permissions_satisfied,
)
from youmi.mcp.tool_store import ToolStore


# ===================================================================
# 辅助工具
# ===================================================================

class MockEmbeddingClient:
    """Mock EmbeddingClient: 根据文本内容生成确定性向量 (含 similarity)"""

    def __init__(self, dim: int = 8):
        self.dim = dim

    async def embed(self, texts):
        return [self._text_to_vec(t) for t in texts]

    async def embed_one(self, text):
        return self._text_to_vec(text)

    async def similarity(self, query_vec, candidates):
        out = []
        for c in candidates:
            dot = sum(a * b for a, b in zip(query_vec, c))
            norm_a = math.sqrt(sum(x * x for x in query_vec))
            norm_b = math.sqrt(sum(x * x for x in c))
            out.append(dot / (norm_a * norm_b) if norm_a and norm_b else 0.0)
        return out

    def _text_to_vec(self, text):
        vec = [0.0] * self.dim
        for i, c in enumerate(text):
            vec[i % self.dim] += ord(c) / 100.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec


def _mock_client() -> MagicMock:
    from youmi.mcp.client import MCPClient
    client = MagicMock(spec=MCPClient)
    client.list_tools = AsyncMock(return_value=[])
    client.call_tool = AsyncMock(return_value=MagicMock(is_error=False, text="ok"))
    client.to_openai_tools = MagicMock(return_value=[])
    return client


def _tool(name, desc, risk="low", perms=None):
    return ToolDefinition(
        name=name, description=desc,
        parameters=[ToolParameter(name="input", type="string")],
        risk_level=risk, required_permissions=perms or [],
    )


def _entry(name, desc, risk="low", perms=None):
    from youmi.mcp.vault import ToolEntry, ToolContextTier
    return ToolEntry(
        tool_name=name, definition=_tool(name, desc, risk, perms),
        summary=desc[:80], tier=ToolContextTier.COLD,
    )


# ===================================================================
# 1. 权限子集判定
# ===================================================================

class TestPermissionsSatisfied:
    """permissions_satisfied 纯函数测试"""

    def test_none_granted_is_unrestricted(self):
        """granted=None 表示无权限系统, 恒通过"""
        assert permissions_satisfied(["fs:delete"], None) is True

    def test_empty_required_always_passes(self):
        """无门槛工具 (required=[]) 恒通过"""
        assert permissions_satisfied([], set()) is True
        assert permissions_satisfied([], {"anything"}) is True

    def test_subset_passes(self):
        assert permissions_satisfied(
            ["fs:read"], {"fs:read", "fs:write"},
        ) is True

    def test_missing_permission_blocks(self):
        assert permissions_satisfied(
            ["fs:delete"], {"fs:read"},
        ) is False

    def test_exact_match_passes(self):
        assert permissions_satisfied(["a", "b"], {"a", "b"}) is True

    def test_strictest_grant_blocks_all(self):
        """空 granted 集合: 任何非空 required 都被裁"""
        assert permissions_satisfied(["fs:read"], set()) is False


# ===================================================================
# 2. Store 路径 (SQL 锥形 + Python 权限裁剪)
# ===================================================================

async def _make_store(with_embedding=True):
    store = ToolStore(
        db_path=":memory:",
        embedding_client=MockEmbeddingClient() if with_embedding else None,
        embedding_dim=8,
    )
    await store.initialize()
    return store


async def _seed(store):
    await store.upsert_tool(_entry("send_email", "发送电子邮件到指定地址", risk="low"))
    await store.upsert_tool(_entry(
        "write_file", "写入文件内容", risk="medium", perms=["fs:write"],
    ))
    await store.upsert_tool(_entry(
        "rm_file", "删除文件的shell工具", risk="critical", perms=["fs:delete"],
    ))
    if store._embedding_client:
        await store.update_embedding("send_email")
        await store.update_embedding("write_file")
        await store.update_embedding("rm_file")


class TestConeRetrieverStore:
    """Store 路径 — SQL 锥形联合 + 权限 Python 裁剪"""

    @pytest.mark.asyncio
    async def test_permission_boundary_filters(self):
        """权限边界: required_permissions 未被授予的候选剔除"""
        store = await _make_store()
        await _seed(store)
        retriever = ConeRetriever(store=store)

        # 只授予 fs:write → rm_file 需要 fs:delete 被裁
        results = await retriever.retrieve(ConeQuery(
            query="工具", top_k=10, min_score=0.0,
            granted_permissions={"fs:write"},
        ))
        names = {r.tool_name for r in results}
        assert "rm_file" not in names
        assert "write_file" in names  # fs:write ⊆ granted
        assert "send_email" in names  # 无门槛恒通过
        await store.close()

    @pytest.mark.asyncio
    async def test_unrestricted_grant_all(self):
        """granted_permissions=None: 权限边界开放"""
        store = await _make_store()
        await _seed(store)
        retriever = ConeRetriever(store=store)

        results = await retriever.retrieve(ConeQuery(
            query="工具", top_k=10, min_score=0.0,
            granted_permissions=None,
        ))
        assert {r.tool_name for r in results} == {"send_email", "write_file", "rm_file"}
        await store.close()

    @pytest.mark.asyncio
    async def test_risk_ceiling_via_store(self):
        """风险边界经 store SQL 锥形生效"""
        store = await _make_store()
        await _seed(store)
        retriever = ConeRetriever(store=store)

        results = await retriever.retrieve(ConeQuery(
            query="工具", top_k=10, min_score=0.0,
            risk_ceiling=RiskLevel.MEDIUM,
        ))
        names = {r.tool_name for r in results}
        assert "rm_file" not in names
        await store.close()

    @pytest.mark.asyncio
    async def test_exclude_lineages_via_store(self):
        """lineage 去重边界经 store SQL 锥形生效"""
        store = await _make_store()
        await _seed(store)
        retriever = ConeRetriever(store=store)

        results = await retriever.retrieve(ConeQuery(
            query="工具", top_k=10, min_score=0.0,
            exclude_lineages={"rm_file"},
        ))
        assert "rm_file" not in {r.tool_name for r in results}
        await store.close()

    @pytest.mark.asyncio
    async def test_stats_counts_permission_filtered(self):
        """ConeStats 统计权限裁剪数量"""
        store = await _make_store()
        await _seed(store)
        retriever = ConeRetriever(store=store)

        detail = await retriever.retrieve_detailed(ConeQuery(
            query="工具", top_k=10, min_score=0.0,
            granted_permissions=set(),  # 最严格: 非空 required 全裁
        ))
        assert detail.stats.recalled == 3
        assert detail.stats.permission_filtered == 2  # write_file + rm_file
        assert detail.stats.returned == 1  # send_email (无门槛)
        await store.close()

    @pytest.mark.asyncio
    async def test_keyword_mode_cone(self):
        """无 embedding client: 关键词锥形路径"""
        store = await _make_store(with_embedding=False)
        await _seed(store)
        retriever = ConeRetriever(store=store)

        # 关键词"文件"命中 write_file 与 rm_file;
        # 权限边界只授予 fs:write → rm_file 需 fs:delete 被裁;
        # 风险上限 medium 同样裁掉 rm_file
        results = await retriever.retrieve(ConeQuery(
            query="文件", top_k=10,
            granted_permissions={"fs:write"},
            risk_ceiling=RiskLevel.MEDIUM,
        ))
        names = {r.tool_name for r in results}
        assert "rm_file" not in names
        assert "write_file" in names
        await store.close()


# ===================================================================
# 3. Vault 路径 (内存锥形)
# ===================================================================

class TestConeRetrieverVault:
    """Vault-only 模式 — 内存召回 + Python 层锥形过滤"""

    def _make_vault(self, with_embedding=True):
        from youmi.mcp.vault import ToolVault
        return ToolVault(
            embedding_client=MockEmbeddingClient() if with_embedding else None,
        )

    @pytest.mark.asyncio
    async def test_vault_risk_filter(self):
        """内存锥形: 风险边界过滤生效 (vault.add_tool 同步 definition 风险位)"""
        vault = self._make_vault()
        await vault.add_tool(_entry("safe_tool", "安全工具描述", risk="low"))
        await vault.add_tool(_entry("danger_tool", "危险工具描述", risk="critical"))
        retriever = ConeRetriever(vault=vault)

        results = await retriever.retrieve(ConeQuery(
            query="工具", top_k=10, min_score=0.0,
            risk_ceiling=RiskLevel.MEDIUM,
        ))
        names = {r.tool_name for r in results}
        assert "danger_tool" not in names
        assert "safe_tool" in names

    @pytest.mark.asyncio
    async def test_vault_lineage_filter(self):
        """内存锥形: lineage 去重过滤生效"""
        vault = self._make_vault()
        await vault.add_tool(_entry("tool_a", "工具A描述"))
        await vault.add_tool(_entry("tool_b", "工具B描述"))
        retriever = ConeRetriever(vault=vault)

        results = await retriever.retrieve(ConeQuery(
            query="工具", top_k=10, min_score=0.0,
            exclude_lineages={"tool_a"},
        ))
        assert "tool_a" not in {r.tool_name for r in results}
        assert "tool_b" in {r.tool_name for r in results}

    @pytest.mark.asyncio
    async def test_vault_permission_filter(self):
        """内存锥形: 权限边界过滤生效"""
        vault = self._make_vault()
        await vault.add_tool(_entry("gated_tool", "受限工具", perms=["fs:write"]))
        await vault.add_tool(_entry("open_tool", "开放工具"))
        retriever = ConeRetriever(vault=vault)

        results = await retriever.retrieve(ConeQuery(
            query="工具", top_k=10, min_score=0.0,
            granted_permissions={"fs:read"},
        ))
        names = {r.tool_name for r in results}
        assert "gated_tool" not in names
        assert "open_tool" in names

    @pytest.mark.asyncio
    async def test_vault_keyword_mode(self):
        """无 embedding client: vault 关键词 + 锥形过滤"""
        vault = self._make_vault(with_embedding=False)
        await vault.add_tool(_entry("danger_tool", "危险删除工具", risk="critical"))
        await vault.add_tool(_entry("safe_tool", "安全读取工具", risk="low"))
        retriever = ConeRetriever(vault=vault)

        results = await retriever.retrieve(ConeQuery(
            query="工具", top_k=10,
            risk_ceiling=RiskLevel.LOW,
        ))
        names = {r.tool_name for r in results}
        assert "danger_tool" not in names
        assert "safe_tool" in names

    @pytest.mark.asyncio
    async def test_no_source_returns_empty(self):
        """无 vault/store: 返回空结果"""
        retriever = ConeRetriever()
        results = await retriever.retrieve(ConeQuery(query="任何"))
        assert results == []


# ===================================================================
# 4. Bridge 集成 — allowed_tools 升为锥形权限边界
# ===================================================================

class TestBridgeConeIntegration:
    """ToolBridge 搜索路径走锥形检索"""

    @staticmethod
    def _make_vault():
        from youmi.mcp.vault import ToolVault
        return ToolVault(embedding_client=MockEmbeddingClient())

    @pytest.mark.asyncio
    async def test_search_new_tools_permission_boundary(self):
        """受限 Agent: required_permissions 超出白名单的候选不被召回"""
        from youmi.mcp.bridge import ToolBridge

        vault = self._make_vault()
        await vault.add_tool(_entry("gated_tool", "受限工具描述", perms=["fs:write"]))
        await vault.add_tool(_entry("open_tool", "开放工具描述"))

        bridge = ToolBridge(
            agent_id="agent-1",
            mcp_client=_mock_client(),
            allowed_tools=["existing_tool"],  # 白名单不含 fs:write
            vault=vault,
            search_meta_tool=True,
        )

        result = await bridge.call_tool("search_new_tools", {"query": "工具", "top_k": 5})
        import json
        payload = json.loads(result.text)
        names = {c["name"] for c in payload["candidates"]}
        assert "gated_tool" not in names
        assert "open_tool" in names

    @pytest.mark.asyncio
    async def test_search_new_tools_unrestricted(self):
        """无限制 Agent: 权限边界开放, 全部候选可召回"""
        from youmi.mcp.bridge import ToolBridge

        vault = self._make_vault()
        await vault.add_tool(_entry("gated_tool", "受限工具描述", perms=["fs:write"]))
        await vault.add_tool(_entry("open_tool", "开放工具描述"))

        bridge = ToolBridge(
            agent_id="agent-2",
            mcp_client=_mock_client(),
            allowed_tools=None,  # 无限制
            vault=vault,
            search_meta_tool=True,
        )

        result = await bridge.call_tool("search_new_tools", {"query": "工具", "top_k": 5})
        import json
        payload = json.loads(result.text)
        names = {c["name"] for c in payload["candidates"]}
        assert names == {"gated_tool", "open_tool"}

    @pytest.mark.asyncio
    async def test_discover_tools_cone(self):
        """discover_tools 走锥形检索 (权限边界生效)"""
        from youmi.mcp.bridge import ToolBridge

        vault = self._make_vault()
        await vault.add_tool(_entry("gated_tool", "受限工具描述", perms=["net:admin"]))
        await vault.add_tool(_entry("open_tool", "开放工具描述"))

        bridge = ToolBridge(
            agent_id="agent-3",
            mcp_client=_mock_client(),
            allowed_tools=["base"],
            vault=vault,
        )

        results = await bridge.discover_tools("工具", top_k=10, min_score=0.0)
        names = {r["tool_name"] for r in results}
        assert "gated_tool" not in names
        assert "open_tool" in names
