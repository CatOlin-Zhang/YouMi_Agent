"""
MasterAgent — 主协调 Agent

继承自 Agent 基类，覆写 ReAct 阶段以实现：
1. 与用户进行初始对话，理解任务需求
2. 分析任务并决定需要哪些子 Agent
3. 实例化子 Agent 并分配任务
4. 收集子 Agent 结果并汇总反馈
5. (P1) 处理子 Agent 工具权限申请
6. (P1) 多轮任务循环 (conversation_loop)
7. (P1) 任务完成后后台流水线 (PostTaskPipeline)
8. (P1) 子 Agent 进程隔离 (SubProcessAgentRunner)

子 Agent 配置约定：
    每个 Agent 的配置存放在 youmi/agents/<agent_name>/ 目录下，
    包含 config.yaml 配置文件。MasterAgent 通过 load_agent_config()
    加载配置并实例化子 Agent。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from youmi.core.agent import Agent, AgentConfig, AgentStatus, TaskResult
from youmi.core.types import (
    AgentMessage,
    AgentMetadata,
    LLMConfig,
    MemoryConfig,
    MessageRole,
)
from youmi.agents import load_agent_config
from youmi.coordinator.tool_approval import ToolApprovalMixin
from youmi.llm.client import LLMClient
from youmi.mcp.approval import ApprovalManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 任务简报模板 — 注入到 SubAgent 的 system prompt 中
# ---------------------------------------------------------------------------

_TASK_BRIEF_TEMPLATE = """

---
## 任务简报
你的任务: {task}

### 自检提醒
在开始工作前，请先评估你当前可用的工具是否足以完成任务。
如果工具不足，请尽力使用已有工具完成，必要时可以通过消息总线申请扩展工具。
"""

# 闲聊关键词 — 不视为新任务信号
_CHAT_KEYWORDS = frozenset({
    "你好", "您好", "hello", "hi", "hey",
    "谢谢", "感谢", "thanks", "thank you",
    "再见", "拜拜", "bye", "goodbye",
    "好的", "ok", "okay", "行",
    "嗯", "嗯嗯", "是", "不是",
    "对", "不对", "没问题",
})


# ---------------------------------------------------------------------------
# 子 Agent 注册表 — 记录 MasterAgent 创建的所有子 Agent
# ---------------------------------------------------------------------------

class SubAgentRecord:
    """子 Agent 运行记录"""

    def __init__(
        self,
        agent: Agent,
        role: str,
        task: str,
        isolated: bool = False,
    ) -> None:
        self.agent = agent
        self.role = role
        self.task = task
        self.result: TaskResult | None = None
        self.isolated = isolated
        # 进程隔离句柄 (仅 isolated=True 时使用)
        self._subprocess_handle: Any = None  # SubProcessHandle | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent.agent_id,
            "name": self.agent.name,
            "role": self.role,
            "task": self.task,
            "status": self.agent.status.value,
            "result": self.result.model_dump() if self.result else None,
            "isolated": self.isolated,
        }


# ---------------------------------------------------------------------------
# MasterAgent
# ---------------------------------------------------------------------------

class MasterAgent(ToolApprovalMixin, Agent):
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
        global_memory: Any | None = None,
        plan_memory: Any | None = None,
    ) -> None:
        super().__init__(config, memory_strategy=memory_strategy, llm_call=llm_call)

        # 子 Agent 注册表
        self._sub_agents: dict[str, SubAgentRecord] = {}

        # P6: 全局记忆 (工具经验知识库，专供工具管理 Agent 使用)
        self._global_memory = global_memory

        # Plan-then-Execute: Plan 记忆复用层（可选）
        self._plan_memory = plan_memory

        # WorkflowPlanner 实例（懒创建，在 on_initialize 中初始化）
        self._planner: Any | None = None

        # P1: 工具申请待处理队列 {requester_agent_id: (tool_description, reason)}
        self._pending_tool_requests: dict[str, tuple[str, str]] = {}

        # P1: 工具申请监听任务
        self._tool_request_listener_task: asyncio.Task | None = None

        # P1: 后台流水线
        self._post_task_pipeline: Any = None  # PostTaskPipeline | None

        # structure.md §2: 三级审批模型（委托 ApprovalManager 统一管理 + 审计）
        self._approval_manager = ApprovalManager()
        self._auto_approve_list: set[str] = set()      # 自动审批工具清单（镜像，与 manager 同步）
        self._sensitive_tools: set[str] = set()          # 需人工审批的敏感工具（镜像，与 manager 同步）
        self._manual_review_queue: dict[str, dict[str, Any]] = {}  # 待人工审批队列

        # 注册 MasterAgent 内置工具
        self._register_master_tools()

    @property
    def global_memory(self) -> Any | None:
        """全局记忆实例 (P6, 可选) — 工具经验知识库"""
        return self._global_memory

    @property
    def plan_memory(self) -> Any | None:
        """Plan 记忆复用层（可选） — 相似任务 WorkflowPlan 复用"""
        return self._plan_memory

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
        isolated: bool = False,
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
            # 自动生成详细的系统提示词（基于 role + task）
            if not system_prompt:
                system_prompt = (
                    f"你是一个专业的 {role} Agent。\n"
                    f"你的具体任务：{task}\n\n"
                    f"要求：\n"
                    f"- 始终围绕上述任务进行回复和工作\n"
                    f"- 以 {role} 的专业视角分析和输出\n"
                    f"- 回复要有条理、具体、可执行\n"
                    f"- 用中文简洁回复"
                )

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

        # P1: 注入任务简报到 system prompt
        if task and not isolated:
            task_brief = _TASK_BRIEF_TEMPLATE.format(task=task[:500])
            sub_config = sub_config.model_copy(
                update={"system_prompt": sub_config.system_prompt + task_brief},
            )

        agent = Agent(sub_config)

        # 共享 LLM 客户端（如果 MasterAgent 有，且非隔离模式）
        if self._llm_client is not None and not isolated:
            agent._llm_client = self._llm_client

        # 注册
        record = SubAgentRecord(
            agent=agent,
            role=role,
            task=task,
            isolated=isolated,
        )
        self._sub_agents[agent.agent_id] = record

        logger.info(
            "MasterAgent created sub-agent: name=%s role=%s id=%s isolated=%s",
            agent.name, role, agent.agent_id, isolated,
        )
        return agent

    async def run_sub_agent(self, agent_id: str) -> TaskResult:
        """初始化并运行指定的子 Agent

        支持进程隔离模式：如果 SubAgentRecord.isolated=True，
        通过 SubProcessAgentRunner 在独立进程中执行。

        Args:
            agent_id: 子 Agent ID

        Returns:
            TaskResult

        Raises:
            KeyError: agent_id 不在子 Agent 注册表中
        """
        record = self._sub_agents.get(agent_id)
        if record is None:
            available = ", ".join(
                f"{aid[:8]}({rec.role})" for aid, rec in self._sub_agents.items()
            ) or "无"
            raise KeyError(
                f"子 Agent '{agent_id}' 未注册。"
                f"当前已创建的子 Agent：{available}。"
                f"请先用 create_sub_agent 创建子 Agent，再用返回的 agent_id 调用 run_sub_agent。"
            )

        # P1: 进程隔离模式
        if record.isolated:
            return await self._run_isolated_sub_agent(record)

        agent = record.agent

        # 初始化（如果尚未初始化）
        if agent.status == AgentStatus.CREATED:
            await agent.initialize()

        # 标记正在作为子 Agent 运行（抑制 _after_model 重复气泡）
        hook_bridge = getattr(self, '_gui_bridge', None)
        if hook_bridge and hasattr(hook_bridge, 'hook_bridge'):
            hook_bridge.hook_bridge._sub_agent_running.add(agent.agent_id)
        try:
            # 执行任务
            result = await agent.run(task=record.task, task_id=agent_id)
        finally:
            if hook_bridge and hasattr(hook_bridge, 'hook_bridge'):
                hook_bridge.hook_bridge._sub_agent_running.discard(agent.agent_id)

        record.result = result

        # 在群聊中广播子 Agent 的独立结果气泡
        bridge = getattr(self, '_gui_bridge', None)
        if bridge is not None and result.output:
            self._broadcast_sub_result(bridge, agent, result)

        logger.info(
            "Sub-agent '%s' finished: status=%s iterations=%d",
            agent.name, result.status.value, result.iterations,
        )
        return result

    def _broadcast_sub_result(self, bridge: Any, agent: Any, result: TaskResult) -> None:
        """将子 Agent 的执行结果作为该 Agent 的直接发言广播到群聊。"""
        from gui.engine.models import MessageRecord, new_id

        session_id = bridge.active_session_id
        if not session_id:
            return
        output = str(result.output or "")
        if not output.strip():
            return
        if len(output) > 4000:
            output = output[:4000] + "\n\n...(内容过长已截断)"
        card = bridge.card_for(agent.agent_id, agent.name)
        # 不再使用 #### ✅ 前缀，避免前端折叠成结果卡片；
        # agent 名称由前端 seg-agent-badge 展示，实现群聊中“谁发言谁显示”的效果。
        text = output
        msg_id = new_id("sub")
        rec = MessageRecord(
            msg_id=msg_id,
            session_id=session_id,
            agent_id=agent.agent_id,
            agent_name=card.name,
            role="assistant",
            kind="text",
            text=text,
        )
        bridge.open_message(rec)
        bridge.close_message(msg_id)

    async def _run_isolated_sub_agent(self, record: SubAgentRecord) -> TaskResult:
        """在独立子进程中运行子 Agent

        通过 SubProcessAgentRunner 启动子进程，发送任务，等待结果。
        需要 MasterAgent 已连接 BusServer（WebSocket 模式）。

        Args:
            record: SubAgentRecord

        Returns:
            TaskResult
        """
        from youmi.coordinator.subprocess_agent import SubProcessAgentRunner

        if self._bus is None:
            logger.error(
                "Cannot run isolated sub-agent '%s': MasterAgent not connected to bus",
                record.agent.name,
            )
            result = TaskResult(
                agent_id=record.agent.agent_id,
                status=AgentStatus.FAILED,
                error="进程隔离模式需要 MasterAgent 连接 BusServer",
            )
            record.result = result
            return result

        # 推断 WebSocket URL（从 bus 连接信息）
        ws_url = getattr(self._bus, '_ws_url', 'ws://localhost:8765')

        runner = SubProcessAgentRunner(ws_url=ws_url)

        try:
            sp_result = await runner.launch_and_run(
                config=record.agent.config,
                task=record.task,
                broker=self._bus,
                workflow_id=self._workflow_id,
                timeout=300.0,
            )

            result = TaskResult(
                agent_id=record.agent.agent_id,
                status=AgentStatus.COMPLETED if sp_result.success else AgentStatus.FAILED,
                output=sp_result.output,
                iterations=sp_result.iterations,
                error=sp_result.error,
            )
        except Exception as exc:
            logger.exception("Isolated sub-agent '%s' failed: %s", record.agent.name, exc)
            result = TaskResult(
                agent_id=record.agent.agent_id,
                status=AgentStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

        record.result = result
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
        """注册 MasterAgent 专用工具

        委托给 youmi.tools.coordinator_ops 层实现，
        工具代码统一存放在 tools/ 目录下。
        """
        from youmi.tools.coordinator_ops import register_coordinator_tools
        register_coordinator_tools(self)



    # -----------------------------------------------------------------------
    # P1: 新任务循环
    # -----------------------------------------------------------------------

    async def conversation_loop(
        self,
        max_turns: int = 0,
        exit_keywords: tuple[str, ...] = ("exit", "quit", "退出"),
    ) -> None:
        """多轮任务循环 — Plan-then-Execute 主流程

        检测到新任务时走 Plan-then-Execute 两阶段：
        1. WorkflowPlanner.generate_plan() 生成结构化 WorkflowPlan（LLM 规划阶段）
        2. WorkflowExecutor.execute() 确定性执行 Plan（引擎执行阶段）
        3. 执行完成后将 Plan 写入 PlanMemory（下次相似任务复用）
        4. 降级：若 Planner 不可用或生成失败，回退 chat_turn() 路径

        非新任务信号（闲聊/澄清）继续走 chat_turn() 原有路径。

        Args:
            max_turns: 最大轮数，0 表示不限制
            exit_keywords: 退出关键词元组
        """
        turns = 0
        task_count = 0

        logger.info("MasterAgent conversation loop started (Plan-then-Execute mode)")

        while True:
            # 检查退出条件
            if max_turns > 0 and turns >= max_turns:
                logger.info("Max turns (%d) reached, exiting loop", max_turns)
                break

            turns += 1

            user_message = await self._get_user_input()
            if user_message is None:
                logger.info("No user input, exiting conversation loop")
                break

            # 检查退出关键词
            if user_message.strip().lower() in exit_keywords:
                logger.info("Exit keyword detected, exiting conversation loop")
                break

            # 检查是否为新任务信号
            if self._is_new_task_signal(user_message):
                task_count += 1
                logger.info(
                    "New task detected (task #%d): %s",
                    task_count, user_message[:80],
                )

                # 重置上一轮任务状态
                if task_count > 1:
                    await self.reset_for_new_task()

                # Plan-then-Execute：有 Planner 时走双阶段，否则降级
                if self._planner is not None:
                    await self._run_plan_then_execute(user_message)
                else:
                    # 没有 LLM 客户端，降级为原有 chat_turn 路径
                    logger.debug("No planner available, falling back to chat_turn")
                    await self.chat_turn(user_message)
            else:
                # 闲聊/澄清 → 原有路径
                result = await self.chat_turn(user_message)
                logger.debug(
                    "Turn %d result: iterations=%d error=%s",
                    turns, result.get("iterations", 0), result.get("error"),
                )

        logger.info(
            "Conversation loop ended: %d turns, %d tasks",
            turns, task_count,
        )

    async def _run_plan_then_execute(self, user_task: str) -> None:
        """Plan-then-Execute 两阶段执行

        1. Planner 生成 WorkflowPlan（失败则降级 chat_turn）
        2. WorkflowExecutor 按 Plan 确定性执行
        3. 将执行结果写入 Agent 记忆，供后续对话上下文使用
        4. 成功 Plan 写入 PlanMemory

        Args:
            user_task: 用户任务文本
        """
        from youmi.coordinator.plan import WorkflowExecutor

        # 阶段1：生成 Plan
        try:
            plan = await self._planner.generate_plan(user_task)
            logger.info(
                "MasterAgent plan generated: name='%s' steps=%d source=%s",
                plan.name, len(plan.steps), plan.metadata.get("source", "unknown"),
            )
        except Exception as exc:
            logger.warning(
                "MasterAgent plan generation failed (%s), falling back to chat_turn",
                exc,
            )
            await self.chat_turn(user_task)
            return

        # 阶段2：执行 Plan
        executor = WorkflowExecutor(
            master_agent=self,
            plan=plan,
            parallel=True,
            fail_fast=True,
            on_step_start=self._on_plan_step_start,
            on_step_complete=self._on_plan_step_complete,
        )

        try:
            results = await executor.execute()
        except Exception as exc:
            logger.error("MasterAgent plan execution failed: %s", exc)
            results = executor.results

        summary = executor.get_summary()
        logger.info("MasterAgent plan execution summary: %s", summary)

        # 将执行摘要写入 Agent 记忆（让后续对话知晓任务结果）
        completed = summary.get("completed", 0)
        failed = summary.get("failed", 0)
        skipped = summary.get("skipped", 0)
        summary_text = (
            f"任务执行完成。计划：{plan.name}，"
            f"步骤共 {len(plan.steps)} 个，"
            f"完成 {completed}，失败 {failed}，跳过 {skipped}。"
        )
        # 收集各步骤输出供汇总
        step_outputs: list[str] = []
        for step_id, step_result in results.items():
            if step_result.task_result and step_result.task_result.output:
                output_preview = str(step_result.task_result.output)[:300]
                step_outputs.append(f"[{step_id}] {output_preview}")
        if step_outputs:
            summary_text += "\n\n各步骤输出摘要：\n" + "\n".join(step_outputs)

        if self._memory:
            await self._memory.on_message("assistant", summary_text)

        # 保存 Plan 到 PlanMemory（有至少一个步骤成功时标记 success）
        any_success = completed > 0
        if self._plan_memory is not None:
            try:
                await self._plan_memory.save_plan(user_task, plan, success=any_success)
                logger.info(
                    "MasterAgent: plan saved to PlanMemory (success=%s)", any_success
                )
            except Exception as exc:
                logger.warning("MasterAgent: failed to save plan to PlanMemory: %s", exc)

    async def _on_plan_step_start(self, step: Any, result: Any) -> None:
        """Plan 步骤开始回调（供 WorkflowExecutor 调用）"""
        logger.debug("Plan step starting: step_id=%s role=%s", step.step_id, step.role)

    async def _on_plan_step_complete(self, step: Any, result: Any) -> None:
        """Plan 步骤完成回调（供 WorkflowExecutor 调用）"""
        logger.debug(
            "Plan step completed: step_id=%s status=%s",
            step.step_id, result.status.value,
        )

    async def _get_user_input(self) -> str | None:
        """获取用户输入 — 子类可覆写

        默认返回 None（无输入）。GUI/CLI 层应覆写此方法，
        从用户界面获取输入。

        Returns:
            用户输入字符串，None 表示无输入
        """
        return None

    @staticmethod
    def _is_new_task_signal(message: str) -> bool:
        """判断消息是否为新任务信号

        简单规则：消息长度 > 10 且不匹配闲聊关键词。

        Args:
            message: 用户消息

        Returns:
            True 表示是新任务信号
        """
        if len(message.strip()) <= 10:
            return False
        msg_lower = message.strip().lower()
        return not any(kw in msg_lower for kw in _CHAT_KEYWORDS)

    async def reset_for_new_task(self) -> None:
        """重置状态以准备新一轮任务

        - 重置所有子 Agent 工具权限到初始状态 (structure.md §2 工作流级回收)
        - 销毁所有子 Agent
        - 清空子 Agent 注册表
        - 重置 MasterAgent 状态为 IDLE
        - 保留记忆系统（跨任务记忆持续）
        """
        logger.info("MasterAgent resetting for new task")

        # 停止工具申请监听
        if self._tool_request_listener_task:
            self._tool_request_listener_task.cancel()
            self._tool_request_listener_task = None

        # structure.md §2: 工作流级权限回收 — 重置所有子 Agent 工具权限
        for record in self._sub_agents.values():
            try:
                record.agent.reset_tool_permissions()
            except Exception as exc:
                logger.debug("Error resetting tool permissions for '%s': %s",
                             record.agent.name, exc)

        # 销毁所有子 Agent
        for record in self._sub_agents.values():
            if record.isolated and record._subprocess_handle:
                await record._subprocess_handle.terminate()
            elif record.agent.is_alive:
                try:
                    await record.agent.destroy()
                except Exception as exc:
                    logger.debug("Error destroying sub-agent: %s", exc)

        self._sub_agents.clear()
        self._pending_tool_requests.clear()
        self._manual_review_queue.clear()
        # 清理已完结的审批记录（保留审计日志）
        self._approval_manager.clear_resolved()

        # 重置状态为 IDLE
        if self._status != AgentStatus.DESTROYED:
            self._status = AgentStatus.IDLE

        logger.info("MasterAgent reset complete")

    # -----------------------------------------------------------------------
    # 生命周期钩子
    # -----------------------------------------------------------------------

    async def on_initialize(self) -> None:
        """初始化钩子：创建 LLM 客户端 + 懒创建 WorkflowPlanner"""
        # 创建 LLM 客户端（如果配置了 api_key）
        llm_cfg = self._config.llm_config
        if llm_cfg.api_key or llm_cfg.base_url:
            self._llm_client = LLMClient(llm_cfg)
            logger.info("MasterAgent LLM client created: model=%s", llm_cfg.model)

        # Plan-then-Execute：懒创建 WorkflowPlanner（需要 LLM 客户端）
        if self._llm_client is not None:
            from youmi.coordinator.planner import WorkflowPlanner
            self._planner = WorkflowPlanner(self, plan_memory=self._plan_memory)
            logger.info("MasterAgent WorkflowPlanner created")

    async def on_start(self, task: str) -> None:
        """任务开始钩子：启动工具申请监听"""
        logger.info("MasterAgent starting task: %s", task[:100])

        # P1: 启动工具申请监听
        await self._start_tool_request_listener()

    async def on_stop(self, error: str | None) -> None:
        """任务结束钩子：汇总子 Agent 状态 + 触发后台流水线"""
        if self._sub_agents:
            summary = {
                aid: rec.agent.status.value
                for aid, rec in self._sub_agents.items()
            }
            logger.info("MasterAgent stopped. Sub-agents status: %s", summary)

        # P1: 停止工具申请监听
        if self._tool_request_listener_task:
            self._tool_request_listener_task.cancel()
            self._tool_request_listener_task = None

        # P1: 触发后台流水线
        completed_results = {
            aid: rec.result
            for aid, rec in self._sub_agents.items()
            if rec.result is not None
        }
        if completed_results:
            try:
                from youmi.coordinator.post_task import PostTaskPipeline
                # P6: 传递全局记忆和 ToolStore（如可用）
                tool_store = None
                bridge = getattr(self, '_tool_bridge', None)
                vault = bridge.vault if bridge is not None else None
                if vault is not None:
                    tool_store = getattr(vault, 'store', None)
                pipeline = PostTaskPipeline(
                    tool_store=tool_store,
                    global_memory=self._global_memory,
                )
                self._post_task_pipeline = pipeline
                # 在后台运行，不阻塞主流程
                asyncio.create_task(pipeline.run(self, completed_results))
                logger.info("PostTaskPipeline triggered in background")
            except Exception as exc:
                logger.debug("Failed to trigger PostTaskPipeline: %s", exc)

    async def on_destroy(self) -> None:
        """销毁钩子：销毁所有子 Agent + 清理资源"""
        # 停止工具申请监听
        if self._tool_request_listener_task:
            self._tool_request_listener_task.cancel()
            self._tool_request_listener_task = None

        for record in self._sub_agents.values():
            if record.isolated and record._subprocess_handle:
                await record._subprocess_handle.terminate()
            elif record.agent.is_alive:
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
