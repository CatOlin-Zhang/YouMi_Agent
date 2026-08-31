"""
摘要记忆策略 (SummaryMemoryStrategy)

保留最近几轮完整对话，同时通过调用 LLM 对更早的对话历史生成摘要，
用一段精简的摘要文本替代冗长的历史记录。

适用于: 长对话场景，需要在上下文窗口限制内保留关键信息。
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from youmi.memory.strategies.base import MemoryStrategy

# LLM 调用函数签名: async def(messages) -> str
LLMCallFn = Callable[[list[dict[str, str]]], Awaitable[str]]

# 默认摘要提示词
DEFAULT_SUMMARY_PROMPT = """请将以下对话历史压缩为一段简洁的摘要。
要求:
- 保留关键信息、用户意图和重要结论
- 去除冗余的中间过程
- 摘要长度不超过原文的 30%
- 使用与对话相同的语言

对话历史:
{conversation}

请输出摘要:"""


class SummaryMemoryStrategy(MemoryStrategy):
    """摘要记忆管理

    工作原理:
    1. 所有消息照常存储
    2. 当消息数超过 buffer_size 时，对最早的一批消息调用 LLM 生成摘要
    3. get_context() 返回: [摘要] + [最近 buffer_size 条完整消息]

    需要传入一个 LLM 调用函数 (llm_call)，签名:
        async def llm_call(messages: list[dict]) -> str
    如果不提供，则退化为全量模式 (不生成摘要)。
    """

    strategy_name = "summary"

    def __init__(
        self,
        agent_id: str,
        config: dict[str, Any] | None = None,
        llm_call: LLMCallFn | None = None,
    ) -> None:
        super().__init__(agent_id, config)
        self._buffer_size: int = self._config.get("buffer_size", 10)
        self._summary_interval: int = self._config.get("summary_interval", 20)
        self._summary_prompt: str = self._config.get("summary_prompt", DEFAULT_SUMMARY_PROMPT)
        self._max_total: int = self._config.get("max_messages", 200)

        self._llm_call: LLMCallFn | None = llm_call
        self._messages: list[dict[str, str]] = []
        self._summary: str | None = None

    async def on_message(self, role: str, content: str, **kwargs: Any) -> None:
        self._messages.append({"role": role, "content": content})
        # 总消息上限
        if len(self._messages) > self._max_total:
            self._messages = self._messages[-self._max_total:]
        # 触发摘要生成
        if (
            self._llm_call is not None
            and len(self._messages) >= self._summary_interval
        ):
            await self._generate_summary()

    async def get_context(self, **kwargs: Any) -> list[dict[str, str]]:
        """返回: [摘要(如有)] + [最近 buffer_size 条消息]"""
        context: list[dict[str, str]] = []

        if self._summary:
            context.append({
                "role": "system",
                "content": f"[历史对话摘要]\n{self._summary}",
            })

        recent = self._messages[-self._buffer_size:]
        context.extend(recent)
        return context

    async def clear(self) -> None:
        self._messages.clear()
        self._summary = None

    async def search(self, query: str, top_k: int = 5) -> list[dict[str, str]]:
        """关键词检索摘要 + 保留的消息 (P6)"""
        candidates: list[dict[str, str]] = []
        if self._summary:
            candidates.append({
                "role": "system",
                "content": f"[历史对话摘要]\n{self._summary}",
            })
        candidates.extend(self._messages)
        return MemoryStrategy.keyword_search(candidates, query, top_k)

    async def on_session_end(self) -> None:
        """会话结束时，对所有消息生成最终摘要"""
        if self._llm_call and self._messages:
            await self._generate_summary(force=True)

    async def _generate_summary(self, force: bool = False) -> None:
        """对较早的消息调用 LLM 生成摘要

        Args:
            force: 为 True 时强制对所有消息生成摘要 (会话结束场景)
        """
        if not self._llm_call:
            return
        if not force and len(self._messages) <= self._buffer_size:
            return

        # 需要摘要的部分
        if force:
            # 强制模式: 对所有消息生成摘要
            to_summarize = self._messages
        else:
            to_summarize = self._messages[:-self._buffer_size]
        conversation_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in to_summarize
        )

        # 如果已有旧摘要，合并进去
        if self._summary:
            conversation_text = f"[之前的摘要]\n{self._summary}\n\n[新增对话]\n{conversation_text}"

        prompt = self._summary_prompt.format(conversation=conversation_text)
        messages = [{"role": "user", "content": prompt}]

        try:
            self._summary = await self._llm_call(messages)
        except Exception:
            # 摘要生成失败不影响主流程
            pass

        # 压缩后只保留最近的 buffer_size 条 (非强制模式)
        if not force:
            self._messages = self._messages[-self._buffer_size:]
        else:
            # 强制模式: 摘要覆盖了所有消息，清空原始列表
            self._messages = []

    async def snapshot(self) -> dict[str, Any]:
        base = await super().snapshot()
        base.update({
            "total_messages": len(self._messages),
            "buffer_size": self._buffer_size,
            "has_summary": self._summary is not None,
            "summary_length": len(self._summary) if self._summary else 0,
            "has_llm": self._llm_call is not None,
        })
        return base
