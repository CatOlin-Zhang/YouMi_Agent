"""
PlanMemory — WorkflowPlan 记忆复用层

将成功执行的 WorkflowPlan 持久化到独立 SQLite 表，
下次相似任务命中时直接复用骨架（角色/依赖/工具白名单），
只需微调各步骤的 task 文本，不重复 LLM 全量规划。

存储：独立 SQLite 文件（默认 .youmi_plans.db），与 GlobalMemory 解耦。

用法::

    from youmi.coordinator.plan_memory import PlanMemory
    from youmi.coordinator.plan import WorkflowPlan

    memory = PlanMemory(db_path=".youmi_plans.db", embedding_client=embedder)
    await memory.initialize()

    # 保存执行成功的 Plan
    await memory.save_plan(user_task, plan, success=True)

    # 语义检索相似任务（返回候选列表，按相似度降序）
    candidates = await memory.search_plan(user_task, top_k=3)
    for plan, similarity in candidates:
        print(f"similarity={similarity:.3f}: {plan.name}")

    await memory.close()
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from youmi.coordinator.plan import WorkflowPlan

if TYPE_CHECKING:
    from youmi.llm.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 建表 SQL
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS workflow_plans (
    plan_id TEXT PRIMARY KEY,
    task_fingerprint TEXT NOT NULL,
    task_text TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    success INTEGER DEFAULT 0,
    exec_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plans_fp ON workflow_plans(task_fingerprint);
CREATE INDEX IF NOT EXISTS idx_plans_success ON workflow_plans(success);
CREATE INDEX IF NOT EXISTS idx_plans_updated ON workflow_plans(updated_at DESC);

CREATE TABLE IF NOT EXISTS plan_vectors (
    plan_id TEXT PRIMARY KEY REFERENCES workflow_plans(plan_id) ON DELETE CASCADE,
    embedding_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算余弦相似度（纯 Python 实现，与 GlobalMemory 保持一致）"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _task_fingerprint(task: str) -> str:
    """生成任务文本的 MD5 指纹（用于精确匹配快速路径）"""
    return hashlib.md5(task.strip().encode("utf-8")).hexdigest()


def _keyword_score(text: str, query: str) -> float:
    """简单关键词相似度（词袋模型，降级策略）"""
    text_tokens = set(text.lower().split())
    query_tokens = set(query.lower().split())
    if not query_tokens:
        return 0.0
    # 中文字符按字拆分
    for ch in query:
        if "\u4e00" <= ch <= "\u9fff":
            query_tokens.add(ch)
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            text_tokens.add(ch)
    intersection = text_tokens & query_tokens
    return len(intersection) / len(query_tokens)


# ---------------------------------------------------------------------------
# PlanMemory
# ---------------------------------------------------------------------------

class PlanMemory:
    """WorkflowPlan 记忆复用层

    Args:
        db_path: SQLite 文件路径（默认 .youmi_plans.db）
        embedding_client: 向量化客户端（可选），有则走余弦相似度，否则降级关键词匹配
        similarity_threshold: 命中阈值（默认 0.85），低于此值不复用
    """

    def __init__(
        self,
        db_path: str = ".youmi_plans.db",
        embedding_client: EmbeddingClient | None = None,
        similarity_threshold: float = 0.85,
    ) -> None:
        self._db_path = db_path
        self._embedding_client = embedding_client
        self._similarity_threshold = similarity_threshold
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -----------------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------------

    async def initialize(self) -> None:
        """建库建表（幂等）"""
        def _init() -> None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.executescript(_CREATE_TABLES_SQL)
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(_init)
        logger.info("PlanMemory initialized: db_path=%s", self._db_path)

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    # -----------------------------------------------------------------------
    # 写入
    # -----------------------------------------------------------------------

    async def save_plan(
        self,
        user_task: str,
        plan: WorkflowPlan,
        success: bool,
    ) -> str:
        """保存 WorkflowPlan

        若相同任务指纹已存在，更新 exec_count 和 success 标志；
        否则插入新记录并尝试向量化。

        Args:
            user_task: 原始用户任务文本
            plan: 执行的 WorkflowPlan
            success: 是否至少有一个步骤成功完成

        Returns:
            plan_id（新建或已有记录的 ID）
        """
        if self._conn is None:
            raise RuntimeError("PlanMemory not initialized. Call initialize() first.")

        fingerprint = _task_fingerprint(user_task)
        plan_json = plan.model_dump_json()
        now = datetime.now(timezone.utc).isoformat()

        def _upsert() -> str:
            # 检查指纹是否已存在
            row = self._conn.execute(
                "SELECT plan_id, exec_count FROM workflow_plans WHERE task_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row:
                pid, count = row
                self._conn.execute(
                    "UPDATE workflow_plans SET exec_count=?, success=?, plan_json=?, updated_at=? WHERE plan_id=?",
                    (count + 1, 1 if success else 0, plan_json, now, pid),
                )
                self._conn.commit()
                return pid
            else:
                pid = uuid.uuid4().hex[:16]
                self._conn.execute(
                    """INSERT INTO workflow_plans
                       (plan_id, task_fingerprint, task_text, plan_json, success, exec_count, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                    (pid, fingerprint, user_task[:2000], plan_json, 1 if success else 0, now, now),
                )
                self._conn.commit()
                return pid

        async with self._lock:
            plan_id = await asyncio.to_thread(_upsert)

        # 尝试向量化（失败不阻塞）
        if self._embedding_client is not None:
            try:
                embedding = await self._embedding_client.embed_one(user_task)
                await self._save_vector(plan_id, embedding)
            except Exception as exc:
                logger.warning("PlanMemory: failed to vectorize plan '%s': %s", plan_id, exc)

        logger.info(
            "PlanMemory: saved plan '%s' (success=%s task_len=%d)",
            plan_id, success, len(user_task),
        )
        return plan_id

    async def _save_vector(self, plan_id: str, embedding: list[float]) -> None:
        """写入或更新向量索引"""
        if self._conn is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        embedding_json = json.dumps(embedding)

        def _write() -> None:
            self._conn.execute(
                """INSERT INTO plan_vectors (plan_id, embedding_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(plan_id) DO UPDATE SET embedding_json=excluded.embedding_json, updated_at=excluded.updated_at""",
                (plan_id, embedding_json, now),
            )
            self._conn.commit()

        await asyncio.to_thread(_write)

    # -----------------------------------------------------------------------
    # 检索
    # -----------------------------------------------------------------------

    async def search_plan(
        self,
        user_task: str,
        top_k: int = 3,
    ) -> list[tuple[WorkflowPlan, float]]:
        """语义检索相似任务的 WorkflowPlan

        优先使用向量余弦相似度（需 embedding_client），
        否则降级为关键词相似度匹配。
        仅返回相似度 >= similarity_threshold 的候选，按相似度降序排列。

        Args:
            user_task: 查询任务文本
            top_k: 最多返回候选数量

        Returns:
            [(WorkflowPlan, similarity), ...] 按相似度降序
        """
        if self._conn is None:
            return []

        # 向量路径
        if self._embedding_client is not None:
            try:
                query_vec = await self._embedding_client.embed_one(user_task)
                return await self._search_by_vector(query_vec, user_task, top_k)
            except Exception as exc:
                logger.warning("PlanMemory: vector search failed (%s), falling back to keyword", exc)

        # 关键词降级路径
        return await self._search_by_keyword(user_task, top_k)

    async def _search_by_vector(
        self,
        query_vec: list[float],
        user_task: str,
        top_k: int,
    ) -> list[tuple[WorkflowPlan, float]]:
        """向量余弦相似度检索"""
        def _load_all() -> list[tuple[str, str, list[float]]]:
            rows = self._conn.execute(
                """SELECT wp.plan_id, wp.plan_json, pv.embedding_json
                   FROM workflow_plans wp
                   JOIN plan_vectors pv ON wp.plan_id = pv.plan_id
                   WHERE wp.success = 1
                   ORDER BY wp.updated_at DESC
                   LIMIT 200"""
            ).fetchall()
            result = []
            for pid, plan_json, emb_json in rows:
                try:
                    emb = json.loads(emb_json)
                    result.append((pid, plan_json, emb))
                except Exception:
                    pass
            return result

        rows = await asyncio.to_thread(_load_all)
        scored: list[tuple[WorkflowPlan, float]] = []
        for pid, plan_json, emb in rows:
            sim = _cosine_similarity(query_vec, emb)
            if sim >= self._similarity_threshold:
                try:
                    plan = WorkflowPlan.model_validate_json(plan_json)
                    scored.append((plan, sim))
                except Exception as exc:
                    logger.warning("PlanMemory: failed to parse plan '%s': %s", pid, exc)

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    async def _search_by_keyword(
        self,
        user_task: str,
        top_k: int,
    ) -> list[tuple[WorkflowPlan, float]]:
        """关键词相似度检索（降级策略）"""
        def _load_all() -> list[tuple[str, str, str]]:
            return self._conn.execute(
                """SELECT plan_id, task_text, plan_json
                   FROM workflow_plans
                   WHERE success = 1
                   ORDER BY updated_at DESC
                   LIMIT 100"""
            ).fetchall()

        rows = await asyncio.to_thread(_load_all)
        scored: list[tuple[WorkflowPlan, float]] = []
        for pid, task_text, plan_json in rows:
            sim = _keyword_score(task_text, user_task)
            if sim >= self._similarity_threshold:
                try:
                    plan = WorkflowPlan.model_validate_json(plan_json)
                    scored.append((plan, sim))
                except Exception as exc:
                    logger.warning("PlanMemory: failed to parse plan '%s': %s", pid, exc)

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # -----------------------------------------------------------------------
    # 统计
    # -----------------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        """返回存储统计信息"""
        if self._conn is None:
            return {}

        def _query() -> dict[str, Any]:
            total = self._conn.execute("SELECT COUNT(*) FROM workflow_plans").fetchone()[0]
            success = self._conn.execute(
                "SELECT COUNT(*) FROM workflow_plans WHERE success=1"
            ).fetchone()[0]
            vectorized = self._conn.execute("SELECT COUNT(*) FROM plan_vectors").fetchone()[0]
            return {"total": total, "success": success, "vectorized": vectorized}

        return await asyncio.to_thread(_query)


__all__ = ["PlanMemory"]
