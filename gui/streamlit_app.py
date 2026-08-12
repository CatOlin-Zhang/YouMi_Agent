"""YouMi Agent — 群聊式 GUI v3

启动命令:
    streamlit run gui/streamlit_app.py

交互模型:
- 群聊界面：所有 Agent 共享同一对话窗口
- @mention：@Agent名 直接与指定 Agent 对话
- Agent 面板：侧边栏显示所有 Agent 实例及其状态
- MasterAgent 创建子 Agent 后自动加入群聊
- 流式输出 + 持久化 Event Loop
"""

from __future__ import annotations

import sys
import os
import re
import time
import asyncio
import logging
import threading
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import streamlit as st

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.types import LLMConfig, LLMProvider, MemoryConfig, AgentMetadata
from youmi.coordinator.master import MasterAgent
from youmi.llm.client import LLMClient

# ---------------------------------------------------------------------------
# 持久化 Event Loop
# ---------------------------------------------------------------------------

class _EventLoopRunner:
    """在独立后台线程运行一个永不关闭的 asyncio event loop"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def run_stream(self, agent, message: str):
        """流式桥接: async generator → sync generator"""
        loop = self._loop
        q_future = asyncio.run_coroutine_threadsafe(_create_queue(), loop)
        q = q_future.result()
        SENTINEL = object()

        async def _bridge():
            try:
                async for item in agent.chat_turn_stream(message):
                    await q.put(item)
            except Exception as e:
                await q.put({"error": str(e), "response": f"出错: {e}",
                             "iterations": 0, "tool_calls": []})
            finally:
                await q.put(SENTINEL)

        asyncio.run_coroutine_threadsafe(_bridge(), loop)

        while True:
            async def _safe_get():
                return await asyncio.wait_for(q.get(), timeout=180)
            try:
                item = asyncio.run_coroutine_threadsafe(_safe_get(), loop).result()
            except asyncio.TimeoutError:
                yield "（等待超时）"
                return
            if item is SENTINEL:
                return
            yield item

    def shutdown(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)


async def _create_queue():
    return asyncio.Queue()


# ---------------------------------------------------------------------------
# 日志捕获
# ---------------------------------------------------------------------------

class _GuiLogHandler(logging.Handler):
    def __init__(self, log_list: list) -> None:
        super().__init__(level=logging.INFO)
        self._log_list = log_list

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log_list.append(self.format(record))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# @mention 解析
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"@(\S+)\s*", re.UNICODE)


def parse_mention(text: str) -> tuple[str | None, str]:
    """解析 @Agent名 前缀，返回 (agent_name, 清理后文本)"""
    m = _MENTION_RE.match(text.strip())
    if m:
        return m.group(1), text[m.end():].strip()
    return None, text


# ---------------------------------------------------------------------------
# Agent 注册表管理
# ---------------------------------------------------------------------------

def _register_agent(agent: Agent, is_master: bool = False) -> None:
    """将 Agent 注册到全局面板"""
    registry: dict = st.session_state.agent_registry
    if agent.agent_id not in registry:
        registry[agent.agent_id] = {
            "name": agent.name,
            "role": agent.metadata.role,
            "agent": agent,
            "is_master": is_master,
        }


def _sync_sub_agents(master: MasterAgent) -> list[str]:
    """同步 MasterAgent 的子 Agent 到注册表，返回新增的名称列表"""
    new_names: list[str] = []
    for aid, rec in master.get_sub_agents().items():
        if aid not in st.session_state.agent_registry:
            st.session_state.agent_registry[aid] = {
                "name": rec.agent.name,
                "role": rec.role,
                "agent": rec.agent,
                "is_master": False,
            }
            new_names.append(rec.agent.name)
    return new_names


def _find_agent_by_name(name: str) -> Agent | None:
    """按名称查找 Agent（不区分大小写）"""
    name_lower = name.lower()
    for info in st.session_state.agent_registry.values():
        if info["name"].lower() == name_lower:
            return info["agent"]
    return None


def _get_status_icon(agent: Agent) -> str:
    s = agent.status.value
    return {"idle": "🟢", "running": "🔵", "completed": "✅",
            "failed": "🔴", "created": "🟡", "waiting": "🟡",
            "destroyed": "⚫"}.get(s, "⚪")


# ---------------------------------------------------------------------------
# 页面配置
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="YouMi Agent — 群聊",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Session State
# ---------------------------------------------------------------------------

defaults = {
    "messages": [],               # 群聊消息列表
    "agent_registry": {},         # {agent_id: {name, role, agent, is_master}}
    "master_agent": None,         # MasterAgent 实例
    "llm_client": None,
    "loop_runner": None,
    "agent_mode": "orchestrator",
    "model": "qwen2.5:3b",
    "ollama_url": "http://localhost:11434/v1",
    "temperature": 0.3,
    "max_tokens": 1024,
    "system_prompt": "你是一个智能助手，基于 YouMi Agent 框架运行。请用中文简洁回答问题。",
    "orchestrator_prompt": (
        "你是 MasterAgent，一个任务协调者。你的工作流程是：\n\n"
        "## 第一步：分析任务\n"
        "收到用户任务后，先用文字输出你的分析：\n"
        "- 任务目标是什么？\n"
        "- 需要拆分为哪些子任务？\n"
        "- 每个子任务需要什么角色的 Agent？\n"
        "- 是否已有可复用的 Agent？（调用 list_sub_agents 检查）\n\n"
        "## 第二步：规划工作流\n"
        "输出工作流计划，列出每个步骤和对应的 Agent 角色。\n\n"
        "## 第三步：创建并运行 Agent\n"
        "- 先 list_sub_agents 检查已有 Agent，避免重复创建\n"
        "- 如需新 Agent，调用 create_sub_agent，必须提供详细 system_prompt\n"
        "- system_prompt 要包含：角色定位、具体任务、输出格式要求\n"
        "- 创建后调用 run_sub_agent 运行\n\n"
        "## 工具\n"
        "- create_sub_agent(role, task, system_prompt): 创建子Agent\n"
        "- run_sub_agent(agent_id): 运行子Agent\n"
        "- list_sub_agents(): 列出所有子Agent\n\n"
        "调用工具时，必须在回复中使用如下 JSON 代码块格式：\n"
        "```json\n"
        '{"tool_call": {"name": "工具名", "arguments": {"参数名": "值"}}}\n'
        "```\n\n"
        "## 示例\n"
        "用户: 帮我做一个邮件自动回复系统\n"
        "你: 我来分析这个任务...\n"
        "工作流计划：\n"
        "1. 创建 email_reader Agent — 负责读取邮件\n"
        "2. 创建 email_writer Agent — 负责撰写回复\n"
        "3. 创建 email_sender Agent — 负责发送回复\n\n"
        "先检查已有 Agent...\n"
        "```json\n"
        '{"tool_call": {"name": "list_sub_agents", "arguments": {}}}\n'
        "```\n\n"
        "请用中文简洁回复。"
    ),
    "processing": False,
    "turn_count": 0,
    "tool_calls_total": 0,
    "activity_log": [],
}

for key, val in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = val

if st.session_state.loop_runner is None:
    st.session_state.loop_runner = _EventLoopRunner()

_runner: _EventLoopRunner = st.session_state.loop_runner

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def check_ollama(base_url: str) -> tuple[bool, list[str]]:
    import httpx
    try:
        resp = httpx.get(f"{base_url}/models", timeout=5)
        models = resp.json().get("data", [])
        return True, [m["id"] for m in models]
    except Exception:
        return False, []


def create_master_agent(model, base_url, temperature, max_tokens, system_prompt):
    """创建 MasterAgent + LLMClient"""
    llm_cfg = LLMConfig(
        provider=LLMProvider.LOCAL, model=model, base_url=base_url,
        api_key="ollama", temperature=temperature,
        max_tokens=max_tokens, timeout_s=180,
    )
    config = AgentConfig(
        name="MasterAgent",
        system_prompt=system_prompt,
        llm_config=llm_cfg,
        memory_config=MemoryConfig(strategy="full"),
        metadata=AgentMetadata(display_name="MasterAgent", role="master"),
        max_iterations=10,
    )
    master = MasterAgent(config)
    client = LLMClient(llm_cfg)
    master._llm_client = client
    return master, client


def reset_session():
    """重置会话"""
    for info in st.session_state.agent_registry.values():
        try:
            _runner.run(info["agent"].destroy())
        except Exception:
            pass
    if st.session_state.llm_client:
        try:
            _runner.run(st.session_state.llm_client.close())
        except Exception:
            pass
    st.session_state.agent_registry = {}
    st.session_state.master_agent = None
    st.session_state.llm_client = None
    st.session_state.messages = []
    st.session_state.turn_count = 0
    st.session_state.tool_calls_total = 0
    st.session_state.activity_log = []
    st.session_state.processing = False


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🤖 YouMi Agent")
    st.caption("多 Agent 群聊")
    st.divider()

    # --- 连接 ---
    st.subheader("连接设置")
    ollama_url = st.text_input("Ollama URL", value=st.session_state.ollama_url)
    connected, available_models = check_ollama(ollama_url)
    if connected:
        st.success(f"已连接 ({len(available_models)} 个模型)")
    else:
        st.error("未连接 — 请确认 Ollama 已启动")

    model = st.selectbox(
        "模型",
        options=available_models if available_models else [st.session_state.model],
        index=0,
    )
    col1, col2 = st.columns(2)
    with col1:
        temperature = st.slider("温度", 0.0, 1.0, st.session_state.temperature, 0.1)
    with col2:
        max_tokens = st.slider("最大Token", 256, 4096, st.session_state.max_tokens, 256)

    st.divider()

    # --- 模式 ---
    st.subheader("Agent 模式")
    agent_mode = st.radio(
        "选择模式",
        options=["orchestrator", "chat"],
        format_func=lambda x: "🎯 编排模式" if x == "orchestrator" else "💬 单Agent",
        index=0 if st.session_state.agent_mode == "orchestrator" else 1,
        horizontal=True,
    )

    with st.expander("系统提示词"):
        default_prompt = (
            st.session_state.orchestrator_prompt
            if agent_mode == "orchestrator"
            else st.session_state.system_prompt
        )
        system_prompt = st.text_area(
            "System Prompt", value=default_prompt,
            height=150, label_visibility="collapsed",
        )

    st.divider()

    # --- Agent 面板 ---
    st.subheader("📋 Agent 面板")
    registry: dict = st.session_state.agent_registry
    if registry:
        for aid, info in registry.items():
            agent = info["agent"]
            icon = _get_status_icon(agent)
            badge = "👑" if info["is_master"] else "🤖"
            st.markdown(f"{badge} **{info['name']}** `{info['role']}` {icon}")
    else:
        st.caption("暂无 Agent — 等待初始化")

    st.divider()

    # --- 统计 ---
    col1, col2 = st.columns(2)
    with col1:
        st.metric("对话轮次", st.session_state.turn_count)
    with col2:
        st.metric("工具调用", st.session_state.tool_calls_total)

    st.divider()

    # --- 操作 ---
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔄 新会话", use_container_width=True):
            reset_session()
            st.rerun()
    with col2:
        show_log = st.toggle("日志", value=False)

    if show_log and st.session_state.activity_log:
        with st.expander("最近日志", expanded=True):
            for entry in st.session_state.activity_log[-30:]:
                st.caption(entry)

# ---------------------------------------------------------------------------
# 确保 MasterAgent 已创建
# ---------------------------------------------------------------------------

need_new = (
    st.session_state.master_agent is None
    or st.session_state.agent_mode != agent_mode
    or (hasattr(st.session_state, '_last_model') and st.session_state._last_model != model)
    or (hasattr(st.session_state, '_last_url') and st.session_state._last_url != ollama_url)
)

if need_new:
    if st.session_state.master_agent is not None:
        reset_session()
    if connected:
        if agent_mode == "orchestrator":
            master, client = create_master_agent(
                model, ollama_url, temperature, max_tokens, system_prompt)
            _runner.run(master.initialize())
            st.session_state.master_agent = master
            st.session_state.llm_client = client
            st.session_state.agent_mode = agent_mode
            st.session_state._last_model = model
            st.session_state._last_url = ollama_url
            _register_agent(master, is_master=True)

            # 日志
            handler = _GuiLogHandler(st.session_state.activity_log)
            handler.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S"))
            logging.getLogger("youmi").addHandler(handler)
        else:
            # 单 Agent 模式：也用 MasterAgent 壳子但没有工具
            llm_cfg = LLMConfig(
                provider=LLMProvider.LOCAL, model=model, base_url=ollama_url,
                api_key="ollama", temperature=temperature,
                max_tokens=max_tokens, timeout_s=180,
            )
            cfg = AgentConfig(
                name="YouMi", system_prompt=system_prompt, llm_config=llm_cfg,
                memory_config=MemoryConfig(strategy="full"),
                metadata=AgentMetadata(display_name="YouMi Agent", role="assistant"),
                max_iterations=5,
            )
            agent = Agent(cfg)
            client = LLMClient(llm_cfg)
            agent._llm_client = client
            _runner.run(agent.initialize())
            st.session_state.master_agent = agent  # 复用 slot
            st.session_state.llm_client = client
            st.session_state.agent_mode = agent_mode
            st.session_state._last_model = model
            st.session_state._last_url = ollama_url
            _register_agent(agent, is_master=True)

            handler = _GuiLogHandler(st.session_state.activity_log)
            handler.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S"))
            logging.getLogger("youmi").addHandler(handler)
    else:
        st.warning("⚠️ Ollama 未连接，请在侧边栏检查设置。")
        st.stop()

# ---------------------------------------------------------------------------
# 聊天历史渲染
# ---------------------------------------------------------------------------

st.markdown("## 💬 群聊")
agent_count = len(st.session_state.agent_registry)
mode_label = "编排模式" if agent_mode == "orchestrator" else "单Agent"
st.caption(f"模型: **{model}** · {mode_label} · {agent_count} 个 Agent · 用 `@Agent名` 直接对话")

for msg in st.session_state.messages:
    role = msg["role"]
    if role == "user":
        with st.chat_message("user", avatar="👤"):
            st.markdown(msg["content"])
    else:
        sender = msg.get("sender", "Agent")
        sender_role = msg.get("sender_role", "assistant")
        avatar = "👑" if msg.get("is_master") else "🤖"
        with st.chat_message("assistant", avatar=avatar):
            st.caption(f"**{sender}** `{sender_role}`")
            st.markdown(msg["content"])
            meta_parts = []
            if msg.get("tool_calls"):
                meta_parts.append(f"🔧 {', '.join(msg['tool_calls'])}")
            if msg.get("iterations", 0) > 1:
                meta_parts.append(f"⚡ {msg['iterations']} 轮")
            if msg.get("elapsed"):
                meta_parts.append(f"⏱ {msg['elapsed']:.1f}s")
            if meta_parts:
                st.caption(" · ".join(meta_parts))
            # 子 Agent 加入提示
            if msg.get("new_agents"):
                for na in msg["new_agents"]:
                    st.info(f"🤖 **{na}** 已加入群聊")

# ---------------------------------------------------------------------------
# 用户输入处理
# ---------------------------------------------------------------------------

# --- Agent 选择器 (@mention 浮动面板) ---
_sub_agents = [
    info for info in st.session_state.agent_registry.values()
    if not info.get("is_master")
]

# 构建选择器 UI
col_popover, col_status = st.columns([1, 6])

with col_popover:
    with st.popover("📋 @", use_container_width=True):
        st.caption("选择要对话的 Agent")
        _filter = st.text_input(
            "搜索", placeholder="输入名称过滤…",
            key="_mention_filter", label_visibility="collapsed",
        )
        # MasterAgent 选项
        if not _filter or "master".startswith(_filter.lower()):
            if st.button("👑 MasterAgent", key="_sel_master",
                         use_container_width=True):
                st.session_state._mention_target = None
                st.rerun()
        st.divider()
        # 子 Agent 列表
        for info in _sub_agents:
            name = info["name"]
            if _filter and _filter.lower() not in name.lower():
                continue
            icon = _get_status_icon(info["agent"])
            if st.button(
                f"🤖 {name}  `{info['role']}` {icon}",
                key=f"_sel_{name}", use_container_width=True,
            ):
                st.session_state._mention_target = name
                st.rerun()

with col_status:
    _target = st.session_state.get("_mention_target")
    if _target:
        st.markdown(f"📩 发给: **@{_target}** &nbsp; "
                     f"[切换回 MasterAgent](#)")
        # 点击"切换"取消选择 — 用 checkbox hack
        if st.button("↩ 切回 MasterAgent", key="_clear_mention"):
            st.session_state._mention_target = None
            st.rerun()
    else:
        st.caption("📩 发给 **MasterAgent** · 点击左侧 **📋 @** 选择其他 Agent")

_selected_agent_name = st.session_state.get("_mention_target")

if user_input := st.chat_input(
    f"给 @{_selected_agent_name} 发消息…" if _selected_agent_name
    else "输入消息… 点击 📋 @ 选择 Agent"
):
    master = st.session_state.master_agent
    if master is None:
        st.warning("Agent 未就绪。")
        st.stop()

    # 如果选择了特定 Agent，自动添加 @mention
    if _selected_agent_name:
        user_input = f"@{_selected_agent_name} {user_input}"

    # 记录用户消息
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user", avatar="👤"):
        st.markdown(user_input)

    # 解析 @mention
    target_name, clean_msg = parse_mention(user_input)
    target_agent = None

    if target_name:
        target_agent = _find_agent_by_name(target_name)
        if target_agent is None:
            # Agent 不存在
            st.session_state.messages.append({
                "role": "assistant",
                "sender": "System",
                "sender_role": "system",
                "is_master": False,
                "content": f"⚠️ 未找到 Agent **@{target_name}**。当前可用: "
                           + ", ".join(f"`{i['name']}`" for i in st.session_state.agent_registry.values()),
            })
            st.rerun()

    # 确定目标 Agent 和消息
    if target_agent is not None:
        # @mention → 直接对话
        active_agent = target_agent
        actual_msg = clean_msg
        is_direct = True
    else:
        # 无 @mention → 发给 MasterAgent
        active_agent = master
        actual_msg = user_input
        is_direct = False

    # 流式回复
    sender_name = active_agent.name
    sender_role = active_agent.metadata.role
    is_master = (active_agent is master and agent_mode == "orchestrator")
    avatar = "👑" if is_master else "🤖"

    with st.chat_message("assistant", avatar=avatar):
        st.caption(f"**{sender_name}** `{sender_role}`")
        placeholder = st.empty()
        full_text = ""
        result_meta: dict = {}
        t0 = time.time()

        try:
            for item in _runner.run_stream(active_agent, actual_msg):
                if isinstance(item, dict):
                    result_meta = item
                else:
                    full_text += str(item)
                    placeholder.markdown(full_text + "▌")
        except Exception as e:
            full_text = f"处理出错: {e}"
            result_meta = {"error": str(e), "iterations": 0, "tool_calls": []}

        placeholder.markdown(full_text)
        elapsed = time.time() - t0

        tool_calls = result_meta.get("tool_calls", [])
        iterations = result_meta.get("iterations", 0)

        meta_parts = []
        if tool_calls:
            meta_parts.append(f"🔧 {', '.join(tool_calls)}")
            st.session_state.tool_calls_total += len(tool_calls)
        if iterations > 1:
            meta_parts.append(f"⚡ {iterations} 轮")
        meta_parts.append(f"⏱ {elapsed:.1f}s")
        st.caption(" · ".join(meta_parts))

    # MasterAgent 回合后同步子 Agent
    new_agents: list[str] = []
    if not is_direct and isinstance(master, MasterAgent):
        new_agents = _sync_sub_agents(master)
        if new_agents:
            for na in new_agents:
                st.info(f"🤖 **{na}** 已加入群聊")

    # 记录回复
    st.session_state.messages.append({
        "role": "assistant",
        "sender": sender_name,
        "sender_role": sender_role,
        "is_master": is_master,
        "content": full_text,
        "tool_calls": tool_calls,
        "iterations": iterations,
        "elapsed": elapsed,
        "new_agents": new_agents,
    })
    st.session_state.turn_count += 1
    st.rerun()
