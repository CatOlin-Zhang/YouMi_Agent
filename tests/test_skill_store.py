"""SkillStore SOP 并行库测试 — P2

覆盖:
1. 生命周期与基础读写 — upsert_skill / get_skill / get_version_chain
2. describe → L1/L2 惰性派生 (upsert 时缺省生成)
3. bound_tool_name 绑定 — resolve_bound_tool
4. 锥形检索 — 向量 ∩ tag ∩ 风险 (vec0 / 降级 / 关键词路径)
5. L2 薄层索引与回退
6. 诊断统计 — stats
"""

from __future__ import annotations

import math

import pytest

from youmi.core.tool import RiskLevel
from youmi.mcp.skill_store import SkillEntry, SkillSearchResult, SkillStore


# ===================================================================
# 辅助
# ===================================================================

class MockEmbeddingClient:
    """Mock EmbeddingClient: 根据文本内容生成确定性向量"""

    def __init__(self, dim: int = 8):
        self.dim = dim

    async def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for i, c in enumerate(text):
            vec[i % self.dim] += ord(c) / 100.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec


def _entry(name: str, describe: str, bound: str = "", **kw) -> SkillEntry:
    return SkillEntry(
        skill_name=name, describe=describe,
        bound_tool_name=bound, content_json=kw.pop("content", "SOP 内容"),
        tags=kw.pop("tags", []), risk_level=kw.pop("risk_level", "low"),
        **kw,
    )


# ===================================================================
# 1. 基础读写
# ===================================================================

class TestSkillStoreBasics:

    @pytest.mark.asyncio
    async def test_initialize_idempotent_and_stats(self):
        store = SkillStore(db_path=":memory:")
        await store.initialize()
        await store.initialize()  # 幂等

        stats = await store.stats()
        assert stats == {"skills": 0, "versions": 0, "bound_tools": 0}
        await store.close()

    @pytest.mark.asyncio
    async def test_upsert_and_get_skill(self):
        store = SkillStore(db_path=":memory:")
        await store.initialize()

        skill_id = await store.upsert_skill(_entry(
            "邮件汇报", "每日汇总数据并发送邮件", bound="send_email",
        ))
        assert skill_id == "邮件汇报@0.0.1"

        entry = await store.get_skill("邮件汇报")
        assert entry is not None
        assert entry.bound_tool_name == "send_email"
        assert entry.describe == "每日汇总数据并发送邮件"
        assert entry.content_json == "SOP 内容"
        await store.close()

    @pytest.mark.asyncio
    async def test_upsert_updates_same_skill_id(self):
        """同 skill_id 重复 upsert → 更新而非重复插入"""
        store = SkillStore(db_path=":memory:")
        await store.initialize()

        await store.upsert_skill(_entry("s1", "描述一", content="v1"))
        await store.upsert_skill(_entry("s1", "描述二", content="v2"))

        entry = await store.get_skill("s1")
        assert entry.describe == "描述二"
        assert entry.content_json == "v2"

        stats = await store.stats()
        assert stats["versions"] == 1
        await store.close()

    @pytest.mark.asyncio
    async def test_get_skill_by_version(self):
        store = SkillStore(db_path=":memory:")
        await store.initialize()
        await store.upsert_skill(_entry("s1", "描述一"))
        await store.upsert_skill(_entry("s1", "描述二", version="0.0.2"))

        latest = await store.get_skill("s1")
        assert latest.version == "0.0.2"
        specific = await store.get_skill("s1", version="0.0.1")
        assert specific.describe == "描述一"
        assert await store.get_skill("s1", version="9.9.9") is None
        await store.close()

    @pytest.mark.asyncio
    async def test_get_version_chain(self):
        store = SkillStore(db_path=":memory:")
        await store.initialize()
        await store.upsert_skill(_entry("s1", "v1"))
        await store.upsert_skill(
            _entry("s1", "v2", version="0.0.2", parent_version_id="s1@0.0.1"),
        )

        chain = await store.get_version_chain("s1")
        assert [c.version for c in chain] == ["0.0.2", "0.0.1"]
        assert chain[0].parent_version_id == "s1@0.0.1"
        assert chain[1].parent_version_id is None
        assert await store.get_version_chain("ghost") == []
        await store.close()

    @pytest.mark.asyncio
    async def test_uninitialized_raises(self):
        store = SkillStore(db_path=":memory:")
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.upsert_skill(_entry("s", "d"))


# ===================================================================
# 2. describe → L1/L2 惰性派生
# ===================================================================

class TestSummaryDerivation:

    @pytest.mark.asyncio
    async def test_l1_l2_derived_from_describe(self):
        """summary/summary_l2 缺省时由 describe 派生 (L1≤80 字 / L2≤30 字)"""
        store = SkillStore(db_path=":memory:")
        await store.initialize()

        long_desc = "这是一段超长的 skill 描述" * 10  # > 80 字
        await store.upsert_skill(_entry("s1", long_desc))

        entry = await store.get_skill("s1")
        assert entry.summary == long_desc[:80]
        assert entry.summary_l2 == entry.summary[:30]
        await store.close()

    @pytest.mark.asyncio
    async def test_explicit_summaries_preserved(self):
        """显式提供的 L1/L2 不被覆盖"""
        store = SkillStore(db_path=":memory:")
        await store.initialize()
        await store.upsert_skill(SkillEntry(
            skill_name="s1", describe="原始描述很长很长",
            summary="显式L1", summary_l2="显式L2",
        ))

        entry = await store.get_skill("s1")
        assert entry.summary == "显式L1"
        assert entry.summary_l2 == "显式L2"
        await store.close()


# ===================================================================
# 3. bound_tool_name 绑定
# ===================================================================

class TestBoundToolResolution:

    @pytest.mark.asyncio
    async def test_resolve_bound_tool(self):
        store = SkillStore(db_path=":memory:")
        await store.initialize()
        await store.upsert_skill(_entry("邮件汇报", "描述", bound="send_email"))
        await store.upsert_skill(_entry("文件清理", "描述", bound="rm_file"))

        skill = await store.resolve_bound_tool("send_email")
        assert skill is not None
        assert skill.skill_name == "邮件汇报"
        assert skill.bound_tool_name == "send_email"

        assert await store.resolve_bound_tool("no_bind") is None
        await store.close()

    @pytest.mark.asyncio
    async def test_resolve_bound_tool_latest_version(self):
        """多版本时返回最新绑定的 Skill 版本"""
        store = SkillStore(db_path=":memory:")
        await store.initialize()
        await store.upsert_skill(_entry("s1", "v1", bound="tool_a"))
        await store.upsert_skill(_entry("s1", "v2", bound="tool_a", version="0.0.2"))

        skill = await store.resolve_bound_tool("tool_a")
        assert skill.version == "0.0.2"
        await store.close()


# ===================================================================
# 4. 锥形检索 (vec0 路径)
# ===================================================================

async def _seed_skills(store: SkillStore) -> None:
    await store.upsert_skill(_entry(
        "邮件汇报", "每日汇总数据并发送邮件", bound="send_email",
        tags=["report", "email"],
    ))
    await store.upsert_skill(_entry(
        "文件清理", "清理临时文件的高危操作", bound="rm_file",
        tags=["ops"], risk_level="critical",
    ))
    await store.upsert_skill(_entry(
        "报表生成", "生成日报表并导出", bound="make_report",
        tags=["report"],
    ))


class TestSearchConeVec0:

    @pytest.mark.asyncio
    async def test_vector_recall(self):
        store = SkillStore(
            db_path=":memory:",
            embedding_client=MockEmbeddingClient(), embedding_dim=8,
        )
        await store.initialize()
        await _seed_skills(store)

        results = await store.search_cone("邮件", top_k=3, min_score=0.0)
        assert results
        assert "邮件汇报" in {r.skill_name for r in results}
        await store.close()

    @pytest.mark.asyncio
    async def test_tag_cone_boundary(self):
        """tag 边界: tags JSON 列命中任一才可召回"""
        store = SkillStore(
            db_path=":memory:",
            embedding_client=MockEmbeddingClient(), embedding_dim=8,
        )
        await store.initialize()
        await _seed_skills(store)

        results = await store.search_cone(
            "汇总", top_k=5, min_score=0.0, tags=["email"],
        )
        names = {r.skill_name for r in results}
        assert names == {"邮件汇报"}

        # 多 tag OR 语义
        results = await store.search_cone(
            "汇总", top_k=5, min_score=0.0, tags=["ops", "report"],
        )
        names = {r.skill_name for r in results}
        assert "文件清理" in names and "报表生成" in names
        await store.close()

    @pytest.mark.asyncio
    async def test_risk_ceiling_boundary(self):
        store = SkillStore(
            db_path=":memory:",
            embedding_client=MockEmbeddingClient(), embedding_dim=8,
        )
        await store.initialize()
        await _seed_skills(store)

        results = await store.search_cone(
            "操作", top_k=5, min_score=0.0, risk_ceiling=RiskLevel.MEDIUM,
        )
        assert "文件清理" not in {r.skill_name for r in results}

        # ceiling 提到 critical → 可召回
        results = await store.search_cone(
            "操作", top_k=5, min_score=0.0, risk_ceiling=RiskLevel.CRITICAL,
        )
        assert "文件清理" in {r.skill_name for r in results}
        await store.close()

    @pytest.mark.asyncio
    async def test_l2_index_level_and_fallback(self):
        """L2 薄层检索命中; 清空 L2 后自动回退 L1"""
        store = SkillStore(
            db_path=":memory:",
            embedding_client=MockEmbeddingClient(), embedding_dim=8,
        )
        await store.initialize()
        await _seed_skills(store)

        res_l2 = await store.search_cone(
            "邮件", top_k=3, min_score=0.0, index_level="l2",
        )
        assert "邮件汇报" in {r.skill_name for r in res_l2}

        # 制造"存量行无 L2 向量"场景 → 回退 L1
        store._conn.execute("DELETE FROM vec_skills_l2_idx")
        store._conn.commit()
        res_fb = await store.search_cone(
            "邮件", top_k=3, min_score=0.0, index_level="l2",
        )
        assert "邮件汇报" in {r.skill_name for r in res_fb}
        await store.close()

    @pytest.mark.asyncio
    async def test_result_carries_cone_metadata(self):
        store = SkillStore(
            db_path=":memory:",
            embedding_client=MockEmbeddingClient(), embedding_dim=8,
        )
        await store.initialize()
        await _seed_skills(store)

        results = await store.search_cone("邮件", top_k=1, min_score=0.0)
        assert isinstance(results[0], SkillSearchResult)
        assert results[0].bound_tool_name == "send_email"
        assert results[0].risk_level == "low"
        assert results[0].summary
        assert results[0].score > 0
        await store.close()


# ===================================================================
# 5. 降级路径 (无 sqlite-vec) 与关键词 fallback
# ===================================================================

class TestSearchConeFallback:

    @pytest.mark.asyncio
    async def test_json_fallback_with_cone(self):
        """强制降级: JSON 向量表 + Python 余弦 + 同一锥形条件"""
        store = SkillStore(
            db_path=":memory:",
            embedding_client=MockEmbeddingClient(), embedding_dim=8,
        )
        await store.initialize()
        assert store._vec_available  # 环境有 sqlite-vec
        store._vec_available = False  # 强制降级
        await _seed_skills(store)

        results = await store.search_cone("邮件", top_k=3, min_score=0.0)
        assert "邮件汇报" in {r.skill_name for r in results}

        results = await store.search_cone(
            "邮件", top_k=5, min_score=0.0, tags=["ops"],
        )
        # ops tag 锥形: 仅文件清理 (唯一含 ops tag) 可召回
        assert {r.skill_name for r in results} == {"文件清理"}
        await store.close()

    @pytest.mark.asyncio
    async def test_keyword_fallback(self):
        """无 embedding client → 关键词路径 (describe/summary/skill_name LIKE)"""
        store = SkillStore(db_path=":memory:", embedding_client=None)
        await store.initialize()
        await _seed_skills(store)

        results = await store.search_cone("邮件", top_k=3)
        assert [r.skill_name for r in results] == ["邮件汇报"]

        # tag 锥形在关键词路径同样生效
        results = await store.search_cone("邮件", top_k=3, tags=["nomatch"])
        assert not results
        await store.close()
