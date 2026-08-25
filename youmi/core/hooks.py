"""
Hook / 插件系统

参考 OpenClaw Plugin Hooks 设计，为 Agent ReAct 循环的各阶段提供拦截点。
第三方插件可以通过注册钩子来:
- 拦截、修改、阻止 Agent 行为的任何阶段
- 注入额外上下文、日志、审计等

钩子类型:
- before_prompt_build: prompt 组装前，可注入额外 prompt 层
- before_model_call: LLM 调用前，可修改消息列表或阻止调用
- after_model_call: LLM 调用后，可修改响应
- before_tool_call: 工具执行前，可修改参数或阻止执行
- after_tool_call: 工具执行后，可修改结果
- message_received: Agent 收到消息时
- message_sending: Agent 发送消息前

每个钩子返回 HookDecision:
- PASS: 不干预，继续执行
- MODIFY: 修改数据后继续
- BLOCK: 终止执行
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Callable, Awaitable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 钩子类型
# ---------------------------------------------------------------------------

class HookType(str, Enum):
    """钩子类型 — 对应 Agent ReAct 循环的各阶段"""

    BEFORE_PROMPT_BUILD = "before_prompt_build"
    BEFORE_MODEL_CALL = "before_model_call"
    AFTER_MODEL_CALL = "after_model_call"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    MESSAGE_RECEIVED = "message_received"
    MESSAGE_SENDING = "message_sending"


class HookDecisionType(str, Enum):
    """钩子决策类型"""

    PASS = "pass"       # 不干预
    MODIFY = "modify"   # 修改数据后继续
    BLOCK = "block"     # 终止执行


# ---------------------------------------------------------------------------
# 钩子上下文
# ---------------------------------------------------------------------------

class HookContext(BaseModel):
    """钩子执行上下文

    传递给每个钩子处理函数的数据容器。
    不同钩子类型使用不同的字段子集。
    """

    hook_type: HookType
    agent_id: str = ""
    agent_name: str = ""

    # before_model_call / before_prompt_build
    messages: list[dict[str, Any]] = Field(default_factory=list)

    # after_model_call
    response: Any = None

    # before_tool_call / after_tool_call
    tool_name: str = ""
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    tool_result: Any = None

    # message_received / message_sending
    message: Any = None

    # 通用扩展字段
    extra: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class HookDecision(BaseModel):
    """钩子决策

    钩子处理函数的返回值，决定后续行为。
    """

    decision: HookDecisionType = HookDecisionType.PASS
    modified_data: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""

    @classmethod
    def pass_through(cls) -> HookDecision:
        """不干预，继续执行"""
        return cls(decision=HookDecisionType.PASS)

    @classmethod
    def modify(cls, **data: Any) -> HookDecision:
        """修改数据后继续"""
        return cls(decision=HookDecisionType.MODIFY, modified_data=data)

    @classmethod
    def block(cls, reason: str = "") -> HookDecision:
        """终止执行"""
        return cls(decision=HookDecisionType.BLOCK, reason=reason)


# 钩子处理函数签名: async def(context) -> HookDecision
HookHandler = Callable[[HookContext], Awaitable[HookDecision]]


# ---------------------------------------------------------------------------
# 钩子注册表
# ---------------------------------------------------------------------------

class _HookEntry(BaseModel):
    """内部: 单个钩子注册条目"""

    hook_type: HookType
    handler: Any  # HookHandler (Pydantic 不支持 Callable 验证，用 Any)
    priority: int = 0
    plugin_name: str = ""

    model_config = {"arbitrary_types_allowed": True}


class HookRegistry:
    """钩子注册表

    管理钩子的注册、注销和链式调用。
    钩子按优先级升序执行（低优先级先执行）。
    block 决策终止后续链。
    """

    def __init__(self) -> None:
        self._hooks: dict[HookType, list[_HookEntry]] = {
            ht: [] for ht in HookType
        }

    def register(
        self,
        hook_type: HookType,
        handler: HookHandler,
        priority: int = 0,
        plugin_name: str = "",
    ) -> None:
        """注册钩子处理函数

        Args:
            hook_type: 钩子类型
            handler: 异步处理函数 async def(context) -> HookDecision
            priority: 优先级（低值先执行）
            plugin_name: 所属插件名称（日志/调试用）
        """
        entry = _HookEntry(
            hook_type=hook_type,
            handler=handler,
            priority=priority,
            plugin_name=plugin_name,
        )
        entries = self._hooks[hook_type]
        entries.append(entry)
        entries.sort(key=lambda e: e.priority)

        logger.debug(
            "Hook registered: %s (priority=%d, plugin=%s)",
            hook_type.value, priority, plugin_name or "anonymous",
        )

    def unregister(self, hook_type: HookType, handler: HookHandler) -> None:
        """注销指定的钩子处理函数"""
        self._hooks[hook_type] = [
            e for e in self._hooks[hook_type] if e.handler is not handler
        ]

    def unregister_all_by_plugin(self, plugin_name: str) -> int:
        """注销指定插件注册的所有钩子

        Returns:
            被注销的钩子数量
        """
        count = 0
        for ht in HookType:
            before = len(self._hooks[ht])
            self._hooks[ht] = [
                e for e in self._hooks[ht] if e.plugin_name != plugin_name
            ]
            count += before - len(self._hooks[ht])
        if count:
            logger.debug("Unregistered %d hooks for plugin '%s'", count, plugin_name)
        return count

    async def invoke(self, hook_type: HookType, context: HookContext) -> HookDecision:
        """链式调用钩子

        按优先级升序依次调用注册的钩子处理函数:
        - PASS: 继续下一个钩子
        - MODIFY: 合并 modified_data 到 context.extra，继续下一个
        - BLOCK: 立即返回，终止后续钩子

        无注册钩子时返回 PASS。

        Args:
            hook_type: 钩子类型
            context: 钩子上下文

        Returns:
            最终的 HookDecision
        """
        entries = self._hooks.get(hook_type, [])
        if not entries:
            return HookDecision.pass_through()

        last_decision = HookDecision.pass_through()

        for entry in entries:
            try:
                decision = await entry.handler(context)
            except Exception as exc:
                logger.warning(
                    "Hook handler error (%s, plugin=%s): %s",
                    hook_type.value, entry.plugin_name or "anonymous", exc,
                    exc_info=True,
                )
                continue

            last_decision = decision

            if decision.decision == HookDecisionType.BLOCK:
                logger.debug(
                    "Hook blocked: %s by plugin '%s' — %s",
                    hook_type.value, entry.plugin_name, decision.reason,
                )
                return decision

            if decision.decision == HookDecisionType.MODIFY:
                # 合并修改数据到 context，供后续钩子读取
                context.extra.update(decision.modified_data)

        return last_decision

    def has_hooks(self, hook_type: HookType) -> bool:
        """检查是否有注册的钩子"""
        return bool(self._hooks.get(hook_type))

    def hook_count(self, hook_type: HookType) -> int:
        """指定类型的钩子数量"""
        return len(self._hooks.get(hook_type, []))

    def __repr__(self) -> str:
        counts = {ht.value: len(entries) for ht, entries in self._hooks.items() if entries}
        return f"<HookRegistry hooks={counts}>"
