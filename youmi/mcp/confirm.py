"""
召回确认策略 — search_and_confirm 的可插拔确认判定

「召回确认闭环」(structure.md §2 / Phase 4) 的判定者 (confirmer) 可插拔:

- 默认 ``auto_confirm``: 自动接受候选 (自动闭环，无需确认者参与)
- ``LLMConfirmer``: 由原发起 Agent 的 LLM 判断候选是否满足需求
- 任意自定义回调: ``(candidate, query) -> ConfirmDecision``
  (支持同步函数或协程函数，也可直接返回 bool)

闭环语义 (由 ToolBridge.search_and_confirm 驱动):
    搜索 → 确认 → 合适则加载到上下文
                  → 不合适则排除该候选并扩大搜索
                  → 多轮无合适候选回复「没有该功能的工具」

用法::

    from youmi.mcp.confirm import ConfirmDecision, build_llm_confirmer

    # 方式一: 默认自动接受 (不传 confirmer)
    result = await bridge.search_and_confirm("发送邮件的工具")

    # 方式二: 注入 LLM 判定
    confirmer = build_llm_confirmer(llm_client)
    result = await bridge.search_and_confirm("发送邮件的工具", confirmer=confirmer)

    # 方式三: 人工确认回调
    async def manual(candidate, query):
        ok = await ask_user(f"是否加载 {candidate.tool_name}?")
        return ConfirmDecision(confirmed=ok, reason="manual")

    result = await bridge.search_and_confirm("发送邮件的工具", confirmer=manual)
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from youmi.mcp.vault import ToolSearchResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------

@dataclass
class ConfirmDecision:
    """确认判定结果

    Args:
        confirmed: 候选工具是否满足需求 (True = 合适，加载到上下文)
        reason: 判定理由 (用于日志/审计)
    """

    confirmed: bool
    reason: str = ""


#: 确认者协议 — 接受 (候选, 原始查询) 返回 ConfirmDecision
#: 支持同步函数或协程函数；返回 bool 时视为 confirmed
Confirmer = Callable[
    ["ToolSearchResult", str],
    "ConfirmDecision | bool | Awaitable[ConfirmDecision | bool]",
]


# ---------------------------------------------------------------------------
# 内置策略
# ---------------------------------------------------------------------------

async def auto_confirm(candidate: Any, query: str) -> ConfirmDecision:
    """默认确认策略 — 自动接受候选

    闭环语义依然生效: 多轮搜索中被否决的候选会被排除;
    需要在候选间做智能取舍时，注入 LLMConfirmer 或自定义回调。
    """
    return ConfirmDecision(confirmed=True, reason="auto")


class LLMConfirmer:
    """LLM 确认判定者 — 由原发起 Agent 的 LLM 判断候选是否满足需求

    以单轮无工具调用询问 LLM 输出 JSON 判定结果。判定调用异常或
    解析失败时优雅降级为「接受」—— 不因判定器故障卡死召回闭环
    (被误接受的候选仍受审批/白名单等后续环节约束)。

    Args:
        llm_client: LLM 客户端 (需实现 ``chat(messages) -> LLMResponse``)
        model: 判定使用的模型名 (仅用于日志展示，None = 使用 client 默认)
        min_confidence: 最低置信度 (低于该值视为否决，默认 0.5)
    """

    def __init__(
        self,
        llm_client: Any,
        model: str | None = None,
        min_confidence: float = 0.5,
    ) -> None:
        self._llm = llm_client
        self._model = model
        self._min_confidence = min(1.0, max(0.0, min_confidence))

    async def __call__(self, candidate: Any, query: str) -> ConfirmDecision:
        prompt = (
            "你是工具召回确认器。请判断候选工具是否满足需求。\n"
            f"需求: {query}\n"
            f"候选工具: {candidate.tool_name} (相似度 {candidate.score:.3f})\n"
            f"工具摘要: {candidate.summary}\n\n"
            '仅输出 JSON: {"suitable": true/false, "confidence": 0-1, '
            '"reason": "一句话理由"}'
        )
        try:
            response = await self._llm.chat(
                messages=[{"role": "user", "content": prompt}],
            )
            content = (response.content or "").strip()
            data = _extract_json_object(content)
            suitable = bool(data.get("suitable"))
            confidence = float(data.get("confidence", 0.5))
            reason = str(data.get("reason", ""))[:200]
        except Exception as exc:
            logger.warning(
                "LLMConfirmer 判定失败，降级为自动接受: %s", exc,
            )
            return ConfirmDecision(
                confirmed=True,
                reason=f"llm_confirm_error: {type(exc).__name__}",
            )

        confirmed = suitable and confidence >= self._min_confidence
        if not reason:
            reason = "llm accept" if confirmed else "llm reject"
        logger.debug(
            "LLMConfirmer[%s]: '%s' → %s (confidence=%.2f, reason=%s)",
            self._model or "default", candidate.tool_name,
            "confirmed" if confirmed else "rejected", confidence, reason,
        )
        return ConfirmDecision(confirmed=confirmed, reason=reason)


def build_llm_confirmer(llm_client: Any, **kwargs: Any) -> LLMConfirmer:
    """工厂 — 构造 LLM 确认判定者

    Args:
        llm_client: LLM 客户端实例
        **kwargs: 透传给 LLMConfirmer (model / min_confidence)
    """
    return LLMConfirmer(llm_client, **kwargs)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

async def call_confirmer(
    confirmer: Confirmer,
    candidate: Any,
    query: str,
) -> ConfirmDecision:
    """统一调用确认者 — 兼容同步/异步回调与 bool 返回值

    Args:
        confirmer: 确认者回调
        candidate: 候选工具 (ToolSearchResult)
        query: 原始查询

    Returns:
        ConfirmDecision (任何异常视为否决，交由调用方继续搜索)
    """
    try:
        outcome = confirmer(candidate, query)
        if inspect.isawaitable(outcome):
            outcome = await outcome
    except Exception as exc:
        logger.warning("confirmer 执行失败，视为否决: %s", exc)
        return ConfirmDecision(
            confirmed=False,
            reason=f"confirmer_error: {type(exc).__name__}",
        )

    if isinstance(outcome, ConfirmDecision):
        return outcome
    if isinstance(outcome, bool):
        return ConfirmDecision(confirmed=outcome)
    return ConfirmDecision(confirmed=bool(outcome))


def _extract_json_object(text: str) -> dict[str, Any]:
    """从 LLM 输出中提取 JSON 对象 (容忍 markdown 代码块与前后缀)

    Raises:
        ValueError: 未找到合法 JSON 对象
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"未找到 JSON 对象: {text[:120]!r}")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层不是对象")
    return data
