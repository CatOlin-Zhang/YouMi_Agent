"""
SummaryGenerator — 三级摘要生成器 (可插拔)

三级摘要索引 (P2 差距 3) 的生成侧:
- L1: 工具摘要 (温态显示 + L1 向量索引文本, ≤80 字)
- L2: 摘要的摘要 (薄层初筛向量索引文本, ≤30 字)
- L3: 完整 schema (definition_json, 仅命中后按需读取, 不生成)

默认启发式: L1 = description 前 80 字, L2 = L1 前 30 字;
注入 LLM client (需提供 ``await chat(messages) -> str`` 接口) 时
切换为生成式摘要，失败自动回退启发式 (不阻塞主流程)。

用法::

    from youmi.mcp.summary import SummaryGenerator

    gen = SummaryGenerator()                       # 启发式
    gen = SummaryGenerator(llm_client=llm)         # 生成式 (LLM 可用时)

    l1 = await gen.generate_l1("send_email", "发送电子邮件到指定地址")
    l2 = await gen.generate_l2("send_email", l1)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# 启发式截断长度
L1_MAX_CHARS = 80
L2_MAX_CHARS = 30

# 生成式摘要提示词
_L1_PROMPT = (
    "用不超过 80 字概括以下工具的功能, 直接输出摘要文本, 不要任何前缀:\n"
    "工具名: {name}\n描述: {description}"
)
_L2_PROMPT = (
    "用不超过 30 字提炼以下工具摘要的核心要点, 直接输出文本, 不要任何前缀:\n"
    "工具名: {name}\n摘要: {summary}"
)


class SummaryGenerator:
    """三级摘要生成器 — 默认启发式截断, 注入 LLM 时生成式

    Args:
        llm_client: LLM 客户端 (可选; 需提供
            ``await chat(messages: list[dict]) -> str`` 接口)。
            注入且调用成功 → 生成式摘要; 失败 → 回退启发式。
    """

    def __init__(self, llm_client: Any = None) -> None:
        self._llm = llm_client

    @property
    def llm_client(self) -> Any:
        return self._llm

    # ------------------------------------------------------------------
    # L1: 工具摘要
    # ------------------------------------------------------------------

    async def generate_l1(self, name: str, description: str) -> str:
        """生成 L1 摘要 (≤80 字)

        默认启发式: description 前 80 字;
        注入 LLM 时生成式摘要 (失败回退启发式)。
        """
        heuristic = (description or "")[:L1_MAX_CHARS].strip()
        if self._llm is None or not heuristic:
            return heuristic
        return await self._generate_with_llm(
            _L1_PROMPT.format(name=name, description=description),
            fallback=heuristic,
        )

    # ------------------------------------------------------------------
    # L2: 摘要的摘要
    # ------------------------------------------------------------------

    async def generate_l2(self, name: str, summary_l1: str) -> str:
        """生成 L2 摘要 (≤30 字, 摘要的摘要)

        默认启发式: L1 摘要前 30 字;
        注入 LLM 时生成式摘要 (失败回退启发式)。
        """
        heuristic = (summary_l1 or "")[:L2_MAX_CHARS].strip()
        if self._llm is None or not heuristic:
            return heuristic
        return await self._generate_with_llm(
            _L2_PROMPT.format(name=name, summary=summary_l1),
            fallback=heuristic,
        )

    # ------------------------------------------------------------------
    # LLM 调用 (带回退)
    # ------------------------------------------------------------------

    async def _generate_with_llm(self, prompt: str, fallback: str) -> str:
        """调用 LLM 生成摘要, 失败回退启发式结果"""
        try:
            reply = await self._llm.chat([
                {"role": "user", "content": prompt},
            ])
            text = str(reply).strip()
            if text:
                return text
        except Exception as exc:
            logger.warning(
                "SummaryGenerator: LLM summary failed, fallback to "
                "heuristic: %s", exc,
            )
        return fallback

    def __repr__(self) -> str:
        return (
            f"<SummaryGenerator mode="
            f"{'llm' if self._llm else 'heuristic'}>"
        )
