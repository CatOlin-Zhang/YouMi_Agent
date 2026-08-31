"""
Agent 基类

框架中所有 Agent 的公共基类，提供:
- 唯一身份 (agent_id) 与生命周期状态机 (AgentStatus)
- 对外标签 (AgentMetadata) — 用于发现、展示、检索
- 独立记忆空间 (MemoryAdapter) — 短期对话 + 长期知识
- ReAct 循环骨架 — 子类通过覆写 _observe/_think/_act/_reflect 扩展行为
- 消息收发 — Agent 间通信接口
- 生命周期钩子 — on_initialize / on_start / on_stop / on_destroy

设计约定:
- AgentConfig 是纯数据配置，可序列化、可从 YAML 加载
- Agent 是运行时实体，持有 Memory / 事件循环等不可序列化资源
- 子类不应覆写 __init__，而应覆写 on_initialize 钩子
"""

from __future__ import annotations

import asyncio
import httpx
import json
import logging
import os
import re
import traceback as _traceback_mod
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from youmi.core.types import (
    AgentMessage,
    AgentMetadata,
    HandoffConfig,
    HandoffRule,
    LLMConfig,
    MemoryConfig,
    MessageRole,
    RetryPolicy,
    ToolsConfig,
)
from youmi.core.tool import ToolRegistry
from youmi.core.hooks import HookRegistry, HookType, HookContext, HookDecision, HookDecisionType
from youmi.core.plugin import Plugin, PluginManager
from youmi.core.prompt import PromptAssembler, PromptLayer
from youmi.llm.client import LLMClient, LLMResponse
from youmi.memory.base import MemoryAdapter
from youmi.memory.memory import MemoryManager
from youmi.memory.strategies.base import MemoryStrategy

# 延迟导入避免循环依赖
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from youmi.mcp.bridge import ToolBridge
    from youmi.mcp.server import MCPServer
    from youmi.bus.broker import MessageBroker
    from youmi.bus.message import WorkflowMessage, WorkflowMessageType

logger = logging.getLogger(__name__)

from youmi.core.models import (
    AgentStatus,
    AgentConfig,
    TaskResult,
    _Observation,
    _Thought,
    _ActionResult,
    _Reflection,
    _TaskSelfCheck,
    _ToolRequest,
    _text_similarity,
)
from youmi.core.tool_executor import ToolExecutionMixin
from youmi.core.mcp_integration import MCPIntegrationMixin


# ---------------------------------------------------------------------------
# Agent 基类
# ---------------------------------------------------------------------------

class Agent(ToolExecutionMixin, MCPIntegrationMixin):
    """Agent 基类

    所有框架内的 Agent 均继承自此。子类通过覆写以下方法定制行为:

    生命周期钩子:
        on_initialize()  — Agent 创建后首次调用，用于装载 Skill/Tool/资源
        on_start()       — 每次 run() 开始时调用
        on_stop()        — run() 结束后调用 (无论成功/失败)
        on_destroy()     — Agent 被销毁前调用，释放资源

    ReAct 阶段 (默认实现已覆盖基础流程):
        _observe()       — 收集上下文: 消息、记忆、环境信息
        _think()         — 调用 LLM 推理，决定下一步行动
        _act()           — 执行行动 (Tool/Skill 调用)
        _reflect()       — 评估行动结果，决定是否继续

    用法::

        class MyAgent(Agent):
            async def _think(self, obs):
                # 自定义推理逻辑
                ...

        config = AgentConfig(name="Coder", system_prompt="你是程序员...")
        agent = MyAgent(config)
        await agent.initialize()
        result = await agent.run(task_description="写一个排序算法")
    """

    def __init__(
        self,
        config: AgentConfig,
        memory_strategy: str | MemoryStrategy | None = None,
        llm_call: Any | None = None,
    ) -> None:
        """
        Args:
            config: Agent 配置
            memory_strategy: 记忆策略覆盖 (优先级高于 config.memory_config.strategy)。
                支持: "full" / "summary" / "lstm" / .py 文件路径 / MemoryStrategy 实例
            llm_call: LLM 调用函数，签名 async def(messages) -> str。
                summary 和 lstm 策略使用此函数。
        """
        self._config = config
        self._status = AgentStatus.CREATED

        # 解析运行环境路径：优先使用配置值，否则自动检测项目根目录
        self._env = config.env if config.env else self._detect_project_root()

        # 记忆系统初始化: 优先使用参数传入的策略，否则从 config 读取
        strategy = memory_strategy or config.memory_config.strategy
        strategy_config = config.memory_config.strategy_config or None

        # 工具权限初始快照（用于工作流级回收）
        self._initial_allowed_tools: set[str] | None = None

        # Session 持久化后端初始化 (P0: Persistence)
        persistence_backend = self._create_persistence_backend(
            config.memory_config.persistence,
        )

        self._memory = MemoryManager(
            agent_id=config.agent_id,
            strategy=strategy,
            config=strategy_config,
            llm_call=llm_call,
            persistence_backend=persistence_backend,
        )
        # 保留旧版 MemoryAdapter 引用 (兼容)
        self._legacy_memory = MemoryAdapter(agent_id=config.agent_id)

        # 上下文压缩器初始化 (P0: Compaction)
        from youmi.memory.compaction import ContextCompactor
        self._compactor = ContextCompactor(
            max_context_tokens=config.llm_config.max_context_tokens,
            reserve_ratio=config.memory_config.compaction.reserve_ratio,
            keep_recent=config.memory_config.compaction.keep_recent,
            llm_call=llm_call,
        ) if config.memory_config.compaction.enabled else None

        self._message_queue: asyncio.Queue[AgentMessage] = asyncio.Queue()
        self._iteration_count: int = 0
        self._task_start_time: datetime | None = None
        self._extra_state: dict[str, Any] = {}   # 子类可扩展的内部状态

        # LLM 客户端 & 工具注册表 (on_initialize 中可装载)
        self._llm_client: LLMClient | None = None
        self._tool_registry: ToolRegistry = ToolRegistry()
        self._tool_bridge: Any = None  # ToolBridge | None (MCP 连接后设置)

        # 消息总线 Broker (connect_bus 后设置)
        self._bus: Any = None  # MessageBroker | None
        self._workflow_id: str = ""  # 当前工作流 ID

        # ToolGuardian 连接 (connect_guardian 后设置)
        self._tool_guardian_id: str = ""
        self._tool_guardian_bus: Any = None  # 用于向 guardian 发送汇报的 broker

        # 运行期间的完整消息列表 (包含 tool_calls / tool results)
        self._conversation: list[dict[str, Any]] = []

        # Hook / 插件系统 (P2: OC-5)
        self._hook_registry = HookRegistry()
        self._plugin_manager = PluginManager(self._hook_registry)

        # Prompt 动态组装器 (P2: OC-6)
        self._prompt_assembler: PromptAssembler | None = None

    # -----------------------------------------------------------------------
    # 公共属性
    # -----------------------------------------------------------------------

    @property
    def agent_id(self) -> str:
        return self._config.agent_id

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def status(self) -> AgentStatus:
        return self._status

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def memory(self) -> MemoryManager:
        """独立记忆空间 (MemoryManager — 策略驱动)"""
        return self._memory

    @property
    def metadata(self) -> AgentMetadata:
        """对外标签"""
        return self._config.metadata

    @property
    def iteration_count(self) -> int:
        return self._iteration_count

    @property
    def tool_registry(self) -> ToolRegistry:
        """工具注册表"""
        return self._tool_registry

    @property
    def tool_bridge(self) -> Any:
        """MCP ToolBridge (连接 MCP 后可用)"""
        return self._tool_bridge

    @property
    def bus(self) -> Any:
        """MessageBroker (connect_bus 后可用)"""
        return self._bus

    @property
    def workflow_id(self) -> str:
        """当前工作流 ID"""
        return self._workflow_id

    @property
    def llm_client(self) -> LLMClient | None:
        return self._llm_client

    @property
    def env(self) -> str:
        """Agent 运行环境路径（逻辑工作目录）

        默认继承项目根目录。子类/子 Agent 可配置为特定子目录作为沙箱。
        """
        return self._env

    @property
    def compactor(self) -> Any:
        """上下文压缩器 (ContextCompactor | None)

        P0: Compaction。启用后在 _observe() 中自动压缩过长的上下文。
        """
        return getattr(self, '_compactor', None)

    @property
    def hook_registry(self) -> HookRegistry:
        """钩子注册表 (P2: OC-5)

        用于注册 before_tool_call / after_tool_call 等拦截钩子。
        """
        return self._hook_registry

    @property
    def plugin_manager(self) -> PluginManager:
        """插件管理器 (P2: OC-5)

        管理已安装的插件实例。通过 plugin_manager.register(plugin) 安装。
        """
        return self._plugin_manager

    @property
    def prompt_assembler(self) -> PromptAssembler | None:
        """Prompt 动态组装器 (P2: OC-6)

        非 None 时，_observe() 会使用组装器生成 system prompt。
        可通过 add_prompt_layer() 添加额外层。
        """
        return self._prompt_assembler

    @staticmethod
    def _detect_project_root() -> str:
        """自动检测项目根目录

        从当前工作目录向上查找 pyproject.toml / setup.py / setup.cfg / .git，
        找到即返回该目录；找不到则返回当前工作目录。
        """
        markers = ("pyproject.toml", "setup.py", "setup.cfg", ".git")
        current = Path.cwd()
        for parent in [current, *current.parents]:
            if any((parent / m).exists() for m in markers):
                return str(parent)
        return str(current)

    @staticmethod
    def _create_persistence_backend(persistence_cfg: Any) -> Any:
        """根据配置创建持久化后端

        Args:
            persistence_cfg: SessionPersistenceConfig 实例

        Returns:
            PersistenceBackend 实例，或 None (未启用时)
        """
        if not persistence_cfg.enabled:
            return None

        if persistence_cfg.backend == "sqlite":
            from youmi.memory.backends.sqlite_backend import SQLiteBackend
            return SQLiteBackend(db_path=persistence_cfg.db_path)
        elif persistence_cfg.backend == "file":
            from youmi.memory.backends.file_backend import FileBackend
            return FileBackend(base_dir=persistence_cfg.base_dir)
        else:
            logger.warning(
                "Unknown persistence backend '%s', persistence disabled.",
                persistence_cfg.backend,
            )
            return None

    def _make_llm_call_fn(self) -> Any:
        """创建 llm_call 函数包装器，供 Compactor / Summary 策略使用

        Returns:
            async def(messages) -> str 函数，无 LLM 客户端时返回 None
        """
        if self._llm_client is None:
            return None

        client = self._llm_client

        async def llm_call(messages: list[dict[str, str]]) -> str:
            response = await client.chat(messages=messages)
            return response.content or ""

        return llm_call

    def register_tool(self, func: Any, **kwargs: Any) -> None:
        """快捷注册工具函数

        注册到本地 ToolRegistry。如果已连接 MCP (connect_mcp)，
        同时注册到 MCP LocalFunctionProvider。
        """
        self._tool_registry.register_function(func, **kwargs)
        # 如果已连接 MCP，同步注册到 provider
        if self._tool_bridge is not None:
            from youmi.mcp.provider import LocalFunctionProvider
            provider = getattr(self._tool_bridge, '_provider', None)
            if provider and isinstance(provider, LocalFunctionProvider):
                provider.register_function(func, **kwargs)

    def register_builtin_tools(self, exclude: list[str] | None = None) -> None:
        """注册内置工具到 Agent（不依赖 MCP）

        将 file_search / file_read / file_write / shell_exec 等内置工具
        直接注册到 Agent 的 ToolRegistry。

        优先级：
        - 如果显式传入 exclude，使用传入值（向后兼容）
        - 如果 exclude=None 且 config.tools 有声明，按 config.tools.builtin 装配
        - 否则全量注册（向后兼容）

        Args:
            exclude: 要排除的工具名称列表
        """
        from youmi.tools.builtin import BuiltinToolProvider

        # 确定排除列表：参数优先 > config > 默认全量
        effective_exclude = self._resolve_builtin_exclude(exclude)
        bp = BuiltinToolProvider(work_dir=self._env, exclude=effective_exclude)

        registered = 0
        for name, defn in bp._definitions.items():
            if name not in self._tool_registry:
                handler = bp._handlers.get(name)
                if handler:
                    self._tool_registry.register(defn, handler)
                    registered += 1

        # 注册 search_new_tools 兜底工具（structure.md §2 + §5.2）
        self._register_search_new_tools()

        logger.info("Agent '%s' registered %d builtin tools (exclude=%s)",
                     self.name, registered, effective_exclude)

    def _resolve_builtin_exclude(self, explicit_exclude: list[str] | None) -> list[str]:
        """计算内置工具排除列表

        优先级：显式参数 > config.tools.builtin > 空列表（全量）

        Args:
            explicit_exclude: 调用方显式传入的排除列表

        Returns:
            最终生效的排除列表
        """
        if explicit_exclude is not None:
            return explicit_exclude

        tools_cfg = self._config.tools
        if tools_cfg.builtin.include:
            # 白名单模式：排除 = 所有内置工具 - include
            from youmi.tools.builtin import BuiltinToolProvider
            all_names = list(BuiltinToolProvider(work_dir=self._env, exclude=[])._definitions.keys())
            included = set(tools_cfg.builtin.include)
            return [n for n in all_names if n not in included]

        # 黑名单模式（或全量）
        return list(tools_cfg.builtin.exclude)

    def _get_builtin_include(self) -> list[str] | None:
        """获取内置工具白名单（如有）

        Returns:
            白名单列表，None 表示不限定白名单（使用排除模式或全量）
        """
        tools_cfg = self._config.tools
        if tools_cfg.builtin.include:
            return list(tools_cfg.builtin.include)
        return None

    def connect_bus(
        self,
        broker: Any,  # MessageBroker
        workflow_id: str = "",
    ) -> None:
        """连接消息总线，启用 Agent 间通信

        连接后:
        - send_message() 通过 Broker 路由投递
        - pending_messages() / wait_for_message() 从 Broker 获取
        - 支持 workflow_id 隔离和跨进程通信

        Args:
            broker: MessageBroker 实例（InProcessBroker 或 BusClient）
            workflow_id: 工作流 ID，空字符串表示稍后由 Broker 分配
        """
        self._bus = broker
        self._workflow_id = workflow_id
        logger.info(
            "Agent '%s' connected to message bus (workflow=%s)",
            self.name, workflow_id or "pending",
        )

    @property
    def is_alive(self) -> bool:
        return not self._status.is_terminal

    # -----------------------------------------------------------------------
    # 生命周期管理
    # -----------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化 Agent — 创建后必须调用一次

        触发 on_initialize 钩子，子类在此装载 Skill/Tool。
        同时初始化记忆策略和持久化后端。如果已 connect_mcp，注册 Provider 到 Server。
        """
        self._ensure_status(AgentStatus.CREATED, "initialize")
        await self._memory.initialize()

        # Session 自动恢复 (P0: Persistence)
        persistence_cfg = self._config.memory_config.persistence
        if persistence_cfg.enabled and persistence_cfg.auto_restore:
            restored = await self._memory.restore_session()
            if restored:
                logger.info(
                    "Agent '%s' restored session '%s' (%d messages)",
                    self.name, self._memory.current_session_id, len(restored),
                )

        # 注册 MCP Provider (如果 connect_mcp 已调用)
        pending = getattr(self, '_mcp_pending_provider', None)
        if pending is not None:
            await self._mcp_server.register_provider(pending)
            self._mcp_pending_provider = None

        await self.on_initialize()

        # Compactor 的 llm_call 可以在 on_initialize 之后设置
        # (因为子类可能在此创建 LLM 客户端)
        if self._compactor is not None and self._compactor._llm_call is None and self._llm_client is not None:
            self._compactor._llm_call = self._make_llm_call_fn()

        # Prompt 动态组装器初始化 (P2: OC-6)
        self._prompt_assembler = PromptAssembler.from_system_prompt(
            self._config.system_prompt,
        )

        self._status = AgentStatus.IDLE
        logger.info("Agent '%s' [%s] initialized (memory=%s, mcp=%s, compaction=%s, persistence=%s).",
                     self.name, self.agent_id, self._memory.strategy_name,
                     'yes' if self._tool_bridge else 'no',
                     'yes' if self._compactor else 'no',
                     'yes' if self._memory.persistence else 'no')

    async def run(self, task: str, task_id: str = "") -> TaskResult:
        """执行任务 — 核心 ReAct 循环

        Args:
            task: 任务描述
            task_id: 可选的任务标识

        Returns:
            TaskResult 包含输出、状态、迭代次数等信息
        """
        self._ensure_status(AgentStatus.IDLE, "run")
        self._status = AgentStatus.RUNNING
        self._iteration_count = 0
        self._task_start_time = datetime.utcnow()

        await self.on_start(task)

        # P1: 任务自检 — 检查工具是否充足
        self_check = await self._self_check_task(task)
        if not self_check.is_sufficient:
            # 将缺失能力注入到 prompt 中
            if self_check.missing_capabilities and self._prompt_assembler is not None:
                check_hint = (
                    "\n\n[工具自检提醒] 当前工具可能不足以完成任务，"
                    "缺少的能力: " + "、".join(self_check.missing_capabilities) + "。"
                    "请尽力使用已有工具完成，或在必要时申请扩展工具。"
                )
                self._prompt_assembler.add_layer(PromptLayer(
                    name="task_self_check",
                    content=check_hint,
                    priority=70,
                ))
            # 如果需要申请工具
            if self_check.request_tools and self._bus is not None:
                for cap in self_check.missing_capabilities:
                    await self.request_tool(
                        tool_description=cap,
                        reason=f"完成以下任务需要: {task[:100]}",
                    )

        # 启动新 session (P0: Persistence)
        if self._memory.persistence is not None:
            self._memory.start_session()

        # 构建初始 conversation
        self._conversation = []
        if self._config.system_prompt:
            self._conversation.append({"role": "system", "content": self._config.system_prompt})
        self._conversation.append({"role": "user", "content": task})

        # 同步到记忆策略
        await self._memory.on_message("user", task, task_id=task_id)

        error_msg: str | None = None
        final_output: Any = None

        try:
            while self._iteration_count < self._config.max_iterations:
                self._iteration_count += 1
                logger.debug(
                    "Agent '%s' iteration %d/%d",
                    self.name, self._iteration_count, self._config.max_iterations,
                )

                # 1. Observe — 构建 LLM 输入
                observation = await self._observe()

                # 2. Think — 调用 LLM，解析 tool_calls
                thought = await self._think(observation)

                # 3. Act — 执行工具调用或直接回复
                action_result = _ActionResult()
                if thought.action_type == "tool_call":
                    action_result = await self._act(thought)
                elif thought.action_type == "respond":
                    # 直接回复，不需要执行 action
                    pass

                # 4. Reflect — 评估结果
                reflection = await self._reflect(observation, thought, action_result)

                # 判断是否达成目标
                if reflection.is_goal_met or not thought.should_continue:
                    final_output = thought.action_payload.get("response", reflection.summary)
                    # 最终回复同步到记忆
                    if final_output:
                        await self._memory.on_message("assistant", str(final_output))
                    self._status = AgentStatus.COMPLETED
                    break
            else:
                # 达到最大迭代次数
                final_output = f"达到最大迭代次数 ({self._config.max_iterations})，任务可能未完成。"
                self._status = AgentStatus.COMPLETED

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            self._status = AgentStatus.FAILED
            logger.exception("Agent '%s' failed: %s", self.name, error_msg)

        await self.on_stop(error_msg)
        await self._memory.on_session_end()

        result = TaskResult(
            agent_id=self.agent_id,
            task_id=task_id,
            status=self._status,
            output=final_output,
            iterations=self._iteration_count,
            error=error_msg,
            started_at=self._task_start_time,
            finished_at=datetime.utcnow(),
        )

        logger.info(
            "Agent '%s' finished: status=%s iterations=%d",
            self.name, self._status.value, self._iteration_count,
        )
        return result

    async def chat_turn(self, message: str) -> dict[str, Any]:
        """单轮对话 — 多轮聊天接口

        与 run() 不同，chat_turn():
        - 不改变 Agent 状态（保持 IDLE）
        - 跨轮次保持对话历史
        - 首次调用时自动初始化 conversation

        Args:
            message: 用户消息

        Returns:
            dict 包含:
            - response (str): Agent 回复
            - iterations (int): 本轮 ReAct 迭代次数
            - tool_calls (list[str]): 本轮调用的工具名列表
            - error (str | None): 错误信息
        """
        # 首次调用: 初始化 conversation 并设为 IDLE
        if not hasattr(self, '_chat_initialized') or not self._chat_initialized:
            if self._status == AgentStatus.CREATED:
                await self.initialize()
            self._conversation = []
            if self._config.system_prompt:
                self._conversation.append(
                    {"role": "system", "content": self._config.system_prompt}
                )
            self._chat_initialized = True
            # 确保状态为 IDLE
            if self._status not in (AgentStatus.IDLE, AgentStatus.RUNNING):
                self._status = AgentStatus.IDLE

        # 追加用户消息
        self._conversation.append({"role": "user", "content": message})
        await self._memory.on_message("user", message)

        response_text = ""
        tool_calls_made: list[str] = []
        iterations = 0
        error_msg: str | None = None

        try:
            for i in range(self._config.max_iterations):
                iterations += 1

                observation = await self._observe()
                thought = await self._think(observation)

                # 记录工具调用
                if thought.action_type == "tool_call":
                    tool_name = thought.action_payload.get("name", "")
                    tool_calls_made.append(tool_name)
                    await self._act(thought)

                reflection = await self._reflect(
                    observation, thought,
                    _ActionResult() if thought.action_type != "tool_call"
                    else _ActionResult(success=True),
                )

                if reflection.is_goal_met or not thought.should_continue:
                    response_text = thought.action_payload.get(
                        "response", reflection.summary,
                    )
                    break
            else:
                response_text = "达到最大迭代次数，回复可能不完整。"

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            response_text = f"处理出错: {error_msg}"
            logger.exception("chat_turn error: %s", error_msg)

        # 记录 assistant 回复到记忆
        if response_text:
            await self._memory.on_message("assistant", str(response_text))

        # 保持 IDLE 状态
        self._status = AgentStatus.IDLE

        return {
            "response": str(response_text),
            "iterations": iterations,
            "tool_calls": tool_calls_made,
            "error": error_msg,
        }

    async def chat_turn_stream(self, message: str):
        """流式单轮对话 — 异步生成器

        与 chat_turn() 行为一致，但以 yield 方式逐块产出文本，
        最后 yield 一个 dict 作为完整结果。

        Yields:
            str: 文本块（LLM 流式输出的 token）
            dict: 最终结果（最后一个 yield，包含 response/iterations/tool_calls/error）
        """
        # 首次调用: 初始化 conversation 并设为 IDLE
        if not hasattr(self, '_chat_initialized') or not self._chat_initialized:
            if self._status == AgentStatus.CREATED:
                await self.initialize()
            self._conversation = []
            if self._config.system_prompt:
                self._conversation.append(
                    {"role": "system", "content": self._config.system_prompt}
                )
            self._chat_initialized = True
            if self._status not in (AgentStatus.IDLE, AgentStatus.RUNNING):
                self._status = AgentStatus.IDLE

        # 追加用户消息
        self._conversation.append({"role": "user", "content": message})
        await self._memory.on_message("user", message)

        response_text = ""
        tool_calls_made: list[str] = []
        iterations = 0
        error_msg: str | None = None
        _consecutive_no_tool = 0  # 连续未调用工具的迭代计数
        _last_text_no_tool = ""  # 上一次纯文本输出（用于检测重复）

        try:
            for i in range(self._config.max_iterations):
                iterations += 1
                observation = await self._observe()

                # --- 流式 Think ---
                if self._llm_client is None:
                    # 无 LLM → 退化 echo
                    last_user = message
                    response_text = f"[无LLM客户端] 收到: {last_user}"
                    yield response_text
                    break

                # 准备 tools schema
                if self._tool_bridge is not None:
                    tools_schema = self._tool_bridge.to_openai_tools()
                else:
                    tools_schema = (
                        self._tool_registry.to_openai_tools()
                        if self._tool_registry else None
                    )

                # 流式调用 LLM
                chunk_text = ""
                try:
                    async for chunk in self._llm_client.chat_stream(
                        messages=observation.messages,
                        tools=tools_schema or None,
                    ):
                        chunk_text += chunk
                        yield chunk  # 逐块产出文本
                except httpx.HTTPStatusError as _http_err:
                    # 400 错误恢复：清理对话中的 tool/assistant+tool_calls 消息，
                    # 回退到纯文本模式继续对话
                    if _http_err.response.status_code == 400:
                        logger.warning(
                            "LLM 400 Bad Request, 尝试清理 tool 消息后重试"
                        )
                        self._conversation = [
                            m for m in self._conversation
                            if m.get("role") != "tool"
                            and not (
                                m.get("role") == "assistant"
                                and m.get("tool_calls")
                            )
                        ]
                        # 回退为非工具调用模式：不带 tools schema 重试
                        observation_clean = await self._observe()
                        async for chunk in self._llm_client.chat_stream(
                            messages=observation_clean.messages,
                            tools=None,
                        ):
                            chunk_text += chunk
                            yield chunk
                    else:
                        raise

                # 获取完整响应
                llm_response: LLMResponse = self._llm_client._last_stream_response

                # 追加 assistant 消息到 conversation
                assistant_msg = llm_response.raw_message
                self._conversation.append(assistant_msg)

                if llm_response.has_tool_calls:
                    # 有工具调用 → 执行后继续循环
                    tc = llm_response.tool_calls[0]
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, TypeError):
                        fn_args = {}

                    tool_calls_made.append(fn_name)
                    logger.info("Stream: LLM requests tool_call: %s(%s)", fn_name, fn_args)

                    thought = _Thought(
                        reasoning=llm_response.content,
                        action_type="tool_call",
                        action_payload={
                            "tool_call_id": tc.get("id", ""),
                            "name": fn_name,
                            "arguments": fn_args,
                            "all_tool_calls": llm_response.tool_calls,
                        },
                        should_continue=True,
                    )
                    await self._act(thought)
                    _consecutive_no_tool = 0  # 重置连续无工具计数

                    # yield 工具调用提示
                    yield f"\n*[🔧 调用工具: {fn_name}]*\n"
                    continue
                else:
                    # 无原生 tool_calls → 尝试文本回退检测
                    response_text = llm_response.content
                    available_tool_names = self._tool_registry.tool_names if self._tool_registry else []

                    if available_tool_names and response_text:
                        text_tool_calls = self._parse_text_tool_calls(
                            response_text, available_tool_names
                        )
                        if text_tool_calls:
                            # 从文本中检测到工具调用 → 执行并继续循环
                            tc = text_tool_calls[0]
                            fn_name = tc["function"]["name"]
                            try:
                                fn_args = json.loads(tc["function"]["arguments"])
                            except (json.JSONDecodeError, TypeError):
                                fn_args = {}

                            tool_calls_made.append(fn_name)
                            logger.info(
                                "Stream: text-fallback tool_call: %s(%s)",
                                fn_name, fn_args,
                            )

                            thought = _Thought(
                                reasoning=response_text,
                                action_type="tool_call",
                                action_payload={
                                    "tool_call_id": tc.get("id", ""),
                                    "name": fn_name,
                                    "arguments": fn_args,
                                    "all_tool_calls": text_tool_calls,
                                },
                                should_continue=True,
                            )
                            await self._act(thought)
                            _consecutive_no_tool = 0  # 重置连续无工具计数
                            yield f"\n*[🔧 文本回退调用工具: {fn_name}]*\n"
                            continue

                    # --- 委派强制: LLM 输出纯文本未调用工具 ---
                    if available_tool_names and len(response_text) > 30:
                        _consecutive_no_tool += 1

                        # 检测重复输出：如果与上一次输出相似度高，提前终止
                        if _last_text_no_tool and _text_similarity(
                            response_text, _last_text_no_tool
                        ) > 0.6:
                            logger.info(
                                "Delegation enforcement: repeated output detected, "
                                "breaking (consecutive_no_tool=%d)",
                                _consecutive_no_tool,
                            )
                            break
                        _last_text_no_tool = response_text

                        if _consecutive_no_tool <= 3:
                            # 检查是否有已创建但未运行的子 Agent
                            _unrun = []
                            _sub_agents = getattr(self, '_sub_agents', None)
                            if _sub_agents:
                                for _aid, _rec in _sub_agents.items():
                                    if _rec.result is None:
                                        _st = getattr(_rec.agent, 'status', None)
                                        _st_val = _st.value if _st else 'created'
                                        if _st_val in ('created', 'idle'):
                                            _unrun.append((_aid, _rec.role, _rec.task))

                            # 检查工作流追踪器是否已全部完成
                            _tracker = getattr(
                                getattr(self, '_gui_bridge', None), 'tracker', None
                            )
                            _wf_done = _tracker.all_done if _tracker else False

                            if _wf_done:
                                # 工作流已全部完成，提醒汇总结果
                                reminder = (
                                    "【系统提示】所有工作流步骤已全部完成。"
                                    "请立即汇总已有结果并回复用户，不要再调用任何工具。"
                                )
                            elif _unrun:
                                # 有未运行的子 Agent，提醒运行而非创建
                                _unrun_desc = ", ".join(
                                    f"{aid[:8]}({role})" for aid, role, _ in _unrun
                                )
                                _n = len(_unrun)
                                reminder = (
                                    f"【系统提醒】你有 {_n} 个已创建但未运行的子Agent：{_unrun_desc}。"
                                    f"请使用 run_sub_agent 运行它们，不要创建新的子Agent。"
                                    f"全部运行完后，汇总结果回复用户。"
                                )
                            elif tool_calls_made:
                                # 工作流中途停下：提醒继续执行
                                reminder = (
                                    "【系统提醒】你已经在执行工作流，但当前迭代未调用任何工具。"
                                    "请继续执行下一步：创建下一个子Agent，或运行已创建的子Agent"
                                    "（使用 run_sub_agent），或汇总结果回复用户。"
                                    "不要停下来等待用户输入。"
                                    "不要重复之前的文字。"
                                )
                            else:
                                # 从未调用工具：提醒开始行动
                                reminder = (
                                    "【系统提醒】你有可用的工具（"
                                    + "、".join(available_tool_names)
                                    + "）。你应该使用工具来完成任务，而不是直接回答。"
                                    "请立即调用 create_sub_agent 创建子Agent。"
                                    "不要输出计划性文字，直接行动。"
                                )
                            self._conversation.append({"role": "user", "content": reminder})
                            logger.info(
                                "Delegation enforcement: injected reminder "
                                "(consecutive_no_tool=%d, tool_calls_so_far=%d)",
                                _consecutive_no_tool, len(tool_calls_made),
                            )
                            yield "\n*[⚠️ 系统提醒：请继续使用工具推进工作流]*\n"
                            continue

                    # 连续无工具调用超限或无工具可用 → 纯文本回复完成
                    break

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            if not response_text:
                response_text = f"处理出错: {error_msg}"
            logger.exception("chat_turn_stream error: %s", error_msg)

        # 记录 assistant 回复到记忆
        if response_text:
            await self._memory.on_message("assistant", str(response_text))

        # 保持 IDLE 状态
        self._status = AgentStatus.IDLE

        # 最终结果
        yield {
            "response": str(response_text),
            "iterations": iterations,
            "tool_calls": tool_calls_made,
            "error": error_msg,
        }

    async def destroy(self) -> None:
        """销毁 Agent — 释放资源，不可复用"""
        if self._status == AgentStatus.DESTROYED:
            return
        await self.on_destroy()
        self._status = AgentStatus.DESTROYED
        logger.info("Agent '%s' [%s] destroyed.", self.name, self.agent_id)

    # -----------------------------------------------------------------------
    # 消息收发
    # -----------------------------------------------------------------------

    async def receive_message(self, message: AgentMessage) -> None:
        """接收来自其他 Agent 或系统的消息"""
        await self._message_queue.put(message)
        await self._memory.on_message(
            role="agent",
            content=message.content,
            from_agent=message.from_agent_id,
            message_id=message.message_id,
        )

        # Hook: message_received — 通知已收到消息
        if self._hook_registry.has_hooks(HookType.MESSAGE_RECEIVED):
            ctx = HookContext(
                hook_type=HookType.MESSAGE_RECEIVED,
                agent_id=self.agent_id,
                agent_name=self.name,
                message=message,
            )
            await self._hook_registry.invoke(HookType.MESSAGE_RECEIVED, ctx)

        await self.on_message_received(message)

    async def send_message(
        self,
        to_agent_id: str | None,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> AgentMessage:
        """发送消息给其他 Agent

        如果已 connect_bus，消息通过 Broker 路由投递；
        否则仅构造消息对象（实际投递由上层 Coordinator 负责）。
        """
        msg = AgentMessage(
            from_agent_id=self.agent_id,
            to_agent_id=to_agent_id,
            role=MessageRole.AGENT,
            content=content,
            metadata=metadata or {},
        )

        # Hook: message_sending — 发送前拦截/修改
        if self._hook_registry.has_hooks(HookType.MESSAGE_SENDING):
            ctx = HookContext(
                hook_type=HookType.MESSAGE_SENDING,
                agent_id=self.agent_id,
                agent_name=self.name,
                message=msg,
            )
            decision = await self._hook_registry.invoke(HookType.MESSAGE_SENDING, ctx)
            if decision.decision == HookDecisionType.BLOCK:
                logger.info(
                    "message_sending hook blocked message to '%s': %s",
                    to_agent_id, decision.reason,
                )
                return msg
            if decision.decision == HookDecisionType.MODIFY and "content" in decision.modified_data:
                msg = AgentMessage(
                    from_agent_id=self.agent_id,
                    to_agent_id=to_agent_id,
                    role=MessageRole.AGENT,
                    content=decision.modified_data["content"],
                    metadata=metadata or {},
                )

        if self._bus is not None:
            # 通过 Broker 投递
            from youmi.bus.message import WorkflowMessage, WorkflowMessageType
            wf_msg = WorkflowMessage(
                message_id=msg.message_id,
                workflow_id=self._workflow_id,
                from_agent_id=self.agent_id,
                to_agent_id=to_agent_id,
                msg_type=WorkflowMessageType.STATUS,
                role=MessageRole.AGENT,
                content=msg.content,
                metadata=msg.metadata,
            )
            await self._bus.publish(wf_msg)
        else:
            # 仅写入记忆（无 Broker 时退化为原行为）
            await self._memory.on_message(
                role="agent",
                content=msg.content,
                to_agent=to_agent_id,
                direction="outbound",
            )
        return msg

    async def pending_messages(self) -> list[AgentMessage]:
        """获取所有待处理消息 (非阻塞)

        如果已 connect_bus，从 Broker 获取；否则从本地队列获取。
        """
        if self._bus is not None:
            wf_messages = await self._bus.pending_messages(self.agent_id)
            return [m.to_agent_message() for m in wf_messages]

        messages: list[AgentMessage] = []
        while not self._message_queue.empty():
            messages.append(self._message_queue.get_nowait())
        return messages

    async def wait_for_message(self, timeout: float = 30.0) -> AgentMessage | None:
        """阻塞等待一条来自其他 Agent 的消息

        需要已 connect_bus。超时返回 None。

        Args:
            timeout: 超时秒数，默认 30 秒

        Returns:
            AgentMessage 或 None（超时时）

        Raises:
            RuntimeError: 未连接消息总线
        """
        if self._bus is None:
            raise RuntimeError(
                f"Agent '{self.name}' 未连接消息总线，请先调用 connect_bus()"
            )
        wf_msg = await self._bus.wait_for_message(self.agent_id, timeout=timeout)
        if wf_msg is None:
            return None
        agent_msg = wf_msg.to_agent_message()
        # 写入记忆（task/feedback 类型）
        if wf_msg.msg_type.writes_to_memory:
            await self._memory.on_message(
                role="agent",
                content=wf_msg.content,
                from_agent=wf_msg.from_agent_id,
                message_id=wf_msg.message_id,
            )
        return agent_msg


    # -----------------------------------------------------------------------
    # ReAct 阶段 — 子类可覆写
    # -----------------------------------------------------------------------

    # 文本回退: 从 LLM 纯文本中检测工具调用 (小模型不支持 function calling)
    _TEXT_TOOL_RE = re.compile(
        r'```(?:json)?\s*(\{[^`]*?"(?:tool_call|name)"[^`]*?\})\s*```',
        re.DOTALL,
    )

    @classmethod
    def _parse_text_tool_calls(cls, text: str, tool_names: list[str]) -> list[dict[str, Any]]:
        """从 LLM 纯文本输出中检测工具调用请求

        当模型不支持 function calling 时，可能以文本形式输出工具调用。
        支持的格式:
        1. JSON 代码块: {"tool_call": {"name": "...", "arguments": {...}}}
        2. JSON 代码块: {"name": "...", "arguments": {...}}
        3. 工具名匹配: tool_name({...})

        Returns:
            检测到的工具调用列表, 格式同 OpenAI tool_calls
        """
        results: list[dict[str, Any]] = []
        tool_name_set = set(tool_names)

        # 模式 1+2: 从 ```json ... ``` 代码块中提取
        for m in cls._TEXT_TOOL_RE.finditer(text):
            try:
                data = json.loads(m.group(1))
                # 提取 name 和 arguments
                if "tool_call" in data:
                    data = data["tool_call"]
                fn_name = data.get("name", "")
                fn_args = data.get("arguments", {})
                if fn_name and fn_name in tool_name_set:
                    if isinstance(fn_args, str):
                        fn_args = json.loads(fn_args)
                    results.append({
                        "id": f"call_text_{fn_name}",
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": json.dumps(fn_args, ensure_ascii=False),
                        },
                    })
            except (json.JSONDecodeError, TypeError):
                continue

        # 模式 3: tool_name({...}) 函数调用
        if not results:
            fn_re = re.compile(
                r'(' + '|'.join(re.escape(n) for n in tool_names) + r')\s*\(\s*(\{[^)]*\})\s*\)',
                re.DOTALL,
            )
            for m in fn_re.finditer(text):
                try:
                    fn_name = m.group(1)
                    fn_args = json.loads(m.group(2))
                    results.append({
                        "id": f"call_text_{fn_name}",
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": json.dumps(fn_args, ensure_ascii=False),
                        },
                    })
                except (json.JSONDecodeError, TypeError):
                    continue

        return results

    async def _observe(self) -> _Observation:
        """Observe: 收集当前上下文

        默认实现: 返回 self._conversation 列表 (含 system/user/assistant/tool 消息)。
        集成 Compactor: 在返回前检查并按需压缩上下文 (P0: Compaction)。
        集成 PromptAssembler: 动态组装 system prompt (P2: OC-6)。
        集成 Hook: 触发 before_prompt_build 钩子 (P2: OC-5)。
        子类可扩展: 注入额外记忆、环境信息等。
        """
        conversation = list(self._conversation)

        # P2: OC-6 — Prompt 动态组装
        if self._prompt_assembler is not None and len(self._prompt_assembler.layers) > 1:
            # 有额外层时，重新组装 system prompt
            assembled = self._prompt_assembler.assemble(
                max_tokens=self._config.llm_config.max_context_tokens // 4,
            )
            if conversation and conversation[0].get("role") == "system":
                conversation[0] = {"role": "system", "content": assembled}

        # P2: OC-5 — before_prompt_build 钩子
        if self._hook_registry.has_hooks(HookType.BEFORE_PROMPT_BUILD):
            ctx = HookContext(
                hook_type=HookType.BEFORE_PROMPT_BUILD,
                agent_id=self.agent_id,
                agent_name=self.name,
                messages=conversation,
            )
            decision = await self._hook_registry.invoke(
                HookType.BEFORE_PROMPT_BUILD, ctx,
            )
            if decision.decision == HookDecisionType.MODIFY and "messages" in decision.modified_data:
                conversation = decision.modified_data["messages"]

        # Compaction: 检查并按需压缩上下文
        if self._compactor is not None:
            conversation = await self._compactor.maybe_compact(conversation)

        return _Observation(messages=conversation)

    async def _think(self, observation: _Observation) -> _Thought:
        """Think: 调用 LLM 推理，自动处理 tool_calls

        默认实现:
        1. 将 observation.messages 发送给 LLM (附带已注册工具)
        2. 如果 LLM 返回 tool_calls → action_type="tool_call"
        3. 如果 LLM 返回纯文本 → action_type="respond"

        子类可覆写以实现自定义推理策略。
        """
        if self._llm_client is None:
            # 无 LLM 客户端时退化为 echo
            last_user = ""
            for msg in reversed(observation.messages):
                if msg.get("role") == "user":
                    last_user = msg.get("content", "")
                    break
            return _Thought(
                action_type="respond",
                action_payload={"response": f"[无LLM客户端] 收到: {last_user}"},
                should_continue=False,
            )

        # 准备 tools schema: 优先使用 MCP ToolBridge，否则用 ToolRegistry
        if self._tool_bridge is not None:
            tools_schema = self._tool_bridge.to_openai_tools()
        else:
            tools_schema = self._tool_registry.to_openai_tools() if self._tool_registry else None

        # P2: OC-5 — before_model_call 钩子
        messages_for_llm = list(observation.messages)
        if self._hook_registry.has_hooks(HookType.BEFORE_MODEL_CALL):
            ctx = HookContext(
                hook_type=HookType.BEFORE_MODEL_CALL,
                agent_id=self.agent_id,
                agent_name=self.name,
                messages=messages_for_llm,
            )
            decision = await self._hook_registry.invoke(HookType.BEFORE_MODEL_CALL, ctx)
            if decision.decision == HookDecisionType.BLOCK:
                logger.info("before_model_call hook blocked: %s", decision.reason)
                return _Thought(
                    action_type="respond",
                    action_payload={"response": f"[模型调用被拦截] {decision.reason}"},
                    should_continue=False,
                )
            if decision.decision == HookDecisionType.MODIFY and "messages" in decision.modified_data:
                messages_for_llm = decision.modified_data["messages"]

        # 调用 LLM
        response: LLMResponse = await self._llm_client.chat(
            messages=messages_for_llm,
            tools=tools_schema or None,
        )

        # P2: OC-5 — after_model_call 钩子
        if self._hook_registry.has_hooks(HookType.AFTER_MODEL_CALL):
            ctx = HookContext(
                hook_type=HookType.AFTER_MODEL_CALL,
                agent_id=self.agent_id,
                agent_name=self.name,
                messages=messages_for_llm,
                response=response,
            )
            await self._hook_registry.invoke(HookType.AFTER_MODEL_CALL, ctx)

        # 将 assistant 回复追加到 conversation
        assistant_msg = response.raw_message
        self._conversation.append(assistant_msg)

        if response.has_tool_calls:
            # 取第一个 tool_call (可扩展支持多个)
            tc = response.tool_calls[0]
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, TypeError):
                fn_args = {}

            logger.info("LLM requests tool_call: %s(%s)", fn_name, fn_args)
            return _Thought(
                reasoning=response.content,
                action_type="tool_call",
                action_payload={
                    "tool_call_id": tc.get("id", ""),
                    "name": fn_name,
                    "arguments": fn_args,
                    "all_tool_calls": response.tool_calls,
                },
                should_continue=True,
            )

        # 无原生 tool_calls → 尝试文本回退检测
        available_tool_names = self._tool_registry.tool_names if self._tool_registry else []
        if available_tool_names and response.content:
            text_tool_calls = self._parse_text_tool_calls(
                response.content, available_tool_names
            )
            if text_tool_calls:
                tc = text_tool_calls[0]
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, TypeError):
                    fn_args = {}
                logger.info("Text-fallback tool_call: %s(%s)", fn_name, fn_args)
                return _Thought(
                    reasoning=response.content,
                    action_type="tool_call",
                    action_payload={
                        "tool_call_id": tc.get("id", ""),
                        "name": fn_name,
                        "arguments": fn_args,
                        "all_tool_calls": text_tool_calls,
                    },
                    should_continue=True,
                )

        # 纯文本回复
        return _Thought(
            reasoning=response.content,
            action_type="respond",
            action_payload={"response": response.content},
            should_continue=False,
        )

    async def _act(self, thought: _Thought) -> _ActionResult:
        """Act: 执行 Think 阶段决定的行动

        默认实现: 根据 action_type 分发。子类可扩展。
        """
        try:
            if thought.action_type == "tool_call":
                return await self._execute_tool_call(thought.action_payload)
            elif thought.action_type == "skill_call":
                return await self._execute_skill_call(thought.action_payload)
            elif thought.action_type == "delegate":
                return await self._execute_delegation(thought.action_payload)
            else:
                return _ActionResult(success=True, output=thought.action_payload)
        except Exception as exc:
            return _ActionResult(success=False, error=str(exc))

    async def _reflect(
        self,
        observation: _Observation,
        thought: _Thought,
        action_result: _ActionResult,
    ) -> _Reflection:
        """Reflect: 评估行动结果，决定是否继续

        默认实现: 如果 action_type="respond" 则认为目标达成。
        子类可扩展: 调用 LLM 进行更复杂的评估。
        """
        if thought.action_type == "respond":
            return _Reflection(
                is_goal_met=True,
                summary=thought.action_payload.get("response", ""),
                should_continue=False,
            )

        if not action_result.success:
            return _Reflection(
                is_goal_met=False,
                summary=f"行动失败: {action_result.error}",
                should_continue=True,
                next_hint="尝试换一种方式完成任务",
            )

        return _Reflection(
            is_goal_met=False,
            summary=f"行动成功: {action_result.output}",
            should_continue=True,
        )

    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # 生命周期钩子 — 子类可覆写
    # -----------------------------------------------------------------------

    async def on_initialize(self) -> None:
        """初始化钩子 — 装载 Skill/Tool/建立连接等"""
        pass

    async def on_start(self, task: str) -> None:
        """任务开始钩子"""
        pass

    async def on_stop(self, error: str | None) -> None:
        """任务结束钩子"""
        pass

    async def on_destroy(self) -> None:
        """销毁钩子 — 释放资源、关闭连接、卸载插件"""
        # P2: OC-5 — 自动卸载所有插件
        if hasattr(self, '_plugin_manager') and len(self._plugin_manager) > 0:
            await self._plugin_manager.unregister_all()
        pass

    async def on_message_received(self, message: AgentMessage) -> None:
        """消息接收钩子"""
        pass

    async def handoff(
        self,
        target_agent_id: str,
        task: str,
        message_template: str = "",
    ) -> _ActionResult:
        """显式委派任务给另一个 Agent (P1: Handoff)

        与 _execute_delegation 相同，但作为公共方法供子类或外部调用。

        Args:
            target_agent_id: 目标 Agent ID
            task: 任务描述
            message_template: 消息模板 (可选)

        Returns:
            _ActionResult 包含委派结果
        """
        return await self._execute_delegation({
            "target_agent_id": target_agent_id,
            "task": task,
            "message_template": message_template,
            "depth": 0,
        })

    def match_handoff_rule(self, message_content: str) -> HandoffRule | None:
        """根据消息内容匹配委派规则 (P1: Handoff)

        按 trigger_keywords 匹配，第一个匹配的规则优先。

        Args:
            message_content: 用户/Agent 消息内容

        Returns:
            匹配的 HandoffRule，无匹配时返回 None
        """
        handoff_cfg = self._config.handoff
        if not handoff_cfg.enabled:
            return None

        content_lower = message_content.lower()
        for rule in handoff_cfg.rules:
            if not rule.enabled:
                continue
            for kw in rule.trigger_keywords:
                if kw.lower() in content_lower:
                    return rule
        return None

    # -----------------------------------------------------------------------
    # P2: 插件与 Prompt 便捷接口
    # -----------------------------------------------------------------------

    async def install_plugin(self, plugin: Plugin) -> None:
        """安装插件 (P2: OC-5 便捷方法)

        将插件注册到 HookRegistry 并安装到 PluginManager。
        建议在 on_initialize 钩子中调用。

        Args:
            plugin: Plugin 实例
        """
        await self._plugin_manager.register(plugin)

    def add_prompt_layer(self, layer: PromptLayer) -> None:
        """添加 Prompt 层 (P2: OC-6 便捷方法)

        将一个新的 PromptLayer 添加到 PromptAssembler。
        如果 PromptAssembler 未初始化，则自动创建。

        Args:
            layer: PromptLayer 实例
        """
        if self._prompt_assembler is None:
            self._prompt_assembler = PromptAssembler.from_system_prompt(
                self._config.system_prompt,
            )
        self._prompt_assembler.add_layer(layer)

    # -----------------------------------------------------------------------
    # 序列化 & 诊断
    # -----------------------------------------------------------------------

    def to_summary(self) -> dict[str, Any]:
        """Agent 状态摘要 (用于日志/调试/Registry)"""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "status": self._status.value,
            "env": self._env,
            "role": self._config.metadata.role,
            "tags": self._config.metadata.tags,
            "capabilities": self._config.metadata.capabilities,
            "iterations": self._iteration_count,
            "bus_connected": self._bus is not None,
            "workflow_id": self._workflow_id,
            "metadata": self._config.metadata.model_dump(),
        }

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"id={self.agent_id!r} "
            f"name={self.name!r} "
            f"status={self._status.value}>"
        )

    # -----------------------------------------------------------------------
    # P1: 任务自检与工具申请
    # -----------------------------------------------------------------------

    async def _self_check_task(self, task: str) -> _TaskSelfCheck:
        """任务自检 — 检查当前工具是否足以完成任务

        在 run() 的 on_start() 之后、ReAct 循环之前调用。
        无 LLM 客户端时退化为乐观策略（认为工具充足）。

        Args:
            task: 任务描述

        Returns:
            _TaskSelfCheck 结果
        """
        # 收集可用工具
        available_tools: list[str] = list(self._tool_registry.tool_names) if self._tool_registry else []
        if self._tool_bridge is not None:
            bridge_tools = getattr(self._tool_bridge, '_allowed_tools', None)
            if bridge_tools:
                available_tools = list(set(available_tools) | set(bridge_tools))

        if not available_tools:
            return _TaskSelfCheck(is_sufficient=True)  # 无工具场景不做自检

        if self._llm_client is None:
            return _TaskSelfCheck(is_sufficient=True)  # 无 LLM 时乐观

        # 构造自检 prompt
        check_prompt = (
            f"你是一个工具充足性评估专家。\n"
            f"\n任务描述: {task}\n"
            f"\n当前可用工具: {', '.join(available_tools)}\n"
            f"\n请判断上述工具是否足以完成任务。"
            f"回复 JSON 格式: "
            f'{{"is_sufficient": true/false, "missing": ["缺少的能力1", ...], "suggestion": "建议"}}\n'
            f"只回复 JSON，不要其他内容。"
        )

        try:
            response = await self._llm_client.chat(
                messages=[{"role": "user", "content": check_prompt}],
            )
            content = response.content.strip()
            # 尝试解析 JSON
            import re
            json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                is_sufficient = data.get("is_sufficient", True)
                missing = data.get("missing", [])
                suggestion = data.get("suggestion", "")
                return _TaskSelfCheck(
                    is_sufficient=is_sufficient,
                    missing_capabilities=missing,
                    suggestion=suggestion,
                    request_tools=not is_sufficient and bool(missing),
                )
        except Exception as exc:
            logger.debug("Self-check LLM call failed (non-critical): %s", exc)

        return _TaskSelfCheck(is_sufficient=True)  # 解析失败时乐观

    async def request_tool(self, tool_description: str, reason: str) -> bool:
        """向 MasterAgent 申请扩展工具权限

        通过消息总线发送 TOOL_REQUEST 消息，等待 TOOL_RESPONSE 回复。
        未连接消息总线时直接返回 False。

        Args:
            tool_description: 需要的工具能力描述
            reason: 申请原因

        Returns:
            True 表示批准，False 表示拒绝或无法申请
        """
        if self._bus is None:
            logger.warning(
                "Agent '%s' cannot request tool: not connected to bus",
                self.name,
            )
            return False

        from youmi.bus.message import WorkflowMessage, WorkflowMessageType

        request_payload = json.dumps({
            "tool_description": tool_description,
            "reason": reason,
        }, ensure_ascii=False)

        wf_msg = WorkflowMessage(
            workflow_id=self._workflow_id,
            from_agent_id=self.agent_id,
            to_agent_id=None,  # 广播，MasterAgent 会接收
            msg_type=WorkflowMessageType.TOOL_REQUEST,
            role=MessageRole.AGENT,
            content=request_payload,
            metadata={"request_type": "tool_extension"},
        )
        await self._bus.publish(wf_msg)

        logger.info(
            "Agent '%s' requesting tool: %s (reason: %s)",
            self.name, tool_description, reason[:60],
        )

        # 等待回复
        try:
            response = await self._bus.wait_for_message(
                self.agent_id, timeout=15.0,
            )
            if response is None:
                logger.warning("Tool request timed out for agent '%s'", self.name)
                return False

            if response.msg_type == WorkflowMessageType.TOOL_RESPONSE:
                resp_data = json.loads(response.content)
                approved = resp_data.get("approved", False)
                if approved:
                    logger.info("Tool request approved for agent '%s'", self.name)
                else:
                    logger.info(
                        "Tool request denied for agent '%s': %s",
                        self.name, resp_data.get("reason", ""),
                    )
                return approved
        except Exception as exc:
            logger.debug("Tool request wait failed: %s", exc)

        return False

    # -----------------------------------------------------------------------
    # 内部工具
    # -----------------------------------------------------------------------

    def _ensure_status(self, expected: AgentStatus, action: str) -> None:
        if self._status != expected:
            raise RuntimeError(
                f"Cannot {action}: Agent '{self.name}' is in status "
                f"'{self._status.value}', expected '{expected.value}'"
            )


# ---------------------------------------------------------------------------
# 向后兼容 re-export — 确保 from youmi.core.agent import XXX 仍然可用
# ---------------------------------------------------------------------------
# 所有数据模型已从 youmi.core.models 导入并在上方 re-export
# 工具执行方法由 ToolExecutionMixin 提供
# MCP 集成方法由 MCPIntegrationMixin 提供
