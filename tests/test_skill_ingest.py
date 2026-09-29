"""SkillIngestor SOP 文档入库测试 — P2

覆盖:
1. parse_skill_doc 宽容解析 — frontmatter / dict / 无 frontmatter 正文标记
2. ingest 流程 — describe → L1/L2 生成 → 绑定 → 入库
3. LLM 生成式摘要注入
4. CallPathRouter SKILL→root 联动 (绑定存在 → root / 无绑定 → head)
"""

from __future__ import annotations

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter
from youmi.mcp.models import ToolEntry, ToolContextTier
from youmi.mcp.skill_ingest import SkillIngestor, parse_skill_doc
from youmi.mcp.skill_store import SkillStore
from youmi.mcp.summary import SummaryGenerator
from youmi.mcp.tool_store import ToolStore
from youmi.mcp.version_router import CallPathRouter, CallPathSource


# ===================================================================
# 辅助
# ===================================================================

_SKILL_DOC = """---
skill: 邮件汇报流程
describe: 每日汇总数据并发送邮件
tools: send_email
tags: [report, email]
risk: medium
---

## 步骤
1. 拉取当日数据
2. 渲染模板并调用邮件工具发送
"""


def _defn(name: str, description: str) -> ToolDefinition:
    return ToolDefinition(
        name=name, description=description,
        parameters=[ToolParameter(name="input", type="string")],
    )


async def _seed_tool_versions(store: ToolStore) -> None:
    """send_email: main 1.0.0 → 1.0.1"""
    await store.upsert_tool(ToolEntry(
        tool_name="send_email", definition=_defn("send_email", "发送电子邮件"),
        summary="发送电子邮件", tier=ToolContextTier.COLD, version="1.0.0",
    ))
    await store.create_version(
        "send_email", _defn("send_email", "发送电子邮件(支持附件)"),
    )


# ===================================================================
# 1. parse_skill_doc 宽容解析
# ===================================================================

class TestParseSkillDoc:

    def test_frontmatter_full(self):
        parsed = parse_skill_doc(_SKILL_DOC)
        assert parsed["skill_name"] == "邮件汇报流程"
        assert parsed["describe"] == "每日汇总数据并发送邮件"
        assert parsed["bound_tool_name"] == "send_email"
        assert parsed["tags"] == ["report", "email"]
        assert parsed["risk_level"] == "medium"
        # 全文保留 (含 frontmatter)
        assert "skill: 邮件汇报流程" in parsed["content"]
        assert "调用邮件工具" in parsed["content"]

    def test_dict_input_aliases(self):
        """dict 输入宽容取键 (name/description/tools 列表)"""
        parsed = parse_skill_doc({
            "name": "报表生成",
            "description": "生成日报表",
            "tools": ["make_report", "send_email"],
            "tags": "report, daily",
        })
        assert parsed["skill_name"] == "报表生成"
        assert parsed["describe"] == "生成日报表"
        # tools 列表取首个 (一 Skill 绑定一 Tool 原始名称)
        assert parsed["bound_tool_name"] == "make_report"
        assert parsed["tags"] == ["report", "daily"]
        # content 由剩余字段序列化
        assert "报表生成" in parsed["content"]

    def test_body_tool_marker_fallback(self):
        """无 frontmatter 时正文代码块工具标记兜底"""
        doc = "# 数据清洗SOP\n\ndescribe: 清洗脏数据\n\n```\ntool: clean_data\n```\n\n步骤若干"
        parsed = parse_skill_doc(doc)
        assert parsed["bound_tool_name"] == "clean_data"
        assert parsed["describe"] == "清洗脏数据"
        # skill_name 兜底自标题
        assert parsed["skill_name"] == "数据清洗SOP"

    def test_defaults_when_nothing_parsed(self):
        parsed = parse_skill_doc("")
        assert parsed["skill_name"] == "untitled_skill"
        assert parsed["describe"] == ""
        assert parsed["bound_tool_name"] == ""
        assert parsed["tags"] == []
        assert parsed["risk_level"] == "low"

    def test_invalid_risk_falls_back_to_low(self):
        parsed = parse_skill_doc("---\nskill: s\ndescribe: d\nrisk: extreme\n---\n正文")
        assert parsed["risk_level"] == "low"


# ===================================================================
# 2. ingest 流程
# ===================================================================

class TestIngest:

    @pytest.mark.asyncio
    async def test_ingest_generates_summaries_and_binding(self):
        """describe → L1/L2 (启发式截断) → 绑定 → 入库"""
        store = SkillStore(db_path=":memory:")
        await store.initialize()
        ingestor = SkillIngestor(store)

        entry = await ingestor.ingest(_SKILL_DOC)
        assert entry.skill_name == "邮件汇报流程"
        assert entry.summary == "每日汇总数据并发送邮件"
        assert entry.summary_l2 == entry.summary[:30]
        assert entry.bound_tool_name == "send_email"
        assert entry.tags == ["report", "email"]
        assert entry.risk_level == "medium"
        assert "步骤" in entry.content_json

        # 入库可读回
        stored = await store.get_skill("邮件汇报流程")
        assert stored is not None
        assert stored.bound_tool_name == "send_email"
        await store.close()

    @pytest.mark.asyncio
    async def test_ingest_does_not_create_tools(self):
        """SOP/Tool 二分: ingest 不创建任何 Tool (ToolStore 无关)"""
        store = SkillStore(db_path=":memory:")
        await store.initialize()
        ingestor = SkillIngestor(store)

        await ingestor.ingest(_SKILL_DOC)
        # SkillStore 侧只有 1 个 skill; ToolStore 未被触碰
        stats = await store.stats()
        assert stats["skills"] == 1
        await store.close()

    @pytest.mark.asyncio
    async def test_ingest_idempotent_same_skill_id(self):
        store = SkillStore(db_path=":memory:")
        await store.initialize()
        ingestor = SkillIngestor(store)

        await ingestor.ingest(_SKILL_DOC)
        await ingestor.ingest(_SKILL_DOC)
        stats = await store.stats()
        assert stats["versions"] == 1
        await store.close()

    @pytest.mark.asyncio
    async def test_ingest_with_llm_summary_generator(self):
        """注入 LLM client 的 SummaryGenerator → 生成式摘要"""
        class FakeLLM:
            async def chat(self, messages):
                return "LLM 生成的摘要"

        store = SkillStore(db_path=":memory:")
        await store.initialize()
        ingestor = SkillIngestor(store, summary_generator=SummaryGenerator(FakeLLM()))

        entry = await ingestor.ingest(_SKILL_DOC)
        assert entry.summary == "LLM 生成的摘要"
        assert entry.summary_l2 == "LLM 生成的摘要"
        await store.close()


# ===================================================================
# 3. CallPathRouter SKILL→root 联动
# ===================================================================

class TestCallPathRouterSkillIntegration:

    @pytest.mark.asyncio
    async def test_bound_tool_resolves_root(self):
        """有 Skill 绑定 → SKILL 来源解析 Tool root 版本"""
        tool_store = ToolStore(db_path=":memory:")
        await tool_store.initialize()
        await _seed_tool_versions(tool_store)

        skill_store = SkillStore(db_path=":memory:")
        await skill_store.initialize()
        await SkillIngestor(skill_store).ingest(_SKILL_DOC)  # 绑定 send_email

        router = CallPathRouter(tool_store, skill_store=skill_store)
        entry = await router.resolve("send_email", CallPathSource.SKILL)
        assert entry is not None
        assert entry.version == "1.0.0"  # root
        assert "附件" not in entry.definition.description

        # DIRECT 仍为 head
        direct = await router.resolve("send_email", CallPathSource.DIRECT)
        assert direct.version == "1.0.1"
        await tool_store.close()
        await skill_store.close()

    @pytest.mark.asyncio
    async def test_unbound_tool_falls_back_to_head(self):
        """无 Skill 绑定 → SKILL 回退 DIRECT (head) 语义"""
        tool_store = ToolStore(db_path=":memory:")
        await tool_store.initialize()
        await _seed_tool_versions(tool_store)

        skill_store = SkillStore(db_path=":memory:")
        await skill_store.initialize()
        # 不 ingest 任何绑定 send_email 的 skill

        router = CallPathRouter(tool_store, skill_store=skill_store)
        entry = await router.resolve("send_email", CallPathSource.SKILL)
        assert entry is not None
        assert entry.version == "1.0.1"  # head
        await tool_store.close()
        await skill_store.close()

    @pytest.mark.asyncio
    async def test_no_skill_store_keeps_p1_behavior(self):
        """未注入 skill_store → SKILL 无条件 root (P1 行为, 存量兼容)"""
        tool_store = ToolStore(db_path=":memory:")
        await tool_store.initialize()
        await _seed_tool_versions(tool_store)

        router = CallPathRouter(tool_store)  # 无 skill_store
        entry = await router.resolve("send_email", CallPathSource.SKILL)
        assert entry.version == "1.0.0"
        await tool_store.close()

    @pytest.mark.asyncio
    async def test_full_sop_pipeline(self):
        """端到端: ingest → 绑定 → router SKILL→root → skill 可语义召回"""
        import math

        class MockEmbeddingClient:
            async def embed_one(self, text):
                vec = [0.0] * 8
                for i, c in enumerate(text):
                    vec[i % 8] += ord(c) / 100.0
                norm = math.sqrt(sum(x * x for x in vec))
                return [x / norm for x in vec] if norm > 0 else vec

        tool_store = ToolStore(db_path=":memory:")
        await tool_store.initialize()
        await _seed_tool_versions(tool_store)

        skill_store = SkillStore(
            db_path=":memory:",
            embedding_client=MockEmbeddingClient(), embedding_dim=8,
        )
        await skill_store.initialize()
        await SkillIngestor(skill_store).ingest(_SKILL_DOC)

        # Skill 语义召回 → 结果携带绑定 → router 解析 root
        results = await skill_store.search_cone("邮件", top_k=3, min_score=0.0)
        assert results and results[0].bound_tool_name == "send_email"

        router = CallPathRouter(tool_store, skill_store=skill_store)
        entry = await router.resolve(results[0].bound_tool_name, CallPathSource.SKILL)
        assert entry.version == "1.0.0"
        await tool_store.close()
        await skill_store.close()
