"""
ToolExperienceExtractor — 工具经验提取器

从 Agent 对话记录中提取语义化的工具使用经验，供 GlobalMemory 沉淀。
相比 PostTaskPipeline 的简单成功/失败统计，本提取器增加:
- LLM 辅助分析 (可选): 分析失败原因、生成修复建议
- 规则降级提取: 无 LLM 时通过错误关键词匹配 + 模板生成经验描述

用法::

    from youmi.knowledge import ToolExperienceExtractor

    extractor = ToolExperienceExtractor()                    # 规则模式
    extractor = ToolExperienceExtractor(llm_call=my_llm_fn)  # LLM 增强模式

    experiences = extractor.extract(conversation, "file_read")
    failures = extractor.analyze_failures(["错误1", "错误2"], "file_read")
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Awaitable

from youmi.knowledge.models import KnowledgeCategory
from youmi.coordinator.post_task import ToolExperience

logger = logging.getLogger(__name__)

# LLM 调用函数类型: (system_prompt, user_prompt) -> str
LLMCallFn = Callable[[str, str], Awaitable[str]]

# ---------------------------------------------------------------------------
# 错误分类规则 (规则模式降级使用)
# ---------------------------------------------------------------------------

_ERROR_RULES: list[tuple[str, str, str]] = [
    # (关键词列表, 错误类别, 经验描述模板)
    (
        ("不存在", "not found", "no such file", "未找到"),
        "missing_target",
        "目标资源不存在: 调用前应先确认路径/目标有效",
    ),
    (
        ("权限", "permission", "denied", "forbidden", "拒绝访问"),
        "permission",
        "权限不足: 需要更高权限或调整白名单配置",
    ),
    (
        ("超时", "timeout", "timed out"),
        "timeout",
        "执行超时: 目标操作耗时过长，考虑增大 timeout 参数或拆分任务",
    ),
    (
        ("参数", "argument", "parameter", "invalid", "类型错误", "type error"),
        "invalid_params",
        "参数不合法: 检查参数类型与格式是否与工具定义匹配",
    ),
    (
        ("编码", "encoding", "unicode", "乱码"),
        "encoding",
        "编码问题: 内容含非 UTF-8 字符，需显式指定编码",
    ),
    (
        ("网络", "network", "connection", "连接", "unreachable"),
        "network",
        "网络问题: 目标服务不可达，检查网络或重试",
    ),
]


class ToolExperienceExtractor:
    """工具经验提取器

    Args:
        llm_call: LLM 调用函数 (system_prompt, user_prompt) -> str。
            None 时使用规则模式 (关键词匹配 + 模板生成)。
        max_samples: 每个工具最多采样的成功/失败消息数
    """

    def __init__(
        self,
        llm_call: LLMCallFn | None = None,
        max_samples: int = 3,
    ) -> None:
        self._llm_call = llm_call
        self._max_samples = max_samples

    @property
    def llm_enabled(self) -> bool:
        """是否启用 LLM 增强分析"""
        return self._llm_call is not None

    # ------------------------------------------------------------------
    # 对话记录解析
    # ------------------------------------------------------------------

    def extract(
        self,
        conversation: list[dict[str, Any]],
        tool_name: str,
    ) -> ToolExperience:
        """从单个 Agent 对话记录中提取指定工具的使用经验

        Args:
            conversation: OpenAI messages 格式对话记录
            tool_name: 工具名称

        Returns:
            ToolExperience 统计结果
        """
        stats = {
            "success": 0,
            "failure": 0,
            "failures": [],
            "successes": [],
        }

        for msg in conversation:
            if msg.get("role") != "tool":
                continue
            tool_call_id = msg.get("tool_call_id", "")
            if not self._match_tool_call(conversation, tool_call_id, tool_name):
                continue

            content = msg.get("content", "") or ""
            if self._is_error(content):
                stats["failure"] += 1
                if len(stats["failures"]) < self._max_samples:
                    stats["failures"].append(content[:200])
            else:
                stats["success"] += 1
                if len(stats["successes"]) < self._max_samples:
                    stats["successes"].append(content[:100])

        total = stats["success"] + stats["failure"]
        return ToolExperience(
            tool_name=tool_name,
            success_patterns=stats["successes"],
            failure_patterns=stats["failures"],
            usage_count=total,
            success_rate=stats["success"] / total if total > 0 else 0.0,
        )

    @staticmethod
    def _match_tool_call(
        conversation: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
    ) -> bool:
        """判断 tool_call_id 是否属于指定工具"""
        if not tool_call_id:
            return False
        for msg in reversed(conversation):
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls", []) or []:
                if tc.get("id") == tool_call_id:
                    return tc.get("function", {}).get("name", "") == tool_name
        return False

    @staticmethod
    def _is_error(content: str) -> bool:
        """判断工具返回内容是否为错误

        约定: JSON 格式且含 "error" 字段视为失败，
        非 JSON 内容视为成功 (与 PostTaskPipeline 逻辑一致)。
        """
        try:
            data = json.loads(content)
            return isinstance(data, dict) and "error" in data
        except (json.JSONDecodeError, TypeError):
            return False

    # ------------------------------------------------------------------
    # 失败分析 → 经验描述
    # ------------------------------------------------------------------

    async def analyze_failures(
        self,
        failure_messages: list[str],
        tool_name: str,
    ) -> list[str]:
        """分析失败消息，生成结构化的经验描述

        LLM 模式: 将失败消息交给 LLM 归纳根因与修复建议。
        规则模式: 关键词匹配错误类别，套用模板生成描述。

        Args:
            failure_messages: 失败消息列表
            tool_name: 工具名称

        Returns:
            经验描述列表 (每条为一段独立文本)
        """
        if not failure_messages:
            return []

        if self._llm_call is not None:
            return await self._analyze_with_llm(failure_messages, tool_name)
        return self._analyze_with_rules(failure_messages, tool_name)

    async def _analyze_with_llm(
        self,
        failure_messages: list[str],
        tool_name: str,
    ) -> list[str]:
        """LLM 增强分析"""
        system_prompt = (
            "你是工具质量分析专家。根据工具调用失败记录，"
            "归纳该工具的失败根因和修复建议。"
            "每条输出格式: [根因] 问题描述 | [建议] 修复方向。"
            "最多输出 3 条，每条一行，不要多余解释。"
        )
        user_prompt = (
            f"工具名: {tool_name}\n\n"
            f"失败记录:\n"
            + "\n---\n".join(failure_messages[:5])
        )

        try:
            response = await self._llm_call(system_prompt, user_prompt)
            lines = [
                line.strip() for line in response.strip().splitlines()
                if line.strip()
            ]
            return lines[:3]
        except Exception as exc:
            logger.warning(
                "ToolExperienceExtractor: LLM analysis failed for '%s': %s",
                tool_name, exc,
            )
            return self._analyze_with_rules(failure_messages, tool_name)

    def _analyze_with_rules(
        self,
        failure_messages: list[str],
        tool_name: str,
    ) -> list[str]:
        """规则模式分析: 关键词匹配 + 模板生成"""
        results: list[str] = []
        seen_categories: set[str] = set()

        for message in failure_messages:
            message_lower = message.lower()
            for keywords, category, template in _ERROR_RULES:
                if any(kw in message_lower for kw in keywords):
                    if category not in seen_categories:
                        seen_categories.add(category)
                        results.append(
                            f"工具 {tool_name} {template} "
                            f"(示例错误: {message[:80]})"
                        )
                    break  # 每条消息只归入一个类别

        if not results:
            # 未匹配任何规则，输出通用经验
            results.append(
                f"工具 {tool_name} 存在未分类的调用失败 "
                f"(示例错误: {failure_messages[0][:80]})"
            )
        return results[:3]

    # ------------------------------------------------------------------
    # 经验 → KnowledgeEntry 内容
    # ------------------------------------------------------------------

    @staticmethod
    def to_experience_content(
        experience: ToolExperience,
    ) -> str:
        """将 ToolExperience 转为适合存入 GlobalMemory 的经验文本

        Args:
            experience: 工具经验统计

        Returns:
            结构化经验描述
        """
        lines = [
            f"工具 {experience.tool_name}: "
            f"共调用 {experience.usage_count} 次, "
            f"成功率 {experience.success_rate:.0%}",
        ]
        if experience.success_patterns:
            lines.append(
                "成功模式: " + "; ".join(experience.success_patterns[:3]),
            )
        if experience.failure_patterns:
            lines.append(
                "失败模式: " + "; ".join(experience.failure_patterns[:3]),
            )
        if experience.boundary_notes:
            lines.append(
                "边界条件: " + "; ".join(experience.boundary_notes[:3]),
            )
        return "\n".join(lines)


__all__ = ["ToolExperienceExtractor", "LLMCallFn"]
