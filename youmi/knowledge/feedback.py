"""
FeedbackCollector — 人工反馈收集与阈值联动

人工反馈回写全局经验 (在线自进化闭环的人工信号源):
- record_feedback(): 记录用户对工具使用的正/负反馈 → GlobalMemory
- tools_over_threshold(): 负反馈累计达到阈值的工具列表，
  由上层 (MasterAgent / PostTaskPipeline / CLI) 决定是否
  提交 ToolGuardian 生成 ToolIssueReport 触发描述修复

用法::

    from youmi.knowledge.feedback import FeedbackCollector

    collector = FeedbackCollector(memory, negative_threshold=3)
    await collector.record_feedback(
        "unit_convert", "换算汇率时结果完全错误", rating="negative",
        task_id="task_042",
    )
    pending = await collector.tools_over_threshold()
    # pending: [{"tool_name": "unit_convert", "negative_count": 3, ...}]
"""

from __future__ import annotations

import logging

from youmi.knowledge.global_memory import GlobalMemory
from youmi.knowledge.models import KnowledgeCategory, KnowledgeEntry

logger = logging.getLogger(__name__)


class FeedbackCollector:
    """人工反馈收集器 — 反馈写入 + 负反馈阈值信号"""

    def __init__(
        self,
        memory: GlobalMemory,
        negative_threshold: int = 3,
    ) -> None:
        """
        Args:
            memory: GlobalMemory 实例 (已 initialize)
            negative_threshold: 负反馈累计阈值，达到后进入
                tools_over_threshold() 的待修复列表
        """
        self._memory = memory
        self._negative_threshold = negative_threshold

    async def record_feedback(
        self,
        tool_name: str,
        feedback: str,
        rating: str = "positive",
        task_id: str = "",
        agent_id: str = "",
    ) -> KnowledgeEntry:
        """记录一条人工反馈并写入全局记忆

        Args:
            tool_name: 关联工具名
            feedback: 反馈内容
            rating: "positive" | "negative"
            task_id / agent_id: 来源标识

        Returns:
            写入后的 KnowledgeEntry
        """
        if rating not in ("positive", "negative"):
            raise ValueError(f"rating 必须是 positive/negative, got: {rating!r}")

        entry = await self._memory.add_feedback(
            tool_name=tool_name,
            feedback=feedback,
            rating=rating,
            source_task_id=task_id,
            source_agent_id=agent_id,
        )
        logger.info(
            "FeedbackCollector: recorded %s feedback for '%s' (entry=%s)",
            rating, tool_name, entry.entry_id,
        )
        return entry

    async def negative_feedback_count(self, tool_name: str) -> int:
        """统计某工具未处理的负反馈条数"""
        entries = await self._memory.list_entries(
            tool_name=tool_name,
            category=KnowledgeCategory.HUMAN_FEEDBACK,
            limit=500,
        )
        return sum(
            1 for e in entries
            if (e.metadata or {}).get("rating") == "negative" and not e.resolved
        )

    async def tools_over_threshold(self) -> list[dict]:
        """返回负反馈达到阈值的工具 (应触发 ToolGuardian 修复)

        Returns:
            [{"tool_name", "negative_count", "suggestion"}] — negative_count
            降序。上层可将 suggestion 作为 ToolIssueReport 的
            error_message 提交给 ToolGuardian。
        """
        entries = await self._memory.list_entries(
            category=KnowledgeCategory.HUMAN_FEEDBACK,
            limit=1000,
        )
        counts: dict[str, int] = {}
        for e in entries:
            if (e.metadata or {}).get("rating") == "negative" and not e.resolved:
                counts[e.tool_name] = counts.get(e.tool_name, 0) + 1

        result = [
            {
                "tool_name": name,
                "negative_count": count,
                "suggestion": f"工具 '{name}' 累计收到 {count} 条未处理负反馈，"
                              "建议生成问题汇报并修正工具描述",
            }
            for name, count in counts.items()
            if count >= self._negative_threshold
        ]
        result.sort(key=lambda x: x["negative_count"], reverse=True)
        return result

    async def recent_feedback(self, limit: int = 50) -> list[KnowledgeEntry]:
        """最近的人工反馈条目"""
        return await self._memory.list_entries(
            category=KnowledgeCategory.HUMAN_FEEDBACK,
            limit=limit,
        )


__all__ = ["FeedbackCollector"]
