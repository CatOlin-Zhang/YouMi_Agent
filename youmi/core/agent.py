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
    LLMConfig,
    MemoryConfig,
    MessageRole,
    RetryPolicy,
    ToolsConfig,
)
from youmi.core.tool import ToolRegistry
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


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

class AgentStatus(str, Enum):
    """Agent 生命周期状态

    状态流转:
        CREATED → IDLE → RUNNING → COMPLETED
                        ↘         ↗
                         WAITING
                        ↘         ↗
                         FAILED
                                 → DESTROYED
    """

    CREATED = "created"          # 已创建，未初始化
    IDLE = "idle"                # 初始化完成，等待任务
    RUNNING = "running"          # 正在执行 ReAct 循环
    WAITING = "waiting"          # 等待外部资源 / 其他 Agent
    COMPLETED = "completed"      # 任务成功完成
    FAILED = "failed"            # 任务失败
    DESTROYED = "destroyed"      # 已销毁，不可复用

    @property
    def is_terminal(self) -> bool:
        return self in (AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.DESTROYED)

    @property
    def is_active(self) -> bool:
        return self in (AgentStatus.RUNNING, AgentStatus.WAITING)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

class AgentConfig(BaseModel):
    """Agent 配置 — 纯数据，可序列化，可从 YAML/JSON 加载

    涵盖本地运行与远程 API 两种场景:
    - llm_config.base_url 指向远程服务 → API 模式
    - llm_config.provider=local + base_url=localhost → 本地模式
    """

    agent_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    name: str = Field(default="Agent", description="Agent 实例名称")
    system_prompt: str = Field(default="", description="系统提示词")

    # LLM
    llm_config: LLMConfig = Field(default_factory=LLMConfig)

    # 记忆
    memory_config: MemoryConfig = Field(default_factory=MemoryConfig)

    # 行为控制
    max_iterations: int = Field(default=20, gt=0, description="ReAct 最大迭代次数")
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    # 授权范围 (空列表表示不限制，由上层工厂填充)
    allowed_skills: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)

    # 工具装配（声明式注册，为空时保持向后兼容）
    tools: ToolsConfig = Field(
        default_factory=ToolsConfig,
        description="工具装配配置：声明 Agent 需要注册哪些工具",
    )

    # 对外标签
    metadata: AgentMetadata = Field(default_factory=AgentMetadata)

    # 自定义扩展
    extra: dict[str, Any] = Field(default_factory=dict)

    # 运行环境路径（逻辑工作目录，默认继承项目根目录）
    env: str = Field(
        default="",
        description="Agent 运行环境路径（逻辑工作目录），为空时自动检测项目根目录",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# 执行结果
# ---------------------------------------------------------------------------

class TaskResult(BaseModel):
    """Agent 任务执行结果"""

    agent_id: str
    task_id: str = ""
    status: AgentStatus = AgentStatus.COMPLETED
    output: Any = None                     # 主输出 (文本 / dict / list)
    iterations: int = 0                    # 实际执行的 ReAct 迭代次数
    error: str | None = None               # 失败时的错误信息
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def success(self) -> bool:
        return self.status == AgentStatus.COMPLETED


# ---------------------------------------------------------------------------
# ReAct 循环中间数据
# ---------------------------------------------------------------------------

class _Observation(BaseModel):
    """Observe 阶段输出 — 收集到的上下文"""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    memories: list[dict[str, Any]] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class _Thought(BaseModel):
    """Think 阶段输出 — LLM 的推理结果"""
    reasoning: str = ""
    action_type: str = "respond"           # "tool_call" | "skill_call" | "respond" | "delegate"
    action_payload: dict[str, Any] = Field(default_factory=dict)
    should_continue: bool = True


class _ActionResult(BaseModel):
    """Act 阶段输出 — 行动执行结果"""
    success: bool = True
    output: Any = None
    error: str | None = None


class _Reflection(BaseModel):
    """Reflect 阶段输出 — 对结果的评估"""
    is_goal_met: bool = False
    summary: str = ""
    should_continue: bool = False
    next_hint: str = ""


# ---------------------------------------------------------------------------
# Agent 基类
# ---------------------------------------------------------------------------

class Agent:
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

        self._memory = MemoryManager(
            agent_id=config.agent_id,
            strategy=strategy,
            config=strategy_config,
            llm_call=llm_call,
        )
        # 保留旧版 MemoryAdapter 引用 (兼容)
        self._legacy_memory = MemoryAdapter(agent_id=config.agent_id)

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

    def connect_guardian(
        self,
        guardian_id: str,
        broker: Any = None,  # MessageBroker
        workflow_id: str = "",
    ) -> None:
        """连接 ToolGuardianAgent，启用工具错误汇报闭环

        连接后，Agent 在工具调用失败时会自动分类错误并汇报给 ToolGuardianAgent。
        ToolGuardianAgent 根据汇报修正工具描述或生成代码修改建议。

        Args:
            guardian_id: ToolGuardianAgent 的 agent_id
            broker: 用于向 guardian 发送消息的 MessageBroker（可复用 connect_bus 的 broker）
            workflow_id: 工作流 ID（可复用 connect_bus 的 workflow_id）
        """
        self._tool_guardian_id = guardian_id
        self._tool_guardian_bus = broker or self._bus
        self._guardian_workflow_id = workflow_id or self._workflow_id
        logger.info(
            "Agent '%s' connected to ToolGuardian '%s'",
            self.name, guardian_id,
        )

    async def report_tool_issue(
        self,
        tool_name: str,
        error_message: str,
        call_arguments: dict[str, Any] | None = None,
        error_traceback: str = "",
        issue_type: str | None = None,
        suggestion: str = "",
    ) -> None:
        """向 ToolGuardianAgent 汇报工具调用问题

        自动分类错误类型（或由调用方显式指定 issue_type），
        构造 ToolIssueReport 并通过消息总线发送给 ToolGuardianAgent。

        错误分类规则（issue_type 为 None 时自动推断）:
        - 包含 "not found" / "未注册" → UNCLEAR_DESCRIPTION
        - 包含 "boundary" / "out of range" / "invalid" / "类型" / "边界" → PARAMETER_BOUNDARY
        - 包含 "not supported" / "不支持" → MISSING_FEATURE
        - 包含 "timeout" / "connection" / "超时" → ERROR_HANDLING
        - 其他 → UNEXPECTED_BEHAVIOR

        Args:
            tool_name: 出问题的工具名称
            error_message: 错误信息
            call_arguments: 调用时的参数
            error_traceback: 完整异常 traceback（可选）
            issue_type: 显式指定问题类型（ToolIssueType 值），None 则自动推断
            suggestion: 汇报者的初步修改建议
        """
        from youmi.mcp.protocol import ToolIssueType, ToolIssueReport

        # 自动推断 issue_type
        if issue_type is None:
            issue_type = self._classify_tool_error(error_message)
        elif isinstance(issue_type, str):
            try:
                issue_type = ToolIssueType(issue_type)
            except ValueError:
                issue_type = ToolIssueType.OTHER

        report = ToolIssueReport(
            reporter_agent_id=self.agent_id,
            tool_name=tool_name,
            issue_type=issue_type,
            error_message=error_message,
            call_arguments=call_arguments or {},
            error_traceback=error_traceback,
            suggestion=suggestion,
        )

        logger.info(
            "Agent '%s' reporting tool issue: tool=%s type=%s error=%s",
            self.name, tool_name, issue_type.value, error_message[:80],
        )

        # 通过消息总线发送给 ToolGuardianAgent
        if self._tool_guardian_bus is not None and self._tool_guardian_id:
            from youmi.bus.message import WorkflowMessage, WorkflowMessageType
            wf_msg = WorkflowMessage(
                workflow_id=getattr(self, '_guardian_workflow_id', self._workflow_id),
                from_agent_id=self.agent_id,
                to_agent_id=self._tool_guardian_id,
                msg_type=WorkflowMessageType.FEEDBACK,
                role=MessageRole.AGENT,
                content=report.model_dump_json(),
                metadata={
                    "report_type": "tool_issue",
                    "tool_name": tool_name,
                    "issue_type": issue_type.value,
                },
            )
            await self._tool_guardian_bus.publish(wf_msg)
        else:
            logger.warning(
                "Agent '%s' cannot report tool issue: guardian not connected",
                self.name,
            )

    @staticmethod
    def _classify_tool_error(error_message: str) -> Any:
        """根据错误信息自动推断工具问题类型"""
        from youmi.mcp.protocol import ToolIssueType

        msg_lower = error_message.lower()

        if any(kw in msg_lower for kw in ("not found", "未注册", "未找到", "不存在")):
            return ToolIssueType.UNCLEAR_DESCRIPTION

        if any(kw in msg_lower for kw in (
            "boundary", "out of range", "invalid", "type error",
            "类型", "边界", "超出范围", "参数错误", "不在", "列表中",
        )):
            return ToolIssueType.PARAMETER_BOUNDARY

        if any(kw in msg_lower for kw in ("not supported", "不支持", "未实现")):
            return ToolIssueType.MISSING_FEATURE

        if any(kw in msg_lower for kw in ("timeout", "connection", "超时", "连接")):
            return ToolIssueType.ERROR_HANDLING

        return ToolIssueType.UNEXPECTED_BEHAVIOR

    def connect_mcp(
        self,
        server: Any,  # MCPServer
        provider_id: str = "local",
        builtin_tools: bool = True,
    ) -> None:
        """连接 MCP Server，启用统一工具调用层

        连接后:
        - Agent 通过 ToolBridge 调用工具 (权限 + MCP 路由)
        - 已注册的 ToolRegistry 工具自动迁移到 MCP Provider
        - 内置工具按 config.tools 声明装配（无声明时全量注册）
        - _think() 和 _execute_tool_call() 自动切换为 MCP 模式

        Args:
            server: MCPServer 实例
            provider_id: 本地工具 Provider 的 ID
            builtin_tools: 是否自动注册内置工具，默认 True
        """
        from youmi.mcp.provider import LocalFunctionProvider
        from youmi.mcp.client import MCPClient
        from youmi.mcp.bridge import ToolBridge

        # 创建 LocalFunctionProvider，迁移已有工具
        provider = LocalFunctionProvider(provider_id=provider_id)
        for name, defn in self._tool_registry._definitions.items():
            handler = self._tool_registry._handlers.get(name)
            if handler:
                provider.register(defn, handler)

        # 注册内置工具 — 按 config.tools.builtin 声明装配
        if builtin_tools:
            from youmi.tools.builtin import BuiltinToolProvider

            # 计算排除列表（复用 register_builtin_tools 的逻辑）
            effective_exclude = self._resolve_builtin_exclude(None)
            bp = BuiltinToolProvider(work_dir=self._env, exclude=effective_exclude)

            registered = 0
            for name, defn in bp._definitions.items():
                if name not in provider._definitions:  # 不覆盖已有工具
                    handler = bp._handlers.get(name)
                    if handler:
                        provider.register(defn, handler)
                        # 同步到 ToolRegistry
                        self._tool_registry.register(defn, handler)
                        registered += 1

            logger.debug(
                "Agent '%s' assembled %d builtin tools via MCP (exclude=%s)",
                self.name, registered, effective_exclude,
            )

        # 注册到 MCPServer (异步操作在 initialize 或首次调用时完成)
        self._mcp_server = server
        self._mcp_provider = provider

        # 创建 MCPClient + ToolBridge
        client = MCPClient(server=server)
        allowed = self._config.allowed_tools or None
        self._tool_bridge = ToolBridge(
            agent_id=self.agent_id,
            mcp_client=client,
            allowed_tools=allowed,
        )
        self._tool_bridge._provider = provider  # 保留引用供 register_tool 使用
        self._mcp_pending_provider = provider  # 标记需要在 initialize 时注册

        logger.info("Agent '%s' connected to MCP (provider=%s, builtin=%s)",
                     self.name, provider_id, builtin_tools)

    @property
    def is_alive(self) -> bool:
        return not self._status.is_terminal

    # -----------------------------------------------------------------------
    # 生命周期管理
    # -----------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化 Agent — 创建后必须调用一次

        触发 on_initialize 钩子，子类在此装载 Skill/Tool。
        同时初始化记忆策略。如果已 connect_mcp，注册 Provider 到 Server。
        """
        self._ensure_status(AgentStatus.CREATED, "initialize")
        await self._memory.initialize()

        # 注册 MCP Provider (如果 connect_mcp 已调用)
        pending = getattr(self, '_mcp_pending_provider', None)
        if pending is not None:
            await self._mcp_server.register_provider(pending)
            self._mcp_pending_provider = None

        await self.on_initialize()
        self._status = AgentStatus.IDLE
        logger.info("Agent '%s' [%s] initialized (memory=%s, mcp=%s).",
                     self.name, self.agent_id, self._memory.strategy_name,
                     'yes' if self._tool_bridge else 'no')

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
        _delegation_reminded = False  # 委派提醒标记（每轮仅一次）

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
                async for chunk in self._llm_client.chat_stream(
                    messages=observation.messages,
                    tools=tools_schema or None,
                ):
                    chunk_text += chunk
                    yield chunk  # 逐块产出文本

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
                            yield f"\n*[🔧 文本回退调用工具: {fn_name}]*\n"
                            continue

                    # --- 委派强制: 有工具却未调用 → 注入提醒并重试一次 ---
                    if (
                        available_tool_names
                        and not tool_calls_made
                        and not _delegation_reminded
                        and len(response_text) > 30
                    ):
                        _delegation_reminded = True
                        reminder = (
                            "【系统提醒】你有可用的工具（"
                            + "、".join(available_tool_names)
                            + "）。你应该使用工具来完成任务，而不是直接回答。"
                            "请先分析任务，再调用工具创建子Agent来执行。"
                            "现在请使用工具。"
                        )
                        self._conversation.append({"role": "user", "content": reminder})
                        logger.info("Delegation enforcement: injected reminder")
                        yield "\n*[⚠️ 系统提醒：请使用工具委派任务]*\n"
                        continue

                    # 纯文本回复 → 完成
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
                content=content,
                metadata=metadata or {},
            )
            await self._bus.publish(wf_msg)
        else:
            # 仅写入记忆（无 Broker 时退化为原行为）
            await self._memory.on_message(
                role="agent",
                content=content,
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
        子类可扩展: 注入额外记忆、环境信息等。
        """
        return _Observation(messages=list(self._conversation))

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

        # 调用 LLM
        response: LLMResponse = await self._llm_client.chat(
            messages=observation.messages,
            tools=tools_schema or None,
        )

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
    # 行动执行器 — 子类/上层可覆写
    # -----------------------------------------------------------------------

    async def _execute_tool_call(self, payload: dict[str, Any]) -> _ActionResult:
        """执行工具调用

        优先通过 MCP ToolBridge (权限 + 路由)，
        退化到 ToolRegistry 直接执行。

        失败时自动向 ToolGuardianAgent 汇报（如果已连接）。

        流程:
        1. 从 payload 提取工具名和参数
        2. 通过 ToolBridge 或 ToolRegistry 执行
        3. 将结果以 tool role 消息追加到 conversation
        4. 同步写入记忆系统
        5. 失败时自动汇报给 ToolGuardianAgent
        """
        name = payload.get("name", "")
        arguments = payload.get("arguments", {})
        tool_call_id = payload.get("tool_call_id", "")
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
        """委托子任务给其他 Agent — 由 Coordinator 注入实际实现"""
        return _ActionResult(
            success=False,
            error="Delegation 未配置，请在 on_initialize 中设置 delegate_handler",
        )

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
        """销毁钩子 — 释放资源、关闭连接"""
        pass

    async def on_message_received(self, message: AgentMessage) -> None:
        """消息接收钩子"""
        pass

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
    # 内部工具
    # -----------------------------------------------------------------------

    def _ensure_status(self, expected: AgentStatus, action: str) -> None:
        if self._status != expected:
            raise RuntimeError(
                f"Cannot {action}: Agent '{self.name}' is in status "
                f"'{self._status.value}', expected '{expected.value}'"
            )
