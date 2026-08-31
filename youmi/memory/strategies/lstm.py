"""
长短时记忆策略 (LSTMMemoryStrategy)

根据对话内容自动区分短期记忆与长期记忆:
- 短期记忆: 当前会话的操作细节、中间过程 (对话结束后丢弃或归档)
- 长期记忆: 关键结论、用户偏好、重要决策 (跨会话保留)

分类逻辑:
- 默认基于关键词规则分类
- 可注入 LLM 进行语义分类 (更准确)

适用于: 需要跨会话积累经验、记住用户偏好的 Agent。
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from youmi.memory.strategies.base import MemoryStrategy

# LLM 调用函数签名: async def(messages) -> str
LLMCallFn = Callable[[list[dict[str, str]]], Awaitable[str]]

# 默认长期记忆关键词 (消息中包含这些词 → 归为长期记忆)
DEFAULT_LONG_TERM_KEYWORDS = [
    "记住", "remember", "偏好", "prefer", "以后",
    "always", "never", "永远", "务必", "important",
    "重要", "决策", "conclusion", "结论", "总结",
    "规则", "rule", "规范", "standard", "约定",
]

# LLM 分类提示词
DEFAULT_CLASSIFY_PROMPT = """判断以下对话内容是否包含需要长期记忆的信息。

长期记忆的标准:
- 用户的偏好或习惯 (如 "我喜欢...", "以后请...")
- 重要的决策或结论
- 需要跨会话记住的规则或约定
- 项目相关的核心知识

如果不是以上类型，则归为短期记忆 (当前会话的操作细节)。

对话内容:
{content}

请只回答 "long_term" 或 "short_term":"""


class LSTMMemoryStrategy(MemoryStrategy):
    """长短时记忆管理

    存储结构:
    - _short_term: 当前会话的操作消息，会话结束后清理
    - _long_term:  持久化的关键知识，跨会话保留

    分类方式 (优先级从高到低):
    1. 如果注入了 llm_call，调用 LLM 进行语义分类
    2. 否则使用关键词规则匹配
    3. 默认归入短期记忆

    用法::

        strategy = LSTMMemoryStrategy(
            agent_id="a1",
            config={"keywords": ["记住", "偏好"]},
            llm_call=my_llm_function,  # 可选
        )
    """

    strategy_name = "lstm"

    def __init__(
        self,
        agent_id: str,
        config: dict[str, Any] | None = None,
        llm_call: LLMCallFn | None = None,
    ) -> None:
        super().__init__(agent_id, config)
        self._keywords: list[str] = self._config.get("keywords", DEFAULT_LONG_TERM_KEYWORDS)
        self._max_short_term: int = self._config.get("max_short_term", 100)
        self._max_long_term: int = self._config.get("max_long_term", 500)
        self._classify_prompt: str = self._config.get("classify_prompt", DEFAULT_CLASSIFY_PROMPT)

        self._llm_call: LLMCallFn | None = llm_call
        self._short_term: list[dict[str, str]] = []
        self._long_term: list[dict[str, str]] = []

    async def on_message(self, role: str, content: str, **kwargs: Any) -> None:
        """根据内容分类存入短期或长期记忆"""
        memory_type = await self._classify(role, content)

        entry = {"role": role, "content": content}

        if memory_type == "long_term":
            self._long_term.append(entry)
            if len(self._long_term) > self._max_long_term:
                self._long_term = self._long_term[-self._max_long_term:]
        else:
            self._short_term.append(entry)
            if len(self._short_term) > self._max_short_term:
                self._short_term = self._short_term[-self._max_short_term:]

    async def get_context(self, **kwargs: Any) -> list[dict[str, str]]:
        """返回: [长期记忆(作为system提示)] + [近期短期记忆]

        Args:
            long_term_limit: 最多返回多少条长期记忆 (默认全部)
            short_term_limit: 最多返回多少条短期记忆 (默认全部)
        """
        long_limit = kwargs.get("long_term_limit", len(self._long_term))
        short_limit = kwargs.get("short_term_limit", len(self._short_term))

        context: list[dict[str, str]] = []

        # 长期记忆 → 作为 system 上下文注入
        if self._long_term:
            lt_entries = self._long_term[-long_limit:]
            lt_text = "\n".join(f"- {e['content']}" for e in lt_entries)
            context.append({
                "role": "system",
                "content": f"[长期记忆 — 跨会话保留的关键信息]\n{lt_text}",
            })

        # 短期记忆 → 正常对话上下文
        st_entries = self._short_term[-short_limit:]
        context.extend(st_entries)

        return context

    async def clear(self) -> None:
        """清空短期记忆 (长期记忆保留)"""
        self._short_term.clear()

    async def clear_all(self) -> None:
        """清空全部记忆 (短期 + 长期)"""
        self._short_term.clear()
        self._long_term.clear()

    async def on_session_end(self) -> None:
        """会话结束 — 短期记忆清理，长期记忆保留"""
        self._short_term.clear()

    async def _classify(self, role: str, content: str) -> str:
        """判断消息应归入短期还是长期记忆

        Returns:
            "long_term" 或 "short_term"
        """
        # 方式1: LLM 语义分类
        if self._llm_call is not None:
            try:
                prompt = self._classify_prompt.format(content=content)
                messages = [{"role": "user", "content": prompt}]
                result = await self._llm_call(messages)
                result = result.strip().lower()
                if "long_term" in result:
                    return "long_term"
                return "short_term"
            except Exception:
                pass  # LLM 失败，回退到关键词

        # 方式2: 关键词匹配
        content_lower = content.lower()
        for keyword in self._keywords:
            if keyword.lower() in content_lower:
                return "long_term"

        # 默认: 短期记忆
        return "short_term"

    async def get_long_term_memories(self) -> list[dict[str, str]]:
        """获取全部长期记忆条目"""
        return list(self._long_term)

    async def search(self, query: str, top_k: int = 5) -> list[dict[str, str]]:
        """关键词检索长期 + 短期记忆 (P6)

        优先返回长期记忆匹配 (更稳定的知识)，其次短期记忆。
        """
        lt_hits = MemoryStrategy.keyword_search(self._long_term, query, top_k)
        if len(lt_hits) >= top_k:
            return lt_hits
        st_hits = MemoryStrategy.keyword_search(
            self._short_term, query, top_k - len(lt_hits),
        )
        return lt_hits + st_hits

    async def get_short_term_memories(self) -> list[dict[str, str]]:
        """获取全部短期记忆条目"""
        return list(self._short_term)

    async def snapshot(self) -> dict[str, Any]:
        base = await super().snapshot()
        base.update({
            "short_term_count": len(self._short_term),
            "long_term_count": len(self._long_term),
            "max_short_term": self._max_short_term,
            "max_long_term": self._max_long_term,
            "has_llm": self._llm_call is not None,
            "keywords_count": len(self._keywords),
        })
        return base
