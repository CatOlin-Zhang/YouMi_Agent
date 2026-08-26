"""不依赖真实 LLM 的本地 Mock 引擎桥接器。

用于在「尚未接入真实 YouMi 引擎 / 没有 LLM API」时预览群聊式 GUI 的全部交互：
- 用户气泡、Master 流式回复（逐字）
- 工具调用卡片（对应 BEFORE_TOOL_CALL / AFTER_TOOL_CALL 效果）
- 群聊中多个 Agent 依次发言 + 协作提示（对应 MESSAGE_SENDING 效果）

只依赖 gui 内部模块（models / events / store / ws_hub），不 import youmi。
通过 ``python -m gui --mock`` 或环境变量 ``YOUMI_GUI_MOCK=1`` 启动。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from gui.engine.models import (
    AgentCard,
    MASTER_COLOR,
    MessageRecord,
    Session,
    color_for,
    new_id,
)
from gui.hub.events import (
    agent_join,
    agent_update,
    error,
    history,
    message_chunk,
    message_end,
    message_start,
    message_replace,
    session_created,
    session_deleted,
)
from gui.persistence.store import Store

logger = logging.getLogger(__name__)


# 可用于「拉群」的虚拟角色清单（mock 模式下列在 /api/agents）
MOCK_AGENTS = [
    {"name": "coder", "display_name": "程序员", "role": "coder",
     "description": "负责编写、修改与调试代码"},
    {"name": "reviewer", "display_name": "审查员", "role": "reviewer",
     "description": "审查代码质量、潜在缺陷与风险"},
    {"name": "researcher", "display_name": "研究员", "role": "researcher",
     "description": "检索资料、整理背景信息"},
    {"name": "planner", "display_name": "规划师", "role": "planner",
     "description": "拆解任务、制定执行计划"},
]

# 各角色在群里被点名后的发言模板
ROLE_REPLIES = {
    "coder": "我来负责实现这块逻辑，先补齐函数签名与边界处理，随后跑通基本用例。",
    "reviewer": "我从可读性与健壮性角度过一遍，重点看异常分支与并发安全。",
    "researcher": "我整理一下相关的资料与最佳实践，给你贴几个可参考的实现。",
    "planner": "我把目标拆成几个小步骤，并标注每步的验收标准。",
}


class MockEngineBridge:
    """镜像 EngineBridge 的公共接口，但全部用本地模拟事件驱动。"""

    def __init__(self, hub: Any, config: Any) -> None:
        self.hub = hub
        self.config = config
        # mock 数据独立存放，避免污染真实会话
        mock_dir = os.path.join(config.data_dir, "mock")
        self.store = Store(mock_dir)

        self.master = None  # 与真实桥接保持一致：无 MasterAgent 实例
        self.master_id = "master"
        self.master_name = config.master_agent_name or "Master"

        self.sessions: dict[str, Session] = {}
        self.cards: dict[str, AgentCard] = {}

        self.active_session_id: str | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._open: dict[str, MessageRecord] = {}

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    async def init(self) -> None:
        self.sessions, self.cards = self.store.load_state()
        self._card_for_agent(self.master_id, self.master_name, "master")
        logger.info("Mock 引擎初始化完成（无 LLM），已加载会话数=%d", len(self.sessions))

    # ------------------------------------------------------------------
    # 卡片 / 广播
    # ------------------------------------------------------------------
    def _card_for_agent(
        self, agent_id: str, name: str, role: str = "agent", bio: str = ""
    ) -> AgentCard:
        if agent_id in self.cards:
            return self.cards[agent_id]
        color = MASTER_COLOR if agent_id == self.master_id else color_for(agent_id)
        card = AgentCard(
            agent_id=agent_id, name=name, role=role, color=color, bio=bio
        )
        self.cards[agent_id] = card
        self.store.save_state(self.sessions, self.cards)
        return card

    def card_for(self, agent_id: str, name: str = "", role: str = "agent") -> AgentCard:
        if agent_id in self.cards:
            return self.cards[agent_id]
        return self._card_for_agent(agent_id, name or agent_id, role)

    def _emit(self, event: dict) -> None:
        asyncio.ensure_future(self.hub.broadcast(event))

    # ------------------------------------------------------------------
    # 消息生命周期（气泡的创建 / 追加 / 收尾 + 持久化）
    # ------------------------------------------------------------------
    def open_message(self, rec: MessageRecord) -> None:
        self._open[rec.msg_id] = rec
        self._emit(
            message_start(
                rec.msg_id, rec.session_id, rec.agent_id,
                rec.agent_name, rec.role, rec.kind, rec.text,
            )
        )

    def append_chunk(self, msg_id: str, text: str) -> None:
        rec = self._open.get(msg_id)
        if not rec:
            return
        rec.text += text
        self._emit(message_chunk(msg_id, text))

    def replace_message(self, msg_id: str, text: str) -> None:
        rec = self._open.get(msg_id)
        if not rec:
            return
        rec.text = text
        self._emit(message_replace(msg_id, text))

    def close_message(self, msg_id: str, meta: dict | None = None) -> None:
        rec = self._open.pop(msg_id, None)
        if not rec:
            return
        if meta:
            rec.meta = dict(rec.meta or {})
            rec.meta.update(meta)
        self._emit(message_end(msg_id, rec.text, meta or {}))
        self.store.append_message(rec)

    # ------------------------------------------------------------------
    # 模拟辅助
    # ------------------------------------------------------------------
    async def _stream_text(
        self, msg_id: str, text: str, *, chunk: int = 4, delay: float = 0.04
    ) -> None:
        for i in range(0, len(text), chunk):
            self.append_chunk(msg_id, text[i:i + chunk])
            await asyncio.sleep(delay)

    async def _sim_tool(
        self,
        session_id: str,
        agent_id: str,
        agent_name: str,
        tool_name: str,
        args: str,
        result: str,
    ) -> None:
        msg_id = new_id("tool")
        rec = MessageRecord(
            msg_id=msg_id,
            session_id=session_id,
            agent_id=agent_id,
            agent_name=agent_name,
            role="tool",
            kind="tool",
            text=f"正在调用工具 `{tool_name}` …",
        )
        self.open_message(rec)
        await asyncio.sleep(0.35)
        text = (
            f"**🔧 {tool_name}**\n\n"
            f"参数：`{args}`\n\n"
            f"结果：\n{result}"
        )
        self.replace_message(msg_id, text)
        await asyncio.sleep(0.25)
        self.close_message(
            msg_id,
            meta={
                "tool_name": tool_name,
                "arguments": args,
                "result": result,
            },
        )

    # ------------------------------------------------------------------
    # REST / WS 对外接口（与 EngineBridge 同签名）
    # ------------------------------------------------------------------
    async def list_agents(self) -> list[dict]:
        return list(MOCK_AGENTS)

    def list_sessions(self) -> list[dict]:
        return [s.to_dict() for s in self.sessions.values()]

    def get_session(self, session_id: str) -> dict | None:
        sess = self.sessions.get(session_id)
        if not sess:
            return None
        messages = self.store.load_messages(session_id)
        members = [
            self.cards[aid].to_dict()
            for aid in sess.member_ids
            if aid in self.cards
        ]
        return {
            "session": sess.to_dict(),
            "messages": messages,
            "members": members,
        }

    async def create_session(
        self, type_: str, name: str, member_roles: list[str] | None = None
    ) -> dict:
        sess = Session(
            session_id=new_id("sess"),
            type=type_,
            name=name or ("群聊" if type_ == "group" else "单聊"),
            owner_agent_id=self.master_id,
            member_ids=[self.master_id],
        )
        self.sessions[sess.session_id] = sess
        self.store.save_state(self.sessions, self.cards)
        self._emit(session_created(sess.to_dict()))
        return sess.to_dict()

    async def delete_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        self.store.delete_session(session_id)
        self.store.save_state(self.sessions, self.cards)
        self._emit(session_deleted(session_id))

    async def push_history(self, ws: Any, session_id: str) -> None:
        data = self.get_session(session_id)
        if data:
            await self.hub.send(
                ws, history(session_id, data["messages"], data["members"])
            )

    # ------------------------------------------------------------------
    # 群成员加入（模拟 create_sub_agent + 入群）
    # ------------------------------------------------------------------
    def _spawn_mock_agent(self, role: str, task: str) -> AgentCard:
        # 与真实引擎同样语义：bio 是角色简要定义，task 是发给子 Agent 的消息
        agent_id = new_id("agent")
        info = next((a for a in MOCK_AGENTS if a["role"] == role), None)
        display = info["display_name"] if info else role
        desc = info["description"] if info else ""
        return AgentCard(
            agent_id=agent_id,
            name=display,
            role=role,
            color=color_for(agent_id),
            bio=desc,
            task=task or "",
        )

    async def add_member(
        self,
        session_id: str,
        role: str,
        task: str,
        system_prompt: str = "",
    ) -> None:
        sess = self.sessions.get(session_id)
        if not sess:
            return
        self.active_session_id = session_id
        try:
            card = self._spawn_mock_agent(role, task or f"作为 {role} 参与群聊")
            self.cards[card.agent_id] = card
            self.store.save_state(self.sessions, self.cards)
            if card.agent_id not in sess.member_ids:
                sess.member_ids.append(card.agent_id)
                sess.updated_at = time.time()
                self.store.save_state(self.sessions, self.cards)
            self._emit(agent_join(session_id, card.to_dict()))
            self._emit(agent_update(session_id, card.agent_id, "idle"))
        except Exception as exc:  # pragma: no cover
            logger.exception("mock add_member 出错")
            self._emit(error(str(exc), session_id))
        finally:
            self.active_session_id = None

    # ------------------------------------------------------------------
    # 用户发言 → 一轮群聊（核心模拟）
    # ------------------------------------------------------------------
    async def send_user_message(self, session_id: str, text: str) -> None:
        sess = self.sessions.get(session_id)
        if not sess:
            self._emit(error("会话不存在", session_id))
            return
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            self.active_session_id = session_id
            try:
                # 1) 用户气泡
                user_rec = MessageRecord(
                    msg_id=new_id("u"), session_id=session_id,
                    agent_id="__user__", agent_name="我",
                    role="user", kind="text", text=text,
                )
                self.open_message(user_rec)
                self.close_message(user_rec.msg_id)

                # 2) Master 流式回复（含可选工具调用）
                await self._master_turn(session_id, text)

                # 3) 群聊：其余成员依次发言
                members = [m for m in sess.member_ids if m != self.master_id]
                for mid in members:
                    card = self.cards.get(mid)
                    if not card:
                        continue
                    await self._member_turn(session_id, card)

                sess.updated_at = time.time()
                self.store.save_state(self.sessions, self.cards)
            finally:
                self.active_session_id = None

    async def _master_turn(self, session_id: str, text: str) -> None:
        card = self.card_for(self.master_id, self.master_name, "master")

        need_tool = any(
            k in text
            for k in ("搜索", "查", "search", "资料", "天气", "新闻", "查询")
        )

        if need_tool:
            # 1) 思考段：工具调用前的中间推理（流式）
            t_id = new_id("m")
            t_rec = MessageRecord(
                msg_id=t_id, session_id=session_id,
                agent_id=card.agent_id, agent_name=card.name,
                role="assistant", kind="text", text="",
            )
            self.open_message(t_rec)
            thinking = (
                f"用户在问「{text.strip()[:40]}」。\n"
                "这需要最新的外部信息，仅凭已有知识可能不够准确，\n"
                "先调用搜索工具收集资料，再基于结果整理回答。"
            )
            await self._stream_text(t_id, thinking, chunk=6, delay=0.03)
            self.close_message(t_id)

            # 2) 工具调用段
            await self._sim_tool(
                session_id, card.agent_id, card.name,
                "web_search",
                '{"query": "' + text.strip()[:40] + '"}',
                "共找到 8 条相关结果，已按相关度排序并摘取摘要。",
            )

        # 3) 最终回复段（流式）
        m_id = new_id("m")
        m_rec = MessageRecord(
            msg_id=m_id, session_id=session_id,
            agent_id=card.agent_id, agent_name=card.name,
            role="assistant", kind="text", text="",
        )
        self.open_message(m_rec)
        await asyncio.sleep(0.15)

        reply = (
            f"收到你的问题：「{text.strip()}」。\n\n"
            "我来拆解一下：\n"
            "1. 先明确目标与约束；\n"
            "2. 调用相关工具收集信息；\n"
            "3. 给出可执行的方案。\n\n"
            "如果是群聊，我会让小组成员分别跟进。"
        )
        await self._stream_text(m_id, reply)
        self.close_message(
            m_id, meta={"tool_calls": ["web_search"] if need_tool else []}
        )

    async def _member_turn(self, session_id: str, card: AgentCard) -> None:
        # 协作提示（对应真实 MESSAGE_SENDING 钩子）
        line = f"🔄 **{self.master_name}** → **{card.name}**：请你跟进这个任务"
        sys_id = new_id("sys")
        sys_rec = MessageRecord(
            msg_id=sys_id, session_id=session_id,
            agent_id=self.master_id, agent_name=self.master_name,
            role="system", kind="system", text=line,
        )
        self.open_message(sys_rec)
        self.close_message(sys_id)
        await asyncio.sleep(0.25)

        content = ROLE_REPLIES.get(
            card.role, f"我是 {card.name}，我来协助完成这部分。"
        )
        m_id = new_id("m")
        m_rec = MessageRecord(
            msg_id=m_id, session_id=session_id,
            agent_id=card.agent_id, agent_name=card.name,
            role="assistant", kind="text", text="",
        )
        self.open_message(m_rec)
        await asyncio.sleep(0.2)
        await self._stream_text(m_id, content)
        self.close_message(m_id)

    # ------------------------------------------------------------------
    # MCP / Bus 接口（mock 模式返回空数据，保持与 EngineBridge 同签名）
    # ------------------------------------------------------------------
    async def list_tools(self) -> list[dict]:
        """返回 mock 工具列表，供前端面板渲染。"""
        return [
            {
                "name": "web_search",
                "description": "在网络上搜索相关信息（mock）",
                "parameters": [
                    {"name": "query", "type": "string",
                     "description": "搜索关键词", "required": True}
                ],
            },
            {
                "name": "file_read",
                "description": "读取指定路径的文件内容（mock）",
                "parameters": [
                    {"name": "path", "type": "string",
                     "description": "文件路径", "required": True}
                ],
            },
            {
                "name": "shell_exec",
                "description": "在终端执行 shell 命令（mock）",
                "parameters": [
                    {"name": "command", "type": "string",
                     "description": "要执行的命令", "required": True}
                ],
            },
        ]

    def get_tool_stats(self) -> dict:
        """返回 mock 统计信息。"""
        return {"providers": 1, "tools": 3, "calls": 0, "errors": 0}

    async def shutdown(self) -> None:
        """mock 关闭（无实际操作）。"""
        logger.info("MockEngineBridge 已关闭")
