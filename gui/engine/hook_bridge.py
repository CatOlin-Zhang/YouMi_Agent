"""GUI Hook 桥接器。

把 YouMi 引擎的 Hook 机制「翻译」成前端能渲染的聊天事件：

- BEFORE_TOOL_CALL  → 打开一个「工具卡片」气泡（等待结果）
- AFTER_TOOL_CALL   → 用参数/结果填充并收尾该工具卡片
- AFTER_MODEL_CALL  → 当某 Agent 产出最终文本（无 tool_calls）时，开一个文本气泡
- MESSAGE_SENDING   → Agent 间互发消息，渲染成一条居中的协作提示

为什么需要它：
引擎里每个 Agent 拥有独立的 HookRegistry，子 Agent 在 create_sub_agent
之后并不会自动继承 Master 的 hooks。因此 GUI 必须在拿到子 Agent 实例后
显式把钩子注入进去（见 engine/bridge.py 的 _patch_create_sub_agent）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from youmi.core.hooks import HookContext, HookDecision, HookType

from gui.engine.models import MessageRecord, new_id
from gui.hub.events import message_end, message_start

logger = logging.getLogger(__name__)

# 工具结果最多展示多少字符，避免超长输出撑爆聊天界面
_MAX_TOOL_RESULT = 2000
# 工具参数最多展示多少字符（如 create_sub_agent 的 task 可能是一整段代码）
_MAX_TOOL_ARGS = 600


class GUIHookBridge:
    def __init__(self, bridge: "Any") -> None:
        self.bridge = bridge
        self._injected: set[str] = set()
        # agent_id -> 正在进行的工具消息 msg_id 栈（支持一个 Agent 连续调多个工具）
        self.tool_stacks: dict[str, list[str]] = {}
        # 正在通过 run_sub_agent 执行的子 Agent ID 集合
        self._sub_agent_running: set[str] = set()

    # ------------------------------------------------------------------
    # 注入
    # ------------------------------------------------------------------
    def inject(self, agent: Any) -> None:
        """把 4 个 GUI 钩子注册到指定 Agent 的 HookRegistry（幂等）。"""
        aid = agent.agent_id
        if aid in self._injected:
            return
        reg = agent.hook_registry
        reg.register(HookType.BEFORE_TOOL_CALL, self._before_tool, plugin_name="gui")
        reg.register(HookType.AFTER_TOOL_CALL, self._after_tool, plugin_name="gui")
        reg.register(HookType.AFTER_MODEL_CALL, self._after_model, plugin_name="gui")
        reg.register(HookType.MESSAGE_SENDING, self._message_sending, plugin_name="gui")
        self._injected.add(aid)
        logger.info("GUI hooks 已注入 Agent '%s'", agent.name)

    # ------------------------------------------------------------------
    # 钩子实现
    # ------------------------------------------------------------------
    async def _before_tool(self, ctx: HookContext) -> HookDecision:
        session_id = self.bridge.active_session_id
        if not session_id:
            return HookDecision.pass_through()
        agent_id = ctx.agent_id
        # 先切断该 Agent 正在流式输出的文本气泡，让工具卡片与其后的新文本
        # 在时间线上排在旧内容之后（前端按到达顺序线性渲染）
        self.bridge.split_agent_stream(agent_id)
        msg_id = new_id("tool")
        self.tool_stacks.setdefault(agent_id, []).append(msg_id)
        rec = MessageRecord(
            msg_id=msg_id,
            session_id=session_id,
            agent_id=agent_id,
            agent_name=ctx.agent_name,
            role="tool",
            kind="tool",
            text=f"正在调用工具 `{ctx.tool_name}` …",
        )
        self.bridge.open_message(rec)
        return HookDecision.pass_through()

    async def _after_tool(self, ctx: HookContext) -> HookDecision:
        session_id = self.bridge.active_session_id
        if not session_id:
            return HookDecision.pass_through()
        agent_id = ctx.agent_id
        stack = self.tool_stacks.get(agent_id, [])
        msg_id = stack.pop() if stack else None
        if not msg_id:
            return HookDecision.pass_through()

        result = ctx.tool_result
        if isinstance(result, (dict, list)):
            result = json.dumps(result, ensure_ascii=False)
        result_str = str(result)[:_MAX_TOOL_RESULT]
        args_str = json.dumps(ctx.tool_arguments, ensure_ascii=False)
        if len(args_str) > _MAX_TOOL_ARGS:
            args_str = args_str[:_MAX_TOOL_ARGS] + " …(参数过长已截断)"
        text = (
            f"**🔧 {ctx.tool_name}**\n\n"
            f"参数：`{args_str}`\n\n"
            f"结果：\n{result_str}"
        )
        self.bridge.replace_message(msg_id, text)
        self.bridge.close_message(
            msg_id,
            meta={
                "tool_name": ctx.tool_name,
                "arguments": args_str,
                "result": result_str,
            },
        )

        # ---- 工作流完成检测：所有步骤完成后注入停止信号 ----
        tracker = getattr(self.bridge, 'tracker', None)
        if tracker and tracker.all_done and ctx.agent_id == self.bridge.master.agent_id:
            master = self.bridge.master
            done = sum(1 for s in tracker.steps if s['status'] == 'done')
            total = len(tracker.steps)
            stop_msg = (
                f"【系统提示】所有 {total} 个工作流步骤已全部完成"
                f"（{done} 个成功，{total - done} 个失败）。"
                f"请立即汇总结果并回复用户，不要再调用任何工具。"
            )
            if hasattr(master, '_conversation') and master._conversation:
                master._conversation.append({"role": "user", "content": stop_msg})
            logger.info("[HookBridge] 工作流已全部完成，注入停止信号")

        return HookDecision.pass_through()

    async def _after_model(self, ctx: HookContext) -> HookDecision:
        session_id = self.bridge.active_session_id
        if not session_id:
            return HookDecision.pass_through()
        # 子 Agent 通过 run_sub_agent 运行时，结果由 run_sub_agent 统一广播，
        # 此处不重复创建气泡
        if ctx.agent_id in self._sub_agent_running:
            return HookDecision.pass_through()
        resp = ctx.response
        has_tools = getattr(resp, "has_tool_calls", False)
        if has_tools:
            # 还有后续工具调用，由工具钩子负责展示
            return HookDecision.pass_through()
        content = getattr(resp, "content", "") or ""
        if not content.strip():
            return HookDecision.pass_through()

        agent_id = ctx.agent_id
        msg_id = new_id("msg")
        rec = MessageRecord(
            msg_id=msg_id,
            session_id=session_id,
            agent_id=agent_id,
            agent_name=ctx.agent_name,
            role="assistant",
            kind="text",
            text=content,
        )
        self.bridge.open_message(rec)
        self.bridge.replace_message(msg_id, content)
        self.bridge.close_message(msg_id)
        return HookDecision.pass_through()

    async def _message_sending(self, ctx: HookContext) -> HookDecision:
        session_id = self.bridge.active_session_id
        if not session_id:
            return HookDecision.pass_through()
        msg = ctx.message
        if msg is None:
            return HookDecision.pass_through()

        from_id = getattr(msg, "from_agent_id", ctx.agent_id)
        to_id = getattr(msg, "to_agent_id", None)
        content = getattr(msg, "content", "") or ""
        from_name = self.bridge.card_for(from_id, ctx.agent_name).name
        to_name = self.bridge.card_for(to_id, to_id).name if to_id else "所有人"
        preview = content[:160].replace("\n", " ")
        text = f"🔄 **{from_name}** → **{to_name}**：{preview}"

        msg_id = new_id("sys")
        rec = MessageRecord(
            msg_id=msg_id,
            session_id=session_id,
            agent_id=from_id,
            agent_name=from_name,
            role="system",
            kind="system",
            text=text,
        )
        self.bridge.open_message(rec)
        self.bridge.close_message(msg_id)
        return HookDecision.pass_through()
