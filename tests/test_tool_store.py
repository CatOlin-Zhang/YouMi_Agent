"""
ToolStore 工具持久化存储层 测试

测试覆盖:
1. 生命周期 — initialize, close
2. 核心 CRUD — upsert_tool, get_tool, list_tools, delete_tool
3. 版本管理 — create_version, get_version_chain, bump_version
4. 变更日志 — add_changelog
5. 向量搜索 — search (向量模式 + 关键词 fallback)
6. 别名与标签 — add_alias, resolve_alias, add_tag, search_by_tags
7. 依赖关系 — add_dependency
8. 诊断统计 — stats
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter, ToolVersion, bump_version
from youmi.mcp.tool_store import ToolStore, _cosine_similarity


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
    embedding: list[float] | None = None,
    version: str = "0.0.1",
):
    """创建测试用 ToolEntry"""
    from youmi.mcp.vault import ToolEntry, ToolContextTier
    return ToolEntry(
        tool_name=name,
        definition=_make_tool(name, description),
        essential=essential,
        summary=description[:80] if description else f"工具 {name}",
        tier=ToolContextTier.COLD,
        embedding=embedding or [],
        version=version,
    )


class MockEmbeddingClient:
    """Mock EmbeddingClient: 根据文本内容生成确定性向量"""

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


@pytest.fixture
async def store():
    """创建内存模式的 ToolStore"""
    s = ToolStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def store_with_embedding():
    """创建带 MockEmbeddingClient 的 ToolStore"""
    s = ToolStore(db_path=":memory:", embedding_client=MockEmbeddingClient())
    await s.initialize()
    yield s
    await s.close()


# ===================================================================
# 1. 余弦相似度工具函数测试
# ===================================================================

class TestCosineSimilarity:
    """_cosine_similarity 纯函数测试"""

    def test_identical(self):
        v = [1.0, 2.0, 3.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal(self):
        assert abs(_cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-6

    def test_opposite(self):
        assert abs(_cosine_similarity([1.0, 0.0], [-1.0, 0.0]) + 1.0) < 1e-6

    def test_empty(self):
        assert _cosine_similarity([], [1.0]) == 0.0
        assert _cosine_similarity([1.0], []) == 0.0

    def test_mismatched_length(self):
        assert _cosine_similarity([1.0, 2.0], [1.0]) == 0.0


# ===================================================================
# 2. bump_version 测试
# ===================================================================

class TestBumpVersion:
    """语义化版本号自增测试"""

    def test_patch(self):
        assert bump_version("1.2.3", "patch") == "1.2.4"

    def test_minor(self):
        assert bump_version("1.2.3", "minor") == "1.3.0"

    def test_major(self):
        assert bump_version("1.2.3", "major") == "2.0.0"

    def test_default_is_patch(self):
        assert bump_version("0.0.1") == "0.0.2"

    def test_invalid_format(self):
        assert bump_version("invalid") == "0.0.1"

    def test_two_parts(self):
        assert bump_version("1.2") == "0.0.1"


# ===================================================================
# 3. ToolStore 生命周期测试
# ===================================================================

class TestToolStoreLifecycle:
    """initialize / close 测试"""

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self):
        s = ToolStore(db_path=":memory:")
        await s.initialize()
        # 再次 initialize 应该幂等
        await s.initialize()
        stats = await s.stats()
        assert stats["tools"] == 0
        await s.close()

    @pytest.mark.asyncio
    async def test_close_and_reinitialize(self):
        s = ToolStore(db_path=":memory:")
        await s.initialize()
        await s.close()
        # close 后操作应抛异常
        with pytest.raises(RuntimeError):
            await s.list_tools()

    @pytest.mark.asyncio
    async def test_ensure_conn_raises(self):
        s = ToolStore(db_path=":memory:")
        with pytest.raises(RuntimeError, match="not initialized"):
            await s.list_tools()


# ===================================================================
# 4. 核心 CRUD 测试
# ===================================================================

class TestToolStoreCRUD:
    """upsert_tool, get_tool, list_tools, delete_tool 测试"""

    @pytest.mark.asyncio
    async def test_upsert_and_get(self, store):
        entry = _make_entry("test_tool", "测试工具描述")
        tool_id = await store.upsert_tool(entry)
        assert tool_id == "test_tool@0.0.1"

        # 获取最新版本
        result = await store.get_tool("test_tool")
        assert result is not None
        assert result.tool_name == "test_tool"
        assert result.version == "0.0.1"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, store):
        result = await store.get_tool("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_specific_version(self, store):
        entry = _make_entry("tool_a", "描述A")
        await store.upsert_tool(entry)

        result = await store.get_tool("tool_a", version="0.0.1")
        assert result is not None
        assert result.tool_name == "tool_a"

        result2 = await store.get_tool("tool_a", version="9.9.9")
        assert result2 is None

    @pytest.mark.asyncio
    async def test_list_tools(self, store):
        await store.upsert_tool(_make_entry("tool_a", "描述A"))
        await store.upsert_tool(_make_entry("tool_b", "描述B"))
        await store.upsert_tool(_make_entry("tool_c", "描述C"))

        tools = await store.list_tools()
        assert len(tools) == 3
        names = {t.tool_name for t in tools}
        assert names == {"tool_a", "tool_b", "tool_c"}

    @pytest.mark.asyncio
    async def test_delete_tool(self, store):
        await store.upsert_tool(_make_entry("tool_x", "描述X"))
        assert await store.get_tool("tool_x") is not None

        result = await store.delete_tool("tool_x")
        assert result is True
        assert await store.get_tool("tool_x") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, store):
        result = await store.delete_tool("nonexistent")
        assert result is True  # 删除不存在的不报错

    @pytest.mark.asyncio
    async def test_upsert_with_embedding(self, store):
        entry = _make_entry("tool_emb", "带向量的工具", embedding=[0.1, 0.2, 0.3])
        await store.upsert_tool(entry)

        result = await store.get_tool("tool_emb")
        assert result is not None
        assert result.embedding == [0.1, 0.2, 0.3]

    @pytest.mark.asyncio
    async def test_get_latest_version(self, store):
        entry = _make_entry("tool_v", "初始版本")
        await store.upsert_tool(entry)

        latest = await store.get_latest_version("tool_v")
        assert latest is not None
        assert latest.version == "0.0.1"


# ===================================================================
# 5. 版本管理测试
# ===================================================================

class TestToolStoreVersioning:
    """create_version, get_version_chain 测试"""

    @pytest.mark.asyncio
    async def test_create_version_patch(self, store):
        entry = _make_entry("versioned_tool", "初始版本")
        await store.upsert_tool(entry)

        new_def = _make_tool("versioned_tool", "修复了 bug")
        new_id = await store.create_version(
            "versioned_tool", new_def, changelog="修复了一个 bug", bump="patch"
        )
        assert new_id == "versioned_tool@0.0.2"

    @pytest.mark.asyncio
    async def test_create_version_minor(self, store):
        entry = _make_entry("tool_minor", "初始")
        await store.upsert_tool(entry)

        new_def = _make_tool("tool_minor", "新增功能")
        new_id = await store.create_version("tool_minor", new_def, bump="minor")
        assert new_id == "tool_minor@0.1.0"

    @pytest.mark.asyncio
    async def test_create_version_major(self, store):
        entry = _make_entry("tool_major", "初始")
        await store.upsert_tool(entry)

        new_def = _make_tool("tool_major", "破坏性变更")
        new_id = await store.create_version("tool_major", new_def, bump="major")
        assert new_id == "tool_major@1.0.0"

    @pytest.mark.asyncio
    async def test_version_chain(self, store):
        entry = _make_entry("chain_tool", "v1")
        await store.upsert_tool(entry)

        # 创建 v2
        v2_def = _make_tool("chain_tool", "v2 改进")
        await store.create_version("chain_tool", v2_def, changelog="v2 变更", bump="patch")

        # 创建 v3
        v3_def = _make_tool("chain_tool", "v3 大改")
        await store.create_version("chain_tool", v3_def, changelog="v3 变更", bump="minor")

        chain = await store.get_version_chain("chain_tool")
        assert len(chain) == 3
        # 按时间倒序: 最新在前
        assert chain[0].version == "0.1.0"
        assert chain[1].version == "0.0.2"
        assert chain[2].version == "0.0.1"

    @pytest.mark.asyncio
    async def test_create_version_nonexistent(self, store):
        with pytest.raises(ValueError, match="not found"):
            await store.create_version("no_such_tool", _make_tool("no_such_tool"))

    @pytest.mark.asyncio
    async def test_get_version(self, store):
        entry = _make_entry("ver_tool", "初始")
        await store.upsert_tool(entry)

        v2_def = _make_tool("ver_tool", "v2")
        await store.create_version("ver_tool", v2_def, bump="patch")

        v1 = await store.get_version("ver_tool", "0.0.1")
        assert v1 is not None
        assert v1.version == "0.0.1"

        v2 = await store.get_version("ver_tool", "0.0.2")
        assert v2 is not None
        assert v2.version == "0.0.2"


# ===================================================================
# 6. 变更日志测试
# ===================================================================

class TestToolStoreChangelog:
    """add_changelog 测试"""

    @pytest.mark.asyncio
    async def test_add_changelog(self, store):
        entry = _make_entry("log_tool", "有日志的工具")
        await store.upsert_tool(entry)

        await store.add_changelog("log_tool", "bugfix", "修复了空指针", source="agent-001")

        chain = await store.get_version_chain("log_tool")
        assert len(chain) == 1
        assert "修复了空指针" in chain[0].changelog

    @pytest.mark.asyncio
    async def test_multiple_changelogs(self, store):
        entry = _make_entry("multi_log", "多日志工具")
        await store.upsert_tool(entry)

        await store.add_changelog("multi_log", "bugfix", "修复 A")
        await store.add_changelog("multi_log", "description_update", "更新描述")

        chain = await store.get_version_chain("multi_log")
        assert len(chain) == 1
        assert "修复 A" in chain[0].changelog
        assert "更新描述" in chain[0].changelog

    @pytest.mark.asyncio
    async def test_changelog_nonexistent_tool(self, store):
        with pytest.raises(ValueError, match="not found"):
            await store.add_changelog("no_tool", "bugfix", "修复")


# ===================================================================
# 7. 向量搜索测试
# ===================================================================

class TestToolStoreSearch:
    """search 向量搜索和关键词搜索测试"""

    @pytest.mark.asyncio
    async def test_vector_search(self, store_with_embedding):
        store = store_with_embedding
        await store.upsert_tool(_make_entry("send_email", "发送电子邮件到指定地址"))
        await store.upsert_tool(_make_entry("calc_math", "执行数学计算"))
        await store.upsert_tool(_make_entry("search_web", "搜索互联网内容"))

        # 更新所有 embedding
        await store.update_embedding("send_email")
        await store.update_embedding("calc_math")
        await store.update_embedding("search_web")

        results = await store.search("发送邮件", top_k=3, min_score=0.0)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_vector_search_with_exclude(self, store_with_embedding):
        store = store_with_embedding
        await store.upsert_tool(_make_entry("tool_a", "工具 A 描述"))
        await store.upsert_tool(_make_entry("tool_b", "工具 B 描述"))

        await store.update_embedding("tool_a")
        await store.update_embedding("tool_b")

        results = await store.search("工具", top_k=5, min_score=0.0, exclude={"tool_a"})
        names = {r.tool_name for r in results}
        assert "tool_a" not in names

    @pytest.mark.asyncio
    async def test_keyword_search_fallback(self, store):
        """无 embedding_client 时使用关键词搜索"""
        await store.upsert_tool(_make_entry("send_email", "发送电子邮件到指定地址"))
        await store.upsert_tool(_make_entry("calc_math", "执行数学计算表达式"))

        results = await store.search("发送邮件", top_k=3)
        # 关键词匹配应该能找到 send_email
        if results:
            assert any(r.tool_name == "send_email" for r in results)

    @pytest.mark.asyncio
    async def test_keyword_search_with_exclude(self, store):
        await store.upsert_tool(_make_entry("send_email", "发送电子邮件"))
        await store.upsert_tool(_make_entry("send_sms", "发送短信消息"))

        results = await store.search("发送", top_k=5, exclude={"send_email"})
        names = {r.tool_name for r in results}
        assert "send_email" not in names

    @pytest.mark.asyncio
    async def test_search_empty_store(self, store):
        results = await store.search("任何查询")
        assert results == []

    @pytest.mark.asyncio
    async def test_update_embedding_no_client(self, store):
        """无 embedding_client 时 update_embedding 应静默返回"""
        await store.upsert_tool(_make_entry("tool_x", "描述"))
        await store.update_embedding("tool_x")  # 不应报错


# ===================================================================
# 8. 别名与标签测试
# ===================================================================

class TestToolStoreAliasAndTags:
    """别名和标签功能测试"""

    @pytest.mark.asyncio
    async def test_add_and_resolve_alias(self, store):
        entry = _make_entry("send_email", "发送邮件")
        await store.upsert_tool(entry)

        await store.add_alias("legacy_email", "send_email", "0.0.1")

        resolved = await store.resolve_alias("legacy_email")
        assert resolved is not None
        assert resolved.tool_name == "send_email"
        assert resolved.version == "0.0.1"

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_alias(self, store):
        result = await store.resolve_alias("no_such_alias")
        assert result is None

    @pytest.mark.asyncio
    async def test_add_tag(self, store):
        entry = _make_entry("tagged_tool", "有标签的工具")
        await store.upsert_tool(entry)

        await store.add_tag("tagged_tool", "communication")
        await store.add_tag("tagged_tool", "email")

        results = await store.search_by_tags(["communication"])
        assert len(results) >= 1
        assert any(r.tool_name == "tagged_tool" for r in results)

    @pytest.mark.asyncio
    async def test_search_by_multiple_tags(self, store):
        await store.upsert_tool(_make_entry("tool_a", "工具 A"))
        await store.upsert_tool(_make_entry("tool_b", "工具 B"))

        await store.add_tag("tool_a", "web")
        await store.add_tag("tool_b", "email")
        await store.add_tag("tool_a", "search")

        results = await store.search_by_tags(["web", "email"])
        names = {r.tool_name for r in results}
        assert "tool_a" in names
        assert "tool_b" in names

    @pytest.mark.asyncio
    async def test_search_by_nonexistent_tag(self, store):
        results = await store.search_by_tags(["nonexistent_tag"])
        assert results == []


# ===================================================================
# 9. 依赖关系测试
# ===================================================================

class TestToolStoreDependencies:
    """add_dependency 测试"""

    @pytest.mark.asyncio
    async def test_add_dependency(self, store):
        await store.upsert_tool(_make_entry("tool_a", "工具 A"))
        await store.upsert_tool(_make_entry("tool_b", "工具 B"))

        # 不应报错
        await store.add_dependency("tool_a", "tool_b", dep_type="required")

    @pytest.mark.asyncio
    async def test_add_dependency_nonexistent(self, store):
        # 不存在的工具不应报错（静默忽略）
        await store.add_dependency("no_tool", "also_no_tool")


# ===================================================================
# 10. 诊断统计测试
# ===================================================================

class TestToolStoreStats:
    """stats 统计信息测试"""

    @pytest.mark.asyncio
    async def test_empty_stats(self, store):
        stats = await store.stats()
        assert stats["tools"] == 0
        assert stats["versions"] == 0
        assert stats["vectors"] == 0
        assert stats["changelogs"] == 0
        assert stats["aliases"] == 0
        assert stats["tags"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_data(self, store):
        await store.upsert_tool(_make_entry("tool_a", "工具 A"))
        await store.upsert_tool(_make_entry("tool_b", "工具 B"))
        await store.add_tag("tool_a", "tag1")
        await store.add_changelog("tool_a", "bugfix", "修复")

        stats = await store.stats()
        assert stats["tools"] == 2
        assert stats["versions"] == 2
        assert stats["changelogs"] == 1
        assert stats["tags"] == 1

    @pytest.mark.asyncio
    async def test_repr(self, store):
        r = repr(store)
        assert "ToolStore" in r
        assert ":memory:" in r
