"""
上下文压缩引擎 (ContextCompactor)

参考 OpenClaw 的 compaction 机制，在对话上下文接近模型 token 上限时
自动将旧消息摘要化，释放 token 空间，防止长对话崩溃。

核心流程:
1. Agent._observe() 在每次 ReAct 迭代前调用 compactor
2. compactor 估算当前 conversation 的 token 数
3. 如果超出预算 (max_context_tokens * compaction_reserve_ratio)，触发压缩
4. 压缩: 保留最近 keep_recent 条消息，将更早的消息通过 LLM 摘要压缩
5. 摘要作为 system 消息插入 conversation 头部
6. 压缩失败时 fallback 到硬截断 (丢弃最早的消息)

设计要点:
- token 估算使用字符数 / 3.5 的近似公式 (中英文混合场景)
- 支持 before_compaction / after_compaction 钩子
- 压缩摘要可增量合并 (旧摘要 + 新增对话 → 新摘要)
- 无 LLM 时退化为硬截断
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

# LLM 调用函数签名: async def(messages) -> str
LLMCallFn = Callable[[list[dict[str, str]]], Awaitable[str]]

# 压缩提示词 (参考 OpenClaw compact 功能)
DEFAULT_COMPACTION_PROMPT = """\
请将以下对话历史压缩为一段简洁的摘要，保留关键信息、用户意图、重要结论和工具调用结果。
去除冗余的中间过程和重复内容。摘要长度不超过原文的 30%。
使用与对话相同的语言。

{previous_summary}

对话历史:
{conversation}

请直接输出摘要内容:"""


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数量

    使用字符数 / 3.5 的近似公式，适用于中英文混合场景。
    - 纯英文约 4 字符/token
    - 纯中文约 1.5-2 字符/token
    - 混合场景约 3-3.5 字符/token

    Args:
        text: 待估算的文本

    Returns:
        估算的 token 数
    """
    if not text:
        return 0
    return max(1, int(len(text) / 3.5))


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """估算一组消息的总 token 数

    Args:
        messages: OpenAI 格式的消息列表

    Returns:
        估算的总 token 数
    """
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if content:
            total += estimate_tokens(content)
        # role 和 metadata 开销约 4 tokens/消息
        total += 4
        # tool_calls 额外开销
        if "tool_calls" in msg:
            total += estimate_tokens(str(msg["tool_calls"]))
    return total


class ContextCompactor:
    """上下文压缩器

    管理对话的 token 预算，在接近上限时自动触发压缩。

    Args:
        max_context_tokens: 上下文 token 预算 (0 表示不限制)
        reserve_ratio: 触发压缩的阈值比例 (0.0~1.0)。
            当已用 token 达到 max_context_tokens * reserve_ratio 时触发。
        keep_recent: 压缩时保留的最近消息条数
        llm_call: LLM 调用函数，用于生成摘要。None 则退化为硬截断。
        compaction_prompt: 压缩提示词模板

    用法::

        compactor = ContextCompactor(
            max_context_tokens=8000,
            reserve_ratio=0.8,
            keep_recent=10,
            llm_call=my_llm_fn,
        )

        # 在 Agent._observe() 中调用
        conversation = await compactor.maybe_compact(conversation)
    """

    def __init__(
        self,
        max_context_tokens: int = 8000,
        reserve_ratio: float = 0.8,
        keep_recent: int = 10,
        llm_call: LLMCallFn | None = None,
        compaction_prompt: str = DEFAULT_COMPACTION_PROMPT,
    ) -> None:
        self._max_tokens = max_context_tokens
        self._reserve_ratio = reserve_ratio
        self._keep_recent = keep_recent
        self._llm_call = llm_call
        self._prompt = compaction_prompt

        # 当前压缩摘要 (跨多次压缩增量合并)
        self._compaction_summary: str | None = None
        # 统计
        self._compaction_count: int = 0
        self._total_tokens_compacted: int = 0

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def compaction_count(self) -> int:
        """已执行的压缩次数"""
        return self._compaction_count

    @property
    def current_summary(self) -> str | None:
        """当前压缩摘要"""
        return self._compaction_summary

    def needs_compaction(self, messages: list[dict[str, Any]]) -> bool:
        """判断是否需要压缩

        Args:
            messages: 当前 conversation 消息列表

        Returns:
            True 表示已用 token 达到阈值，需要压缩
        """
        if self._max_tokens <= 0:
            return False
        tokens = estimate_messages_tokens(messages)
        threshold = int(self._max_tokens * self._reserve_ratio)
        return tokens >= threshold

    async def maybe_compact(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """检查并按需压缩上下文

        如果不需要压缩，直接返回原消息列表。
        如果需要压缩:
        1. 将 conversation 拆分为 [待压缩部分] + [保留的最近消息]
        2. 用 LLM 对 [待压缩部分] 生成摘要 (合并旧摘要)
        3. 返回 [摘要 system 消息] + [保留的最近消息]
        4. LLM 不可用时 fallback 到硬截断

        Args:
            messages: 当前 conversation 消息列表 (含 system/user/assistant/tool)

        Returns:
            压缩后的消息列表
        """
        if not self.needs_compaction(messages):
            return messages

        logger.info(
            "Compaction triggered: %d messages, ~%d tokens (budget=%d, threshold=%.0f%%)",
            len(messages),
            estimate_messages_tokens(messages),
            self._max_tokens,
            self._reserve_ratio * 100,
        )

        # 拆分: system 消息单独保留，其余按 keep_recent 拆分
        system_msgs: list[dict[str, Any]] = []
        non_system_msgs: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_msgs.append(msg)
            else:
                non_system_msgs.append(msg)

        if len(non_system_msgs) <= self._keep_recent:
            # 消息太少，无法压缩，只能截断 system
            return messages

        to_compact = non_system_msgs[:-self._keep_recent]
        to_keep = non_system_msgs[-self._keep_recent:]

        # 计算压缩前的 token 数
        compacted_tokens = estimate_messages_tokens(to_compact)

        # 尝试 LLM 摘要压缩
        summary = await self._generate_compaction_summary(to_compact)

        if summary is not None:
            # 成功: 用摘要替代旧消息
            self._compaction_summary = summary
            self._compaction_count += 1
            self._total_tokens_compacted += compacted_tokens

            logger.info(
                "Compaction #%d: %d messages (~%d tokens) → summary (%d tokens)",
                self._compaction_count,
                len(to_compact),
                compacted_tokens,
                estimate_tokens(summary),
            )

            # 构造压缩后的消息列表
            compacted: list[dict[str, Any]] = list(system_msgs)
            compacted.append({
                "role": "system",
                "content": f"[历史对话压缩摘要]\n{summary}",
            })
            compacted.extend(to_keep)
            return compacted
        else:
            # LLM 不可用或失败: fallback 到硬截断
            logger.warning(
                "LLM compaction failed, falling back to hard truncation "
                "(dropping %d oldest messages)",
                len(to_compact),
            )
            self._compaction_count += 1

            truncated: list[dict[str, Any]] = list(system_msgs)
            truncated.extend(to_keep)
            return truncated

    async def _generate_compaction_summary(
        self,
        messages: list[dict[str, Any]],
    ) -> str | None:
        """调用 LLM 生成压缩摘要

        Args:
            messages: 需要压缩的消息列表

        Returns:
            摘要文本，LLM 不可用或调用失败时返回 None
        """
        if self._llm_call is None:
            return None

        # 构造对话文本
        conversation_text = "\n".join(
            f"{msg.get('role', '?')}: {msg.get('content', '')}"
            for msg in messages
            if msg.get("content")  # 跳过无内容的 tool 消息
        )

        # 如果有旧摘要，合并进去
        previous_section = ""
        if self._compaction_summary:
            previous_section = f"之前的摘要:\n{self._compaction_summary}\n\n"

        prompt = self._prompt.format(
            previous_summary=previous_section,
            conversation=conversation_text,
        )

        try:
            result = await self._llm_call([{"role": "user", "content": prompt}])
            return result.strip() if result else None
        except Exception as exc:
            logger.error("Compaction LLM call failed: %s", exc)
            return None

    def reset(self) -> None:
        """重置压缩状态"""
        self._compaction_summary = None
        self._compaction_count = 0
        self._total_tokens_compacted = 0

    def snapshot(self) -> dict[str, Any]:
        """返回压缩器状态快照"""
        return {
            "max_tokens": self._max_tokens,
            "reserve_ratio": self._reserve_ratio,
            "keep_recent": self._keep_recent,
            "compaction_count": self._compaction_count,
            "total_tokens_compacted": self._total_tokens_compacted,
            "has_summary": self._compaction_summary is not None,
            "summary_tokens": estimate_tokens(self._compaction_summary) if self._compaction_summary else 0,
        }
