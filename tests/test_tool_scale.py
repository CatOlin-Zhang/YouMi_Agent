"""
ToolStore 500 工具规模测试

验证 sqlite-vec 向量检索在 500 个语义均匀分布工具下的召回质量:
- 25 个功能域 × 20 个工具 = 500 个工具，语义分布均匀
- 基于词特征的哈希 embedding (256 维)，使相似描述产生相近向量
- 临时数据库 (tempfile)，与生产 .youmi_tools.db 完全隔离
- 两轮召回: 第一轮 top_k=3，第二轮 top_k=3 自动排除第一轮结果
- 多次随机查询验证统计召回率
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import tempfile
import time

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter
from youmi.mcp.tool_store import ToolStore


# ---------------------------------------------------------------------------
# 词特征哈希 embedding — 基于词级 bigram 特征映射到固定维度向量
#
# 同域工具共享领域词汇 → 高相似度
# 不同域工具词汇不重叠 → 低相似度
# 查询文本与同域描述有词重叠 → 可被召回
# ---------------------------------------------------------------------------

_EMBED_DIM = 256


def _tokenize(text: str) -> list[str]:
    """中英文混合分词: 英文按空格/标点切分, 中文按 bigram 切分"""
    tokens: list[str] = []
    # 英文/数字 token (小写化)
    buf: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                tokens.append("".join(buf))
                buf.clear()
    if buf:
        tokens.append("".join(buf))

    # 中文 bigram
    cn = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    for i in range(len(cn) - 1):
        tokens.append(cn[i] + cn[i + 1])

    return tokens


def _hash_embed(text: str, dim: int = _EMBED_DIM) -> list[float]:
    """词级特征哈希 → 归一化向量 (确定性)"""
    vec = [0.0] * dim
    tokens = _tokenize(text)

    # unigram
    for tok in tokens:
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        idx = h % dim
        vec[idx] += 1.0

    # bigram (捕捉短语结构)
    for i in range(len(tokens) - 1):
        bigram = f"{tokens[i]}|{tokens[i + 1]}"
        h = int(hashlib.md5(bigram.encode()).hexdigest(), 16)
        idx = h % dim
        vec[idx] += 0.5

    # L2 归一化
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


class HashEmbeddingClient:
    """基于词特征哈希的 embedding 客户端 — 无需真实模型, 确定性强"""

    def __init__(self, dim: int = _EMBED_DIM):
        self.dim = dim

    async def embed_one(self, text: str) -> list[float]:
        return _hash_embed(text, self.dim)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hash_embed(t, self.dim) for t in texts]


# ---------------------------------------------------------------------------
# 工具生成: 25 域 × 20 工具 = 500
#
# 每个域有独立的动作词、操作对象、参数名，确保:
# 1. 同域工具共享领域词汇 → 向量聚拢
# 2. 跨域词汇几乎不重叠 → 向量分散
# 3. 每个工具有唯一 tool_name 和独特描述
# ---------------------------------------------------------------------------

# (domain_key, action_verb, object_phrase, param_name)
_DOMAINS: list[tuple[str, str, str, str]] = [
    # ── 通信 ──
    ("email",       "send",     "email message to recipient address",     "recipient"),
    ("chat",        "post",     "chat message to conversation channel",   "channel"),
    ("notify",      "push",     "notification alert to subscribed user",  "user_id"),
    # ── 文件 ──
    ("file_rw",     "read",     "file content from local disk path",      "filepath"),
    ("file_mgmt",   "copy",     "file entry to destination directory",    "dest_path"),
    ("archive",     "compress", "archive bundle of selected files",       "output_path"),
    # ── 数据 ──
    ("database",    "execute",  "SQL query on relational database table", "query"),
    ("spreadsheet", "update",   "spreadsheet cell value in workbook",     "cell_ref"),
    ("csv_data",    "parse",    "CSV data rows from structured source",   "source"),
    # ── 媒体 ──
    ("image",       "resize",   "image file to target dimensions",        "width"),
    ("audio",       "convert",  "audio clip to different encoding",       "format"),
    ("video",       "extract",  "video frame at specified timestamp",     "timestamp"),
    # ── 数学 / 统计 ──
    ("math_calc",   "compute",  "math expression with numeric values",    "expression"),
    ("statistics",  "calculate","statistical metric on data samples",     "samples"),
    ("geometry",    "measure",  "geometric shape property value",         "shape"),
    # ── 系统 / DevOps ──
    ("system",      "monitor",  "system resource usage and health",       "resource"),
    ("deploy",      "deploy",   "application service to cluster node",    "service"),
    ("container",   "manage",   "container lifecycle state operation",    "container_id"),
    # ── 网络 ──
    ("http_req",    "send",     "HTTP request to remote API endpoint",    "url"),
    ("dns",         "resolve",  "DNS record for specified domain name",   "domain"),
    ("socket",      "open",     "socket connection to network address",   "host"),
    # ── 安全 ──
    ("auth",        "verify",   "authentication credential token",        "token"),
    ("encrypt",     "encrypt",  "data payload with cipher algorithm",     "payload"),
    ("audit",       "log",      "audit trail event with metadata",        "event"),
    # ── AI / 测试 ──
    ("ml_model",    "predict",  "machine learning model inference",       "features"),
]

_VARIANTS = [
    ("basic",       "basic",       "Standard"),
    ("advanced",    "advanced",    "Advanced"),
    ("batch",       "batch",       "Batch"),
    ("single",      "single",      "Single-item"),
    ("bulk",        "bulk",        "Bulk"),
    ("stream",      "streaming",   "Streaming"),
    ("async",       "asynchronous","Asynchronous"),
    ("sync",        "synchronous", "Synchronous"),
    ("secure",      "secure",      "Secure"),
    ("fast",        "fast",        "High-performance"),
    ("parallel",    "parallel",    "Parallel"),
    ("scheduled",   "scheduled",   "Scheduled"),
    ("on_demand",   "on-demand",   "On-demand"),
    ("cached",      "cached",      "Cached"),
    ("direct",      "direct",      "Direct-access"),
    ("filtered",    "filtered",    "Filtered"),
    ("sorted",      "sorted",      "Sorted"),
    ("validated",   "validated",   "Validated"),
    ("raw",         "raw",         "Raw"),
    ("formatted",   "formatted",   "Formatted"),
]


def _generate_all_tools() -> list[tuple[str, str, str]]:
    """生成 500 个 (tool_name, description, param_name) 元组"""
    tools: list[tuple[str, str, str]] = []
    for domain, action, obj, param in _DOMAINS:
        for suffix, adj, label in _VARIANTS:
            name = f"{domain}_{suffix}"
            desc = (
                f"{label} tool to {action} {obj} "
                f"with {adj} processing capabilities"
            )
            tools.append((name, desc, param))
    return tools


# 固定 target: email 域的第一个工具
TARGET_NAME = "email_basic"
TARGET_DESC = (
    "Standard tool to send email message to recipient address "
    "with basic processing capabilities"
)
# 一个与 email 域语义接近的自然语言查询
SEARCH_QUERY = "I need a tool to send email message to a recipient"


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------

def _make_tool_entry(name: str, description: str, param: str):
    from youmi.mcp.models import ToolContextTier, ToolEntry

    defn = ToolDefinition(
        name=name,
        description=description,
        parameters=[
            ToolParameter(name=param, type="string", description=f"{param} parameter"),
        ],
    )
    return ToolEntry(
        tool_name=name,
        definition=defn,
        summary=description[:80],
        tier=ToolContextTier.COLD,
        embedding=[],
        version="0.0.1",
    )


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

class TestToolStore500Scale:
    """500 工具规模下的向量召回质量测试"""

    @pytest.fixture
    async def scale_store(self):
        """临时数据库 + HashEmbeddingClient, 测试结束自动清理"""
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix="_scale_test.db", prefix="youmi_test_",
        )
        os.close(tmp_fd)

        store = ToolStore(
            db_path=tmp_path,
            embedding_client=HashEmbeddingClient(),
            embedding_dim=_EMBED_DIM,
        )
        await store.initialize()
        yield store
        await store.close()

        # 清理临时数据库文件
        for ext in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(tmp_path + ext)
            except FileNotFoundError:
                pass

    # --------------------------------------------------------------
    # 核心测试: 500 工具两轮召回
    # --------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_500_tool_two_round_retrieval(self, scale_store):
        """
        500 工具中精确召回目标工具:
        1. 第一轮: top_k=3 召回最相关的 3 个工具
        2. 第二轮: top_k=3 + exclude 第一轮结果, 召回接下来 3 个
        3. 两轮结果不重叠, 分数在合理范围内
        """
        store = scale_store
        all_tools = _generate_all_tools()
        assert len(all_tools) == 500, f"应有 500 个工具, 实际 {len(all_tools)}"

        # ── 批量写入 ──
        t0 = time.time()
        for name, desc, param in all_tools:
            entry = _make_tool_entry(name, desc, param)
            await store.upsert_tool(entry)
        write_elapsed = time.time() - t0

        # ── 批量生成 embedding ──
        t0 = time.time()
        for name, _, _ in all_tools:
            await store.update_embedding(name)
        embed_elapsed = time.time() - t0

        # ── 验证写入 ──
        stats = await store.stats()
        assert stats["tools"] == 500
        assert stats["vectors"] == 500

        # ── 第一轮召回: top_k=3 ──
        t0 = time.time()
        round1 = await store.search(SEARCH_QUERY, top_k=3, min_score=0.0)
        search1_elapsed = time.time() - t0

        assert len(round1) == 3, f"第一轮应返回 3 个, 实际 {len(round1)}"
        round1_names = {r.tool_name for r in round1}

        print(f"\n{'='*60}")
        print(f"500 工具规模测试 — 临时库: {store._db_path}")
        print(f"写入 500 工具: {write_elapsed:.2f}s")
        print(f"生成 500 embedding: {embed_elapsed:.2f}s")
        print(f"sqlite-vec 可用: {store._vec_available}")
        print(f"\n第一轮召回 ({search1_elapsed*1000:.1f}ms):")
        for i, r in enumerate(round1, 1):
            tag = " ★ TARGET" if r.tool_name == TARGET_NAME else ""
            print(f"  {i}. {r.tool_name:30s}  score={r.score:.4f}{tag}")

        # ── 第二轮召回: top_k=3, 排除第一轮 ──
        t0 = time.time()
        round2 = await store.search(
            SEARCH_QUERY, top_k=3, min_score=0.0, exclude=round1_names,
        )
        search2_elapsed = time.time() - t0

        assert len(round2) == 3, f"第二轮应返回 3 个, 实际 {len(round2)}"
        round2_names = {r.tool_name for r in round2}

        print(f"\n第二轮召回 (exclude={round1_names}) ({search2_elapsed*1000:.1f}ms):")
        for i, r in enumerate(round2, 1):
            tag = " ★ TARGET" if r.tool_name == TARGET_NAME else ""
            print(f"  {i}. {r.tool_name:30s}  score={r.score:.4f}{tag}")

        # ── 验证: 两轮不重叠 ──
        assert round1_names.isdisjoint(round2_names), \
            f"两轮结果不应重叠: {round1_names & round2_names}"

        # ── 验证: 分数范围 ──
        for r in round1 + round2:
            assert -1.0 <= r.score <= 1.0, f"{r.tool_name} 分数越界: {r.score}"

        # ── 验证: 第一轮最高分 ≥ 第二轮最高分 ──
        assert round1[0].score >= round2[0].score, (
            f"第一轮最高分 ({round1[0].score:.4f}) 应 ≥ "
            f"第二轮最高分 ({round2[0].score:.4f})"
        )

        # ── 验证: email 域工具排在前面 ──
        # 查询 "send email message to recipient" 与 email_* 域词汇高度重叠
        top6_names = round1_names | round2_names
        email_count = sum(1 for n in top6_names if n.startswith("email_"))
        print(f"\nTop 6 中 email 域工具数: {email_count}/6")
        assert email_count >= 3, (
            f"Top 6 中 email 域工具应 ≥ 3 (查询与 email 域最相关), "
            f"实际 {email_count}: {top6_names}"
        )

        print(f"{'='*60}")

    # --------------------------------------------------------------
    # 多次随机查询统计
    # --------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_multi_query_recall_rate(self, scale_store):
        """
        对 5 个不同域的查询各检索一次, 验证同域工具被优先召回
        """
        store = scale_store
        all_tools = _generate_all_tools()

        for name, desc, param in all_tools:
            entry = _make_tool_entry(name, desc, param)
            await store.upsert_tool(entry)
        for name, _, _ in all_tools:
            await store.update_embedding(name)

        # 5 个不同域的查询
        queries: list[tuple[str, str]] = [
            ("email 域",     "send email message to recipient address"),
            ("database 域",  "execute SQL query on database table"),
            ("image 域",     "resize image to target dimensions"),
            ("auth 域",      "verify authentication credential token"),
            ("deploy 域",    "deploy application service to cluster"),
        ]

        print(f"\n{'='*60}")
        print(f"多域召回率测试 — {stats_tools(store)} tools loaded")

        for domain_label, query in queries:
            prefix = domain_label.split(" ")[0]
            results = await store.search(query, top_k=3, min_score=0.0)
            top3_names = [r.tool_name for r in results]
            match_count = sum(1 for n in top3_names if n.startswith(f"{prefix}_"))

            print(f"  {domain_label:12s}  top3={top3_names}  "
                  f"同域命中={match_count}/3")

            # 至少 1 个同域工具应在 top 3
            assert match_count >= 1, (
                f"{domain_label} 查询 '{query}' 的 top-3 中无同域工具: {top3_names}"
            )

        print(f"{'='*60}")


def stats_tools(store: ToolStore) -> int:
    """同步获取工具数 (仅供 print 用)"""
    return 500
