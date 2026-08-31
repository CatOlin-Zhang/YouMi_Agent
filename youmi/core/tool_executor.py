"""
工具执行 Mixin

从 youmi/core/agent.py 提取，包含 Agent 工具执行相关方法：
- _execute_tool_call   — 工具调用入口（含钩子集成）
- _do_execute_tool     — 实际工具执行（MCP / ToolRegistry 双路径）
- _auto_report_tool_error — 工具错误自动汇报
- _execute_skill_call  — 技能调用（占位，由 SkillLoader 注入）
- _execute_delegation  — Agent 间任务委派 (P1: Handoff)

使用方式：
    class Agent(ToolExecutionMixin, MCPIntegrationMixin):
        ...
"""

from __future__ import annotations

import json
import logging
import traceback as _traceback_mod
from typing import Any, TYPE_CHECKING

from youmi.core.models import _ActionResult
from youmi.core.types import MessageRole

if TYPE_CHECKING:
    from youmi.core.hooks import HookContext, HookDecisionType, HookType

logger = logging.getLogger(__name__)


class ToolExecutionMixin:
    """工具执行 Mixin — 为 Agent 提供工具调用、技能调用、任务委派能力

    依赖 Agent 实例的以下属性（通过继承链提供）:
    - _hook_registry: HookRegistry
    - _tool_bridge: ToolBridge | None
    - _tool_registry: ToolRegistry
    - _conversation: list[dict]
    - _memory: MemoryManager
    - _tool_guardian_id: str
    - _config: AgentConfig
    - _bus: MessageBroker | None
    - _workflow_id: str
    - agent_id / name: str (property)
    - report_tool_issue() (由 MCPIntegrationMixin 提供)
    """

    async def _execute_tool_call(self, payload: dict[str, Any]) -> _ActionResult:
        """执行工具调用

        优先通过 MCP ToolBridge (权限 + 路由)，
        退化到 ToolRegistry 直接执行。

        失败时自动向 ToolGuardianAgent 汇报（如果已连接）。
        集成 before_tool_call / after_tool_call 钩子 (P2: OC-5)。

        流程:
        1. 从 payload 提取工具名和参数
        2. 触发 before_tool_call 钩子 (可拦截/修改)
        3. 通过 ToolBridge 或 ToolRegistry 执行
        4. 触发 after_tool_call 钩子 (可修改结果)
        5. 将结果以 tool role 消息追加到 conversation
        6. 同步写入记忆系统
        7. 失败时自动汇报给 ToolGuardianAgent
        """
        from youmi.core.hooks import HookType, HookContext, HookDecisionType

        name = payload.get("name", "")
        arguments = payload.get("arguments", {})
        tool_call_id = payload.get("tool_call_id", "")
        result_str: str = ""

        # P2: OC-5 — before_tool_call 钩子
        if self._hook_registry.has_hooks(HookType.BEFORE_TOOL_CALL):
            ctx = HookContext(
                hook_type=HookType.BEFORE_TOOL_CALL,
                agent_id=self.agent_id,
                agent_name=self.name,
                tool_name=name,
                tool_arguments=arguments,
            )
            decision = await self._hook_registry.invoke(HookType.BEFORE_TOOL_CALL, ctx)
            if decision.decision == HookDecisionType.BLOCK:
                logger.info("before_tool_call hook blocked tool '%s': %s", name, decision.reason)
                return _ActionResult(
                    success=False,
                    error=f"工具调用被拦截: {decision.reason}",
                )
            if decision.decision == HookDecisionType.MODIFY:
                if "tool_arguments" in decision.modified_data:
                    arguments = decision.modified_data["tool_arguments"]

        # --- 实际工具执行 ---
        action_result = await self._do_execute_tool(name, arguments, tool_call_id)

        # P2: OC-5 — after_tool_call 钩子
        if self._hook_registry.has_hooks(HookType.AFTER_TOOL_CALL):
            ctx = HookContext(
                hook_type=HookType.AFTER_TOOL_CALL,
                agent_id=self.agent_id,
                agent_name=self.name,
                tool_name=name,
                tool_arguments=arguments,
                tool_result=action_result.output if action_result.success else action_result.error,
            )
            await self._hook_registry.invoke(HookType.AFTER_TOOL_CALL, ctx)

        return action_result

    async def _do_execute_tool(
        self, name: str, arguments: dict[str, Any], tool_call_id: str,
    ) -> _ActionResult:
        """实际工具执行逻辑（从 _execute_tool_call 拆分，方便钩子包装）"""
        result_str: str = ""

        if self._tool_bridge is not None:
            # MCP 模式: 通过 ToolBridge 调用
            mcp_result = await self._tool_bridge.call_tool(name, arguments)
            result_str = mcp_result.text
            success = not mcp_result.is_error

            self._conversation.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result_str,
            })

            if not success:
                # 自动汇报给 ToolGuardianAgent
                await self._auto_report_tool_error(
                    name, result_str, arguments,
                )
                return _ActionResult(success=False, error=result_str)

            await self._memory.on_message("tool", result_str, tool_name=name)
            logger.debug("MCP tool '%s' → %s", name, result_str[:100])
            return _ActionResult(success=True, output=result_str)

        # 退化: 直接 ToolRegistry
        if name not in self._tool_registry:
            error_msg = f"工具 '{name}' 未注册"
            self._conversation.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps({"error": error_msg}),
            })
            await self._auto_report_tool_error(name, error_msg, arguments)
            return _ActionResult(success=False, error=error_msg)

        try:
            result = await self._tool_registry.execute(name, arguments)
            result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

            self._conversation.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result_str,
            })

            await self._memory.on_message("tool", result_str, tool_name=name)
            logger.debug("Tool '%s' executed: %s", name, result_str[:100])
            return _ActionResult(success=True, output=result_str)
        except Exception as exc:
            error_msg = f"工具 '{name}' 执行失败: {exc}"
            tb_str = _traceback_mod.format_exc()
            self._conversation.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps({"error": error_msg}),
            })
            logger.warning(error_msg)
            await self._auto_report_tool_error(
                name, error_msg, arguments, error_traceback=tb_str,
            )
            return _ActionResult(success=False, error=error_msg)

    async def _auto_report_tool_error(
        self,
        tool_name: str,
        error_message: str,
        arguments: dict[str, Any],
        error_traceback: str = "",
    ) -> None:
        """工具调用失败时的自动汇报（内部方法）

        仅在已连接 ToolGuardianAgent 时生效，静默失败不影响主流程。
        """
        if not self._tool_guardian_id:
            return
        try:
            await self.report_tool_issue(
                tool_name=tool_name,
                error_message=error_message,
                call_arguments=arguments,
                error_traceback=error_traceback,
            )
        except Exception:
            logger.debug("Failed to auto-report tool error (non-critical)", exc_info=True)

    async def _execute_skill_call(self, payload: dict[str, Any]) -> _ActionResult:
        """执行技能调用 — 由 SkillLoader 注入实际实现"""
        return _ActionResult(
            success=False,
            error="SkillLoader 未装载，请在 on_initialize 中配置",
        )

    async def _execute_delegation(self, payload: dict[str, Any]) -> _ActionResult:
        """委托子任务给其他 Agent (P1: Handoff)

        payload 包含:
        - target_agent_id: 目标 Agent ID
        - task: 任务描述
        - message_template: 消息模板 (可选)
        - depth: 当前委派深度 (内部跟踪)

        流程:
        1. 根据 handoff_rules 匹配目标 Agent
        2. 通过消息总线发送 task 消息
        3. 等待 feedback 回复
        4. 返回委派结果
        """
        from youmi.bus.message import WorkflowMessage, WorkflowMessageType

        target_agent_id = payload.get("target_agent_id", "")
        task = payload.get("task", "")
        depth = payload.get("depth", 0)

        if not target_agent_id:
            return _ActionResult(
                success=False,
                error="delegation 未指定 target_agent_id",
            )

        # 检查委派深度限制
        handoff_cfg = self._config.handoff
        max_depth = handoff_cfg.default_max_depth
        # 查找匹配规则中的 max_depth
        for rule in handoff_cfg.rules:
            if rule.target_agent_id == target_agent_id and rule.enabled:
                max_depth = min(max_depth, rule.max_depth)
                break

        if depth >= max_depth:
            return _ActionResult(
                success=False,
                error=f"委派链深度已达上限 ({max_depth})，拒绝进一步委派",
            )

        # 构造委派消息
        message_template = payload.get("message_template", "请将以下任务完成:\n{task}")
        delegated_task = message_template.format(task=task)

        if self._bus is None:
            return _ActionResult(
                success=False,
                error="未连接消息总线，无法执行 Agent 间委派",
            )

        # 发送 task 消息
        wf_msg = WorkflowMessage(
            workflow_id=self._workflow_id,
            from_agent_id=self.agent_id,
            to_agent_id=target_agent_id,
            msg_type=WorkflowMessageType.TASK,
            role=MessageRole.AGENT,
            content=delegated_task,
            metadata={
                "delegation": True,
                "depth": depth + 1,
                "original_agent_id": payload.get("original_agent_id", self.agent_id),
            },
        )
        await self._bus.publish(wf_msg)

        logger.info(
            "Agent '%s' delegating to '%s' (depth=%d): %s",
            self.name, target_agent_id, depth + 1, task[:80],
        )

        # 等待 feedback 回复
        timeout = handoff_cfg.timeout_seconds
        feedback = await self._bus.wait_for_message(
            self.agent_id, timeout=timeout,
        )

        if feedback is None:
            return _ActionResult(
                success=False,
                error=f"委派超时 ({timeout}s): 未收到 '{target_agent_id}' 的反馈",
            )

        # 解析反馈
        result_content = feedback.content
        success = feedback.msg_type == WorkflowMessageType.FEEDBACK

        return _ActionResult(
            success=success,
            output=result_content,
            error=None if success else f"委派失败: {result_content}",
        )
