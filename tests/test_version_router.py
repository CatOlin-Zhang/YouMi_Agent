"""CallPathRouter 调用来源版本分流测试 — P1

覆盖:
1. SKILL → root 版本 (lineage 初始提交)
2. CROSS_DOMAIN → head 版本 (最新)
3. DIRECT → 最新版本 (现状语义)
4. ToolStore.get_root_version / get_head_version 辅助方法
5. resolve_tool_id / 字符串 source / 不存在工具
"""

from __future__ import annotations

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter
from youmi.mcp.models import ToolEntry, ToolContextTier
from youmi.mcp.tool_store import ToolStore
from youmi.mcp.version_router import CallPathRouter, CallPathSource


# ===================================================================
# 辅助
# ===================================================================

async def _make_store() -> ToolStore:
    store = ToolStore(db_path=":memory:", embedding_client=None)
    await store.initialize()
    return store


def _defn(name: str, description: str) -> ToolDefinition:
    return ToolDefinition(
        name=name, description=description,
        parameters=[ToolParameter(name="input", type="string")],
    )


async def _seed_versions(store: ToolStore) -> None:
    """创建 send_email 三个版本: 1.0.0 → 1.0.1 → 1.0.2"""
    await store.upsert_tool(ToolEntry(
        tool_name="send_email",
        definition=_defn("send_email", "发送电子邮件"),
        summary="发送电子邮件", tier=ToolContextTier.COLD, version="1.0.0",
    ))
    await store.create_version(
        "send_email", _defn("send_email", "发送电子邮件(支持附件)"),
        changelog="支持附件",
    )
    await store.create_version(
        "send_email", _defn("send_email", "发送电子邮件(支持群发和附件)"),
        changelog="支持群发",
    )


# ===================================================================
# 1. 三来源分流
# ===================================================================

class TestCallPathRouter:

    @pytest.mark.asyncio
    async def test_skill_resolves_root(self):
        """SKILL 来源 → root 版本 (lineage 初始提交 1.0.0)"""
        store = await _make_store()
        await _seed_versions(store)
        router = CallPathRouter(store)

        entry = await router.resolve("send_email", CallPathSource.SKILL)
        assert entry is not None
        assert entry.version == "1.0.0"
        assert "发送电子邮件" in entry.definition.description
        assert "附件" not in entry.definition.description
        await store.close()

    @pytest.mark.asyncio
    async def test_cross_domain_resolves_head(self):
        """CROSS_DOMAIN 来源 → head 版本 (最新)"""
        store = await _make_store()
        await _seed_versions(store)
        router = CallPathRouter(store)

        entry = await router.resolve("send_email", CallPathSource.CROSS_DOMAIN)
        assert entry is not None
        assert entry.version == "1.0.2"
        assert "群发" in entry.definition.description
        await store.close()

    @pytest.mark.asyncio
    async def test_direct_resolves_latest(self):
        """DIRECT 来源 → 最新版本 (现状语义)"""
        store = await _make_store()
        await _seed_versions(store)
        router = CallPathRouter(store)

        entry = await router.resolve("send_email", CallPathSource.DIRECT)
        assert entry is not None
        assert entry.version == "1.0.2"
        await store.close()

    @pytest.mark.asyncio
    async def test_default_source_is_direct(self):
        """source 缺省 → DIRECT"""
        store = await _make_store()
        await _seed_versions(store)
        router = CallPathRouter(store)

        entry = await router.resolve("send_email")
        assert entry is not None
        assert entry.version == "1.0.2"
        await store.close()

    @pytest.mark.asyncio
    async def test_string_source_accepted(self):
        """字符串 source 自动转换 (含 SKILL)"""
        store = await _make_store()
        await _seed_versions(store)
        router = CallPathRouter(store)

        root_e = await router.resolve("send_email", "skill")
        head_e = await router.resolve("send_email", "cross_domain")
        assert root_e.version == "1.0.0"
        assert head_e.version == "1.0.2"
        await store.close()

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_none(self):
        store = await _make_store()
        await _seed_versions(store)
        router = CallPathRouter(store)

        assert await router.resolve("ghost", CallPathSource.SKILL) is None
        assert await router.resolve("ghost", CallPathSource.DIRECT) is None
        await store.close()

    @pytest.mark.asyncio
    async def test_invalid_source_string_raises(self):
        store = await _make_store()
        router = CallPathRouter(store)
        with pytest.raises(ValueError):
            await router.resolve("x", "not_a_source")
        await store.close()

    @pytest.mark.asyncio
    async def test_resolve_tool_id(self):
        store = await _make_store()
        await _seed_versions(store)
        router = CallPathRouter(store)

        assert await router.resolve_tool_id("send_email", "skill") == \
            "send_email@1.0.0"
        assert await router.resolve_tool_id("send_email", "direct") == \
            "send_email@1.0.2"
        assert await router.resolve_tool_id("ghost") is None
        await store.close()

    @pytest.mark.asyncio
    async def test_single_version_root_equals_head(self):
        """单版本工具: root == head"""
        store = await _make_store()
        await store.upsert_tool(ToolEntry(
            tool_name="solo", definition=_defn("solo", "单版本工具"),
            summary="单版本工具", tier=ToolContextTier.COLD, version="2.0.0",
        ))
        router = CallPathRouter(store)

        root_e = await router.resolve("solo", CallPathSource.SKILL)
        head_e = await router.resolve("solo", CallPathSource.DIRECT)
        assert root_e.version == "2.0.0"
        assert head_e.version == "2.0.0"
        await store.close()


# ===================================================================
# 2. ToolStore root/head 辅助
# ===================================================================

class TestStoreRootHeadHelpers:

    @pytest.mark.asyncio
    async def test_get_root_and_head_version(self):
        store = await _make_store()
        await _seed_versions(store)

        root_e = await store.get_root_version("send_email")
        head_e = await store.get_head_version("send_email")
        assert root_e is not None and root_e.version == "1.0.0"
        assert head_e is not None and head_e.version == "1.0.2"
        await store.close()

    @pytest.mark.asyncio
    async def test_head_matches_latest_semantics(self):
        """get_head_version 与 get_latest_version 语义一致"""
        store = await _make_store()
        await _seed_versions(store)

        head_e = await store.get_head_version("send_email")
        latest_e = await store.get_latest_version("send_email")
        assert head_e.tool_name == latest_e.tool_name
        assert head_e.version == latest_e.version
        await store.close()

    @pytest.mark.asyncio
    async def test_missing_lineage_returns_none(self):
        store = await _make_store()
        await _seed_versions(store)

        assert await store.get_root_version("ghost") is None
        assert await store.get_head_version("ghost") is None
        await store.close()

    @pytest.mark.asyncio
    async def test_root_carries_cone_metadata(self):
        """root/head 结果携带锥形元数据 (risk/permissions/lineage)"""
        store = await _make_store()
        defn = ToolDefinition(
            name="gated", description="受限工具",
            parameters=[ToolParameter(name="input", type="string")],
            risk_level="high", required_permissions=["fs:write"],
        )
        await store.upsert_tool(ToolEntry(
            tool_name="gated", definition=defn,
            summary="受限工具", tier=ToolContextTier.COLD, version="1.0.0",
        ))

        root_e = await store.get_root_version("gated")
        assert root_e is not None
        assert root_e.risk_level == "high"
        assert root_e.required_permissions == ["fs:write"]
        assert root_e.lineage_id == "gated"
        await store.close()
