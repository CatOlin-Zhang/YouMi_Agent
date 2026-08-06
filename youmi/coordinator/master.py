"""
MasterAgent — 主协调 Agent

继承自 Agent 基类，覆写 ReAct 阶段以实现：
1. 与用户进行初始对话，理解任务需求
2. 分析任务并决定需要哪些子 Agent
3. 实例化子 Agent 并分配任务
4. 收集子 Agent 结果并汇总反馈

子 Agent 配置约定：
    每个 Agent 的配置存放在 youmi/agents/<agent_name>/ 目录下，
    包含 config.yaml 配置文件。MasterAgent 通过 load_agent_config()
    加载配置并实例化子 Agent。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from youmi.core.agent import Agent, AgentConfig, AgentStatus, TaskResult
from youmi.core.tool import ToolDefinition, ToolParameter, ToolRegistry
from youmi.core.types import (
    AgentMessage,
    AgentMetadata,
    LLMConfig,
    MemoryConfig,
)
from youmi.agents import get_agent_dir, list_agents, load_agent_config
from youmi.llm.client import LLMClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 子 Agent 注册表 — 记录 MasterAgent 创建的所有子 Agent
# ---------------------------------------------------------------------------

class SubAgentRecord:
    """子 Agent 运行记录"""

    def __init__(self, agent: Agent, role: str, task: str) -> None:
        self.agent = agent
        self.role = role
        self.task = task
        self.result: TaskResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent.agent_id,
            "name": self.agent.name,
            "role": self.role,
            "task": self.task,
            "status": self.agent.status.value,
            "result": self.result.model_dump() if self.result else None,
        }


# ---------------------------------------------------------------------------
# MasterAgent
# ---------------------------------------------------------------------------

class MasterAgent(Agent):
    """主协调 Agent

    MasterAgent 是框架的入口 Agent，负责：
    - 与用户进行初始对话
    - 分析任务并拆解子任务
    - 按需实例化子 Agent
    - 编排执行流程并收集结果

    用法::

        from youmi.coordinator.master import MasterAgent

        master = MasterAgent.from_config_dir()  # 从 youmi/agents/master/ 加载配置
        await master.initialize()
        result = await master.run("帮我写一个 Python 排序算法")

    子类可通过覆写 `_analyze_task()` 和 `_create_sub_agents()` 扩展分析逻辑。
    """

    def __init__(
        self,
        config: AgentConfig,
        memory_strategy: str | None = None,
        llm_call: Any | None = None,
    ) -> None:
        super().__init__(config, memory_strategy=memory_strategy, llm_call=llm_call)

        # 子 Agent 注册表
        self._sub_agents: dict[str, SubAgentRecord] = {}

        # 注册 MasterAgent 内置工具
        self._register_master_tools()

    # -----------------------------------------------------------------------
    # 工厂方法
    # -----------------------------------------------------------------------

    @classmethod
    def from_config_dir(
        cls,
        agent_name: str = "master",
        overrides: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> MasterAgent:
        """从 youmi/agents/<agent_name>/config.yaml 加载配置并创建实例

        Args:
            agent_name: Agent 配置目录名，默认 "master"
            overrides: 覆盖配置项（优先级高于 YAML）
            **kwargs: 传给 __init__ 的额外参数

        Returns:
            MasterAgent 实例
        """
        data = load_agent_config(agent_name)

        # 应用覆盖
        if overrides:
            data.update(overrides)

        # 分离已知字段
        llm_data = data.pop("llm_config", {})
        memory_data = data.pop("memory_config", {})
        metadata_data = data.pop("metadata", {})

        config = AgentConfig(
            llm_config=LLMConfig(**llm_data),
            memory_config=MemoryConfig(**memory_data),
            metadata=AgentMetadata(**metadata_data),
            **data,
        )
        return cls(config, **kwargs)

    # -----------------------------------------------------------------------
    # 子 Agent 管理
    # -----------------------------------------------------------------------

    def create_sub_agent(
        self,
        role: str,
        name: str = "",
        system_prompt: str = "",
        task: str = "",
        allowed_tools: list[str] | None = None,
        env: str = "",
        config_overrides: dict[str, Any] | None = None,
    ) -> Agent:
        """创建并注册一个子 Agent

        优先尝试从 youmi/agents/<role>/config.yaml 加载配置，
        找不到则使用参数构造默认配置。

        Args:
            role: Agent 角色标识（如 coder / reviewer / researcher）
            name: Agent 实例名称，默认使用 role
            system_prompt: 系统提示词覆盖
            task: 分配给子 Agent 的任务描述（仅记录，不自动执行）
            allowed_tools: 允许使用的工具列表
            env: 运行环境路径，默认继承 MasterAgent 的 env
            config_overrides: 其他配置覆盖项

        Returns:
            新创建的 Agent 实例（尚未 initialize）
        """
        agent_name = name or role

        # 尝试从配置目录加载
        try:
            data = load_agent_config(role)
            llm_data = data.pop("llm_config", {})
            memory_data = data.pop("memory_config", {})
            metadata_data = data.pop("metadata", {})

            # 移除 YAML 中的 name（使用参数传入的 agent_name）
            data.pop("name", None)

            # 参数覆盖
            if system_prompt:
                data["system_prompt"] = system_prompt
            if allowed_tools is not None:
                data["allowed_tools"] = allowed_tools

            if config_overrides:
                data.update(config_overrides)

            sub_config = AgentConfig(
                name=agent_name,
                llm_config=LLMConfig(**llm_data),
                memory_config=MemoryConfig(**memory_data),
                metadata=AgentMetadata(**metadata_data),
                env=env or self._env,
                **data,
            )
        except FileNotFoundError:
            # 没有配置文件，使用参数构造
            sub_config = AgentConfig(
                name=agent_name,
                system_prompt=system_prompt or f"你是一个 {role} 角色的 Agent。",
                llm_config=self._config.llm_config,
                memory_config=self._config.memory_config,
                allowed_tools=allowed_tools or [],
                env=env or self._env,
                metadata=AgentMetadata(
                    display_name=agent_name,
                    role=role,
                    description=f"由 MasterAgent 创建的 {role} Agent",
                ),
            )

        agent = Agent(sub_config)

        # 共享 LLM 客户端（如果 MasterAgent 有）
        if self._llm_client is not None:
            agent._llm_client = self._llm_client

        # 注册
        self._sub_agents[agent.agent_id] = SubAgentRecord(
            agent=agent,
            role=role,
            task=task,
        )

        logger.info(
            "MasterAgent created sub-agent: name=%s role=%s id=%s",
            agent.name, role, agent.agent_id,
        )
        return agent

    async def run_sub_agent(self, agent_id: str) -> TaskResult:
        """初始化并运行指定的子 Agent

        Args:
            agent_id: 子 Agent ID

        Returns:
            TaskResult

        Raises:
            KeyError: agent_id 不在子 Agent 注册表中
        """
        record = self._sub_agents.get(agent_id)
        if record is None:
            raise KeyError(f"子 Agent '{agent_id}' 未注册，请先调用 create_sub_agent()")

        agent = record.agent

        # 初始化（如果尚未初始化）
        if agent.status == AgentStatus.CREATED:
            await agent.initialize()

        # 执行任务
        result = await agent.run(task=record.task, task_id=agent_id)
        record.result = result

        logger.info(
            "Sub-agent '%s' finished: status=%s iterations=%d",
            agent.name, result.status.value, result.iterations,
        )
        return result

    async def run_all_sub_agents(self, parallel: bool = False) -> dict[str, TaskResult]:
        """运行所有已注册但未执行的子 Agent

        Args:
            parallel: 是否并行执行（默认串行）

        Returns:
            {agent_id: TaskResult} 映射
        """
        import asyncio

        pending = [
            rec for rec in self._sub_agents.values()
            if rec.result is None and rec.agent.status in (
                AgentStatus.CREATED, AgentStatus.IDLE,
            )
        ]

        results: dict[str, TaskResult] = {}

        if parallel:
            tasks = [self.run_sub_agent(rec.agent.agent_id) for rec in pending]
            task_results = await asyncio.gather(*tasks, return_exceptions=True)
            for rec, res in zip(pending, task_results):
                if isinstance(res, Exception):
                    logger.error("Sub-agent '%s' failed: %s", rec.agent.name, res)
                    results[rec.agent.agent_id] = TaskResult(
                        agent_id=rec.agent.agent_id,
                        status=AgentStatus.FAILED,
                        error=str(res),
                    )
                else:
                    results[rec.agent.agent_id] = res
        else:
            for rec in pending:
                result = await self.run_sub_agent(rec.agent.agent_id)
                results[rec.agent.agent_id] = result

        return results

    def get_sub_agent(self, agent_id: str) -> Agent | None:
        """获取子 Agent 实例"""
        record = self._sub_agents.get(agent_id)
        return record.agent if record else None

    def get_sub_agents(self) -> dict[str, SubAgentRecord]:
        """获取所有子 Agent 记录"""
        return dict(self._sub_agents)

    # -----------------------------------------------------------------------
    # 内置工具注册
    # -----------------------------------------------------------------------

    def _register_master_tools(self) -> None:
        """注册 MasterAgent 专用工具"""

        # 工具: create_sub_agent
        create_tool = ToolDefinition(
            name="create_sub_agent",
            description=(
                "创建一个新的子 Agent 来执行特定任务。"
                "指定角色（如 coder、reviewer、researcher）和任务描述。"
            ),
            parameters=[
                ToolParameter(
                    name="role",
                    type="string",
                    description="Agent 角色标识，如 coder/reviewer/researcher/writer",
                    required=True,
                ),
                ToolParameter(
                    name="task",
                    type="string",
                    description="分配给子 Agent 的具体任务描述",
                    required=True,
                ),
                ToolParameter(
                    name="system_prompt",
                    type="string",
                    description="自定义系统提示词（可选）",
                    required=False,
                    default="",
                ),
                ToolParameter(
                    name="allowed_tools",
                    type="array",
                    description="允许使用的工具名称列表（可选）",
                    required=False,
                ),
            ],
        )

        async def _create_sub_agent_handler(**kwargs: Any) -> str:
            role = kwargs.get("role", "general")
            task = kwargs.get("task", "")
            system_prompt = kwargs.get("system_prompt", "")
            allowed_tools = kwargs.get("allowed_tools", [])

            agent = self.create_sub_agent(
                role=role,
                task=task,
                system_prompt=system_prompt,
                allowed_tools=allowed_tools or None,
            )
            return json.dumps({
                "agent_id": agent.agent_id,
                "name": agent.name,
                "role": role,
                "task": task,
                "status": "created",
            }, ensure_ascii=False)

        self._tool_registry.register(create_tool, _create_sub_agent_handler)

        # 工具: run_sub_agent
        run_tool = ToolDefinition(
            name="run_sub_agent",
            description="运行指定的子 Agent，让其执行已分配的任务。",
            parameters=[
                ToolParameter(
                    name="agent_id",
                    type="string",
                    description="要运行的子 Agent ID",
                    required=True,
                ),
            ],
        )

        async def _run_sub_agent_handler(**kwargs: Any) -> str:
            agent_id = kwargs.get("agent_id", "")
            try:
                result = await self.run_sub_agent(agent_id)
                return json.dumps({
                    "agent_id": agent_id,
                    "status": result.status.value,
                    "output": result.output,
                    "iterations": result.iterations,
                    "error": result.error,
                }, ensure_ascii=False)
            except KeyError as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        self._tool_registry.register(run_tool, _run_sub_agent_handler)

        # 工具: list_sub_agents
        list_tool = ToolDefinition(
            name="list_sub_agents",
            description="列出所有已创建的子 Agent 及其状态。",
            parameters=[],
        )

        async def _list_sub_agents_handler(**kwargs: Any) -> str:
            agents_info = [rec.to_dict() for rec in self._sub_agents.values()]
            return json.dumps(agents_info, ensure_ascii=False)

        self._tool_registry.register(list_tool, _list_sub_agents_handler)

        # 工具: list_available_roles
        roles_tool = ToolDefinition(
            name="list_available_roles",
            description="列出所有已配置的 Agent 角色（在 youmi/agents/ 目录中有配置的）。",
            parameters=[],
        )

        async def _list_available_roles_handler(**kwargs: Any) -> str:
            roles = list_agents()
            return json.dumps({
                "available_roles": roles,
                "description": "这些角色在 youmi/agents/ 中有配置文件，可以直接创建",
            }, ensure_ascii=False)

        self._tool_registry.register(roles_tool, _list_available_roles_handler)

    # -----------------------------------------------------------------------
    # 生命周期钩子
    # -----------------------------------------------------------------------

    async def on_initialize(self) -> None:
        """初始化钩子：创建 LLM 客户端"""
        # 创建 LLM 客户端（如果配置了 api_key）
        llm_cfg = self._config.llm_config
        if llm_cfg.api_key or llm_cfg.base_url:
            self._llm_client = LLMClient(llm_cfg)
            logger.info("MasterAgent LLM client created: model=%s", llm_cfg.model)

    async def on_start(self, task: str) -> None:
        """任务开始钩子"""
        logger.info("MasterAgent starting task: %s", task[:100])

    async def on_stop(self, error: str | None) -> None:
        """任务结束钩子：汇总子 Agent 状态"""
        if self._sub_agents:
            summary = {
                aid: rec.agent.status.value
                for aid, rec in self._sub_agents.items()
            }
            logger.info("MasterAgent stopped. Sub-agents status: %s", summary)

    async def on_destroy(self) -> None:
        """销毁钩子：销毁所有子 Agent"""
        for record in self._sub_agents.values():
            if record.agent.is_alive:
                await record.agent.destroy()
        self._sub_agents.clear()
        logger.info("MasterAgent destroyed all sub-agents.")

    # -----------------------------------------------------------------------
    # 序列化
    # -----------------------------------------------------------------------

    def to_summary(self) -> dict[str, Any]:
        """MasterAgent 状态摘要（含子 Agent 信息）"""
        summary = super().to_summary()
        summary["sub_agents"] = {
            aid: rec.to_dict() for aid, rec in self._sub_agents.items()
        }
        summary["sub_agent_count"] = len(self._sub_agents)
        return summary
