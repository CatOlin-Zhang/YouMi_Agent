"""引擎桥接器：GUI 与 YouMi 引擎之间的适配层。

职责：
1. 持有唯一的 MasterAgent 实例，并把 GUI hooks 注入进去。
2. 在 ``create_sub_agent`` 上做运行时包装：子 Agent 一旦被创建，
   立即注入 hooks、生成卡片、作为成员「加入群聊」。
3. 把一次用户发言驱动成一段「群聊」：Master 的流式输出作为自己的气泡，
   子 Agent 的发言/工具调用经 hooks 变成独立的气泡。
4. 维护会话、成员、消息的持久化与实时广播。

注意：所有 Agent 处理都在事件循环上顺序进行；同一会话同一时刻只允许一轮
对话（用 per-session 锁保护），避免多轮输出互相交错。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
from gui.engine.mcp_service import MCPService
from gui.engine.tracker import WorkflowTracker
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

# 过滤 MasterAgent.chat_turn_stream 在文本流中夹带的工具标记（已由工具钩子独立展示）
_MARKER_RE = re.compile(r"\*\[[^\]]*?\]\*")


class EngineBridge:
    def __init__(self, hub: Any, config: Any) -> None:
        self.hub = hub
        self.config = config
        self.store = Store(config.data_dir)

        self.master: MasterAgent | None = None
        self.hook_bridge: GUIHookBridge | None = None

        self.sessions: dict[str, Session] = {}
        self.cards: dict[str, AgentCard] = {}

        self.active_session_id: str | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._open: dict[str, MessageRecord] = {}  # 进行中的消息（用于流式追加）
        self.tracker: WorkflowTracker | None = None  # 当前轮次的工作流追踪器
        self.mcp_service: MCPService | None = None   # MCP 服务层
        self.broker: Any = None                      # InProcessBroker 消息总线

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    async def init(self) -> None:
        # 延迟导入 youmi，避免 mock 模式（无 youmi）在导入期就报错
        from youmi.coordinator.master import MasterAgent
        from gui.engine.hook_bridge import GUIHookBridge

        self.sessions, self.cards = self.store.load_state()
        self.master = MasterAgent.from_config_dir(self.config.master_agent_name)

        # ---- MCP 服务层初始化 ----
        if self.config.mcp_enabled:
            try:
                self.mcp_service = MCPService(
                    vault_enabled=getattr(self.config, 'vault_enabled', True),
                    db_path=getattr(self.config, 'vault_db_path', '') or '',
                    embedding_base_url=getattr(self.config, 'embedding_base_url', 'http://localhost:11434/v1'),
                    embedding_model=getattr(self.config, 'embedding_model', 'nomic-embed-text'),
                )
                await self.mcp_service.setup(self.master)
                logger.info("MCP 服务已启用: %s", self.mcp_service.get_tool_stats())
            except Exception as exc:
                logger.warning("MCP 服务初始化失败，退化为 ToolRegistry 模式: %s", exc)
                self.mcp_service = None

        # ---- 消息总线初始化 ----
        if self.config.bus_enabled:
            try:
                from youmi.bus.broker import InProcessBroker
                self.broker = InProcessBroker()
                self.master.connect_bus(self.broker)
                logger.info("消息总线已启用 (InProcessBroker)")
            except Exception as exc:
                logger.warning("消息总线初始化失败: %s", exc)
                self.broker = None

        self.master._gui_bridge = self  # 供 run_sub_agent 广播结果用
        self.hook_bridge = GUIHookBridge(self)
        self.hook_bridge.inject(self.master)
        self._patch_create_sub_agent()
        self._patch_run_sub_agent()
        self._card_for_agent(self.master.agent_id, self.master.name, "master")
        logger.info(
            "EngineBridge 初始化完成: master=%s, 会话数=%d, mcp=%s, bus=%s",
            self.master.name, len(self.sessions),
            'yes' if self.mcp_service else 'no',
            'yes' if self.broker else 'no',
        )

    # ------------------------------------------------------------------
    # Agent 卡片
    # ------------------------------------------------------------------
    def _card_for_agent(
        self, agent_id: str, name: str, role: str = "agent", bio: str = ""
    ) -> AgentCard:
        if agent_id in self.cards:
            return self.cards[agent_id]
        color = MASTER_COLOR if agent_id == self.master.agent_id else color_for(agent_id)
        card = AgentCard(agent_id=agent_id, name=name, role=role, color=color, bio=bio)
        self.cards[agent_id] = card
        self.store.save_state(self.sessions, self.cards)
        return card

    def card_for(self, agent_id: str, name: str = "", role: str = "agent") -> AgentCard:
        if agent_id in self.cards:
            return self.cards[agent_id]
        return self._card_for_agent(agent_id, name or agent_id, role)

    # ------------------------------------------------------------------
    # 子 Agent 注入（运行时包装 create_sub_agent）
    # ------------------------------------------------------------------
    def _patch_create_sub_agent(self) -> None:
        original = self.master.create_sub_agent
        bridge = self

        def patched(role: str, **kwargs: Any):
            # ---- 工作流硬限制：阻止创建过多子 Agent ----
            tracker = bridge.tracker
            if tracker and not tracker.can_create_more():
                msg = tracker.get_limit_message()
                logger.warning("[Bridge] 子 Agent 创建被限制: %s", msg)
                session_id = bridge.active_session_id
                if session_id:
                    bridge._emit(error(msg, session_id))
                raise RuntimeError(msg)

            agent = original(role=role, **kwargs)

            # ---- MCP 接入：子 Agent 共享 MCPServer ----
            if bridge.mcp_service and bridge.mcp_service.initialized:
                try:
                    allowed = kwargs.get("allowed_tools", None)
                    bridge.mcp_service.connect_agent(agent, allowed_tools=allowed)
                except Exception as exc:
                    logger.warning("子 Agent '%s' MCP 接入失败: %s", agent.name, exc)

            # ---- 消息总线接入：子 Agent 连接 InProcessBroker ----
            if bridge.broker is not None:
                try:
                    agent.connect_bus(bridge.broker)
                except Exception as exc:
                    logger.warning("子 Agent '%s' 总线接入失败: %s", agent.name, exc)

            bridge.hook_bridge.inject(agent)  # 显式注入 GUI hooks
            task = kwargs.get("task", "")
            bridge.on_sub_agent_created(agent, role, task)
            # 注意：tracker 通知由 coordinator_ops 工具函数统一处理，
            # 避免双重通知。这里只负责 hook 注入和卡片登记。
            return agent

        self.master.create_sub_agent = patched

    def _patch_run_sub_agent(self) -> None:
        """包装 MasterAgent.run_sub_agent，在子 Agent 实际运行时广播 running/idle 状态。"""
        original = self.master.run_sub_agent
        bridge = self

        async def patched(agent_id: str):
            bridge.update_agent_status(agent_id, "running")
            try:
                return await original(agent_id)
            finally:
                bridge.update_agent_status(agent_id, "idle")

        self.master.run_sub_agent = patched

    def on_sub_agent_created(self, agent: Any, role: str, task: str) -> None:
        """子 Agent 被创建后：登记卡片并（若正处于某群聊中）加入该群。

        语义区分：
        - bio  ← 角色简要定义（agents/<role>/config.yaml 的 metadata.description）
        - task ← Master 发给该子 Agent 的任务消息，单独存放（可能很长）
        """
        try:
            from youmi.agents import load_agent_config
            desc = (load_agent_config(role).get("metadata", {}) or {}).get(
                "description", ""
            )
        except Exception:
            desc = ""
        card = self._card_for_agent(agent.agent_id, agent.name, role, bio=desc)
        card.task = task or ""
        self.store.save_state(self.sessions, self.cards)

        session_id = self.active_session_id
        if session_id and session_id in self.sessions:
            sess = self.sessions[session_id]
            if agent.agent_id not in sess.member_ids:
                sess.member_ids.append(agent.agent_id)
                sess.updated_at = time.time()
                self.store.save_state(self.sessions, self.cards)
            self._emit(agent_join(session_id, card.to_dict()))
            self._emit(agent_update(session_id, agent.agent_id, card.status))

    def update_agent_status(self, agent_id: str, status: str) -> None:
        """更新某 Agent 的运行状态并广播到前端。

        状态约定：
        - idle: 空闲/已完成
        - running: 正在执行任务（前端会闪烁提示）
        """
        card = self.cards.get(agent_id)
        if card:
            card.status = status
            self.store.save_state(self.sessions, self.cards)
        session_id = self.active_session_id
        if session_id:
            self._emit(agent_update(session_id, agent_id, status))

    # ------------------------------------------------------------------
    # 事件广播
    # ------------------------------------------------------------------
    def _emit(self, event: dict) -> None:
        asyncio.ensure_future(self.hub.broadcast(event))

    # ------------------------------------------------------------------
    # 消息生命周期（气泡的创建 / 追加 / 收尾 + 持久化）
    # ------------------------------------------------------------------
    def open_message(self, rec: MessageRecord) -> None:
        self._open[rec.msg_id] = rec
        # 查找该 agent 的稳定颜色
        card = self.cards.get(rec.agent_id)
        color = card.color if card else ""
        self._emit(
            message_start(
                rec.msg_id, rec.session_id, rec.agent_id,
                rec.agent_name, rec.role, rec.kind, rec.text,
                color=color,
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

    def split_agent_stream(self, agent_id: str) -> None:
        """切断该 Agent 正在流式输出的文本消息。

        工具调用发生时（BEFORE_TOOL_CALL）调用：先收尾当前流式段，
        之后的流式文本由 send_user_message 开启新消息段继续接收。
        这样「思考 → 工具 → 回复」按真实时间顺序到达前端，
        而不是全部追加进最早打开的气泡里。
        """
        for msg_id, rec in list(self._open.items()):
            if (
                rec.agent_id == agent_id
                and rec.role == "assistant"
                and rec.kind == "text"
            ):
                self.close_message(msg_id)

    def _open_master_msg(self, session_id: str, card: AgentCard) -> str:
        """开启一段 Master 流式文本消息，返回 msg_id。"""
        msg_id = new_id("m")
        rec = MessageRecord(
            msg_id=msg_id, session_id=session_id,
            agent_id=card.agent_id, agent_name=card.name,
            role="assistant", kind="text", text="",
        )
        self.open_message(rec)
        return msg_id

    # ------------------------------------------------------------------
    # 对外 REST/WS API
    # ------------------------------------------------------------------
    async def list_agents(self) -> list[dict]:
        from youmi.agents import list_agents, load_agent_config

        out: list[dict] = []
        for name in list_agents():
            try:
                data = load_agent_config(name)
                md = data.get("metadata", {}) or {}
                out.append({
                    "name": name,
                    "display_name": md.get("display_name", name),
                    "role": md.get("role", name),
                    "description": md.get("description", ""),
                })
            except Exception:
                out.append({
                    "name": name, "display_name": name,
                    "role": name, "description": "",
                })
        return out

    async def list_tools(self) -> list[dict]:
        """列出 MCPServer 中所有已注册的工具（供 /api/tools 端点调用）。"""
        if self.mcp_service and self.mcp_service.initialized:
            return await self.mcp_service.list_tools()
        # 退化：列出 ToolRegistry 中的工具
        if self.master and self.master._tool_registry:
            return [
                {
                    "name": defn.name,
                    "description": defn.description,
                    "parameters": [
                        {"name": p.name, "type": p.type, "description": p.description, "required": p.required}
                        for p in (defn.parameters or [])
                    ],
                }
                for defn in self.master._tool_registry._definitions.values()
            ]
        return []

    def get_tool_stats(self) -> dict:
        """获取 MCPServer 统计信息。"""
        if self.mcp_service:
            return self.mcp_service.get_tool_stats()
        return {"providers": 0, "tools": 0, "calls": 0, "errors": 0}

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
        owner = self.master.agent_id
        sess = Session(
            session_id=new_id("sess"),
            type=type_,
            name=name or ("群聊" if type_ == "group" else "单聊"),
            owner_agent_id=owner,
            member_ids=[owner],
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
            await self.hub.send(ws, history(
                session_id, data["messages"], data["members"]
            ))

    # ------------------------------------------------------------------
    # 核心：用户发言 → 群聊一轮
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

                # 2) 初始化本轮工作流追踪器
                self.tracker = WorkflowTracker(self)

                # 3) Master 气泡（流式）
                # 工具调用会触发 split_agent_stream 切断当前流式段：
                # 工具调用前的中间推理与之后的最终回复拆成多个消息段，
                # 前端按到达顺序线性渲染，保证时间线不错乱。
                card = self.card_for(self.master.agent_id, self.master.name, "master")
                m_id = self._open_master_msg(session_id, card)
                ended = False
                try:
                    async for chunk in self.master.chat_turn_stream(text):
                        if isinstance(chunk, dict):
                            # chat_turn_stream 最后一个 yield 是结果字典
                            self.close_message(
                                m_id, meta={"tool_calls": chunk.get("tool_calls", [])}
                            )
                            ended = True
                            break
                        clean = _MARKER_RE.sub("", chunk)
                        if not clean.strip():
                            continue
                        if m_id not in self._open:
                            # 上一段已被工具调用切断，开启新消息段继续输出
                            m_id = self._open_master_msg(session_id, card)
                        self.append_chunk(m_id, clean)
                except Exception as exc:  # 引擎异常不应让 GUI 崩溃
                    logger.exception("chat_turn_stream 执行出错")
                    if m_id not in self._open:
                        m_id = self._open_master_msg(session_id, card)
                    self.append_chunk(m_id, f"\n\n[出错] {exc}")
                    self._emit(error(str(exc), session_id))

                if not ended:
                    self.close_message(m_id)

                sess.updated_at = time.time()
                self.store.save_state(self.sessions, self.cards)
            finally:
                self.active_session_id = None

    # ------------------------------------------------------------------
    # 群聊：把配置好的某个角色作为成员拉进群
    # ------------------------------------------------------------------
    async def add_member(
        self, session_id: str, role: str, task: str, system_prompt: str = ""
    ) -> None:
        sess = self.sessions.get(session_id)
        if not sess:
            return
        self.active_session_id = session_id
        try:
            agent = self.master.create_sub_agent(
                role=role,
                task=task or f"作为 {role} 参与群聊",
                system_prompt=system_prompt,
            )
            # patched create_sub_agent 已注入 hooks 并执行 on_sub_agent_created（加入群）
            self._emit(agent_update(session_id, agent.agent_id, "idle"))
        except Exception as exc:
            logger.exception("add_member 出错")
            self._emit(error(str(exc), session_id))
        finally:
            self.active_session_id = None

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    async def shutdown(self) -> None:
        """关闭引擎，释放 MCP 和总线资源。"""
        if self.mcp_service:
            await self.mcp_service.shutdown()
        if self.broker:
            try:
                await self.broker.close()
            except Exception:
                pass
        logger.info("EngineBridge 已关闭")
