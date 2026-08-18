"""
工作流计划与执行器 (WorkflowPlan + WorkflowExecutor)

将 MasterAgent 内联的任务编排逻辑提取为独立模型:
- WorkflowStep: 单个执行步骤 (Agent 角色、任务描述、依赖关系)
- WorkflowPlan: 完整的工作流计划 (步骤列表、执行模式、全局配置)
- WorkflowExecutor: 按计划实例化 Agent、分配任务、收集结果

执行模式:
- serial: 按依赖顺序串行执行
- parallel: 无依赖的步骤并行扇出
- mixed: 混合模式 (DAG 拓扑排序后按层级并行)

用法::

    from youmi.coordinator.plan import WorkflowPlan, WorkflowStep, WorkflowExecutor

    plan = WorkflowPlan(
        name="代码审查工作流",
        steps=[
            WorkflowStep(step_id="analyze", role="researcher", task="分析需求..."),
            WorkflowStep(step_id="code", role="coder", task="编写代码...", depends_on=["analyze"]),
            WorkflowStep(step_id="review", role="reviewer", task="审查代码...", depends_on=["code"]),
        ],
    )

    executor = WorkflowExecutor(master_agent=master, plan=plan)
    results = await executor.execute()
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from youmi.core.agent import Agent, AgentConfig, AgentStatus, TaskResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 步骤状态
# ---------------------------------------------------------------------------

class StepStatus(str, Enum):
    """工作流步骤状态"""

    PENDING = "pending"       # 等待执行
    RUNNING = "running"       # 执行中
    COMPLETED = "completed"   # 成功完成
    FAILED = "failed"         # 执行失败
    SKIPPED = "skipped"       # 跳过 (依赖失败)


# ---------------------------------------------------------------------------
# 步骤定义
# ---------------------------------------------------------------------------

class WorkflowStep(BaseModel):
    """工作流步骤定义

    Args:
        step_id: 步骤唯一标识 (用于依赖引用)
        role: Agent 角色 (用于 create_sub_agent)
        task: 任务描述
        name: Agent 实例名称 (默认使用 role)
        system_prompt: 系统提示词覆盖
        allowed_tools: 允许使用的工具列表
        depends_on: 依赖的步骤 ID 列表 (这些步骤完成后才开始)
        env: 运行环境路径 (空字符串表示继承 MasterAgent)
        config_overrides: 额外配置覆盖
        max_iterations: 最大 ReAct 迭代次数
        timeout_seconds: 超时秒数 (0 表示不限制)
    """

    step_id: str = Field(description="步骤唯一标识")
    role: str = Field(description="Agent 角色")
    task: str = Field(description="任务描述")
    name: str = Field(default="", description="Agent 名称，默认使用 role")
    system_prompt: str = Field(default="")
    allowed_tools: list[str] | None = None
    depends_on: list[str] = Field(default_factory=list)
    env: str = ""
    config_overrides: dict[str, Any] = Field(default_factory=dict)
    max_iterations: int = 20
    timeout_seconds: float = 0

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# 步骤执行结果
# ---------------------------------------------------------------------------

class StepResult(BaseModel):
    """步骤执行结果"""

    step_id: str
    status: StepStatus = StepStatus.PENDING
    agent_id: str = ""
    task_result: TaskResult | None = None
    error: str | None = None
    started_at: float = 0
    finished_at: float = 0

    @property
    def success(self) -> bool:
        return self.status == StepStatus.COMPLETED

    @property
    def duration(self) -> float:
        if self.finished_at and self.started_at:
            return self.finished_at - self.started_at
        return 0


# ---------------------------------------------------------------------------
# 工作流计划
# ---------------------------------------------------------------------------

class WorkflowPlan(BaseModel):
    """工作流计划 — 定义完整的执行蓝图

    Args:
        name: 计划名称 (日志/展示用)
        steps: 步骤列表
        description: 计划描述
        metadata: 附加元数据

    用法::

        plan = WorkflowPlan(
            name="数据分析工作流",
            steps=[
                WorkflowStep(step_id="collect", role="researcher", task="收集数据"),
                WorkflowStep(step_id="analyze", role="analyst", task="分析数据", depends_on=["collect"]),
                WorkflowStep(step_id="report", role="writer", task="写报告", depends_on=["analyze"]),
            ],
        )

        # 验证依赖
        plan.validate()
    """

    name: str = Field(default="workflow", description="计划名称")
    steps: list[WorkflowStep] = Field(default_factory=list)
    description: str = Field(default="")
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}

    def validate(self) -> list[str]:
        """验证计划的有效性

        Returns:
            错误消息列表 (空列表表示有效)
        """
        errors: list[str] = []
        step_ids = {s.step_id for s in self.steps}

        # 检查重复 ID
        if len(step_ids) != len(self.steps):
            errors.append("存在重复的 step_id")

        # 检查依赖引用
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in step_ids:
                    errors.append(
                        f"步骤 '{step.step_id}' 依赖 '{dep}'，但该步骤不存在"
                    )
            if step.step_id in step.depends_on:
                errors.append(f"步骤 '{step.step_id}' 不能依赖自己")

        # 检查循环依赖
        if not errors:
            cycle = self._detect_cycle()
            if cycle:
                errors.append(f"检测到循环依赖: {' → '.join(cycle)}")

        return errors

    def _detect_cycle(self) -> list[str] | None:
        """检测循环依赖 (DFS)"""
        adj: dict[str, list[str]] = {s.step_id: list(s.depends_on) for s in self.steps}
        visited: set[str] = set()
        in_stack: set[str] = set()
        path: list[str] = []

        def dfs(node: str) -> list[str] | None:
            visited.add(node)
            in_stack.add(node)
            path.append(node)

            for dep in adj.get(node, []):
                if dep not in visited:
                    cycle = dfs(dep)
                    if cycle:
                        return cycle
                elif dep in in_stack:
                    idx = path.index(dep)
                    return path[idx:] + [dep]

            path.pop()
            in_stack.discard(node)
            return None

        for step in self.steps:
            if step.step_id not in visited:
                cycle = dfs(step.step_id)
                if cycle:
                    return cycle
        return None

    def get_execution_order(self) -> list[list[str]]:
        """计算拓扑执行顺序 (按层级分组)

        Returns:
            层级列表: [[无依赖的步骤], [依赖第一层的步骤], ...]
            同一层级的步骤可并行执行。
        """
        # 计算入度
        in_degree: dict[str, int] = {s.step_id: len(s.depends_on) for s in self.steps}
        adj: dict[str, list[str]] = defaultdict(list)
        for s in self.steps:
            for dep in s.depends_on:
                adj[dep].append(s.step_id)

        # BFS 分层
        layers: list[list[str]] = []
        queue = deque(sid for sid, deg in in_degree.items() if deg == 0)

        while queue:
            layer = list(queue)
            layers.append(layer)
            next_queue: deque[str] = deque()
            for sid in layer:
                for child in adj[sid]:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        next_queue.append(child)
            queue = next_queue

        return layers

    def get_step(self, step_id: str) -> WorkflowStep | None:
        """按 ID 获取步骤"""
        for s in self.steps:
            return s if s.step_id == step_id else None
        return None


# ---------------------------------------------------------------------------
# 工作流执行器
# ---------------------------------------------------------------------------

class WorkflowExecutor:
    """工作流执行器 — 按计划实例化 Agent 并执行

    根据 WorkflowPlan 的步骤定义，自动:
    1. 为每个步骤创建子 Agent (通过 MasterAgent.create_sub_agent)
    2. 按依赖关系决定执行顺序
    3. 无依赖的步骤并行执行，有依赖的等待前置完成
    4. 收集所有步骤结果

    Args:
        master_agent: MasterAgent 实例 (用于创建子 Agent)
        plan: 工作流计划
        parallel: 是否启用并行执行 (默认 True)
        fail_fast: 某步骤失败时是否跳过依赖它的后续步骤 (默认 True)
        on_step_start: 步骤开始回调 (可选)
        on_step_complete: 步骤完成回调 (可选)

    用法::

        executor = WorkflowExecutor(master, plan)
        results = await executor.execute()

        for step_id, result in results.items():
            print(f"{step_id}: {result.status.value}")
    """

    def __init__(
        self,
        master_agent: Any,  # MasterAgent (避免循环导入)
        plan: WorkflowPlan,
        parallel: bool = True,
        fail_fast: bool = True,
        on_step_start: Any = None,
        on_step_complete: Any = None,
    ) -> None:
        self._master = master_agent
        self._plan = plan
        self._parallel = parallel
        self._fail_fast = fail_fast
        self._on_step_start = on_step_start
        self._on_step_complete = on_step_complete

        # 执行状态
        self._results: dict[str, StepResult] = {}
        self._agents: dict[str, Agent] = {}

    @property
    def plan(self) -> WorkflowPlan:
        return self._plan

    @property
    def results(self) -> dict[str, StepResult]:
        return dict(self._results)

    async def execute(self) -> dict[str, StepResult]:
        """执行工作流计划

        Returns:
            {step_id: StepResult} 映射

        Raises:
            ValueError: 计划验证失败
        """
        # 验证计划
        errors = self._plan.validate()
        if errors:
            raise ValueError(f"计划验证失败: {'; '.join(errors)}")

        # 初始化结果
        for step in self._plan.steps:
            self._results[step.step_id] = StepResult(step_id=step.step_id)

        # 按层级执行
        layers = self._plan.get_execution_order()

        logger.info(
            "WorkflowExecutor starting: plan='%s' steps=%d layers=%d",
            self._plan.name, len(self._plan.steps), len(layers),
        )

        for layer_idx, layer in enumerate(layers):
            logger.debug(
                "Executing layer %d/%d: steps=%s",
                layer_idx + 1, len(layers), layer,
            )

            if self._parallel and len(layer) > 1:
                await self._execute_layer_parallel(layer)
            else:
                await self._execute_layer_serial(layer)

        # 汇总日志
        completed = sum(1 for r in self._results.values() if r.success)
        failed = sum(1 for r in self._results.values() if r.status == StepStatus.FAILED)
        skipped = sum(1 for r in self._results.values() if r.status == StepStatus.SKIPPED)

        logger.info(
            "WorkflowExecutor finished: plan='%s' completed=%d failed=%d skipped=%d",
            self._plan.name, completed, failed, skipped,
        )

        return dict(self._results)

    async def _execute_layer_serial(self, layer: list[str]) -> None:
        """串行执行一层中的步骤"""
        for step_id in layer:
            await self._execute_step(step_id)

    async def _execute_layer_parallel(self, layer: list[str]) -> None:
        """并行执行一层中的步骤"""
        tasks = [self._execute_step(sid) for sid in layer]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute_step(self, step_id: str) -> None:
        """执行单个步骤"""
        step = self._get_step(step_id)
        if step is None:
            return

        result = self._results[step_id]

        # 检查依赖是否全部完成
        for dep_id in step.depends_on:
            dep_result = self._results.get(dep_id)
            if dep_result is None:
                result.status = StepStatus.SKIPPED
                result.error = f"依赖步骤 '{dep_id}' 不存在"
                return
            if dep_result.status in (StepStatus.FAILED, StepStatus.SKIPPED):
                if self._fail_fast:
                    result.status = StepStatus.SKIPPED
                    result.error = f"依赖步骤 '{dep_id}' 失败: {dep_result.error}"
                    logger.warning(
                        "Step '%s' skipped: dependency '%s' failed",
                        step_id, dep_id,
                    )
                    return

        # 开始执行
        result.status = StepStatus.RUNNING
        import time
        result.started_at = time.time()

        if self._on_step_start:
            try:
                await self._on_step_start(step, result)
            except Exception:
                logger.exception("on_step_start callback error (non-critical)")

        try:
            # 创建子 Agent
            agent = self._master.create_sub_agent(
                role=step.role,
                name=step.name or step.role,
                system_prompt=step.system_prompt,
                task=step.task,
                allowed_tools=step.allowed_tools,
                env=step.env,
                config_overrides={
                    **step.config_overrides,
                    "max_iterations": step.max_iterations,
                } if step.config_overrides or step.max_iterations != 20 else step.config_overrides,
            )
            self._agents[step_id] = agent
            result.agent_id = agent.agent_id

            # 初始化并执行
            if agent.status == AgentStatus.CREATED:
                await agent.initialize()

            # 超时控制
            if step.timeout_seconds > 0:
                task_result = await asyncio.wait_for(
                    agent.run(task=step.task, task_id=step_id),
                    timeout=step.timeout_seconds,
                )
            else:
                task_result = await agent.run(task=step.task, task_id=step_id)

            result.task_result = task_result
            result.status = (
                StepStatus.COMPLETED if task_result.success
                else StepStatus.FAILED
            )
            result.error = task_result.error

        except asyncio.TimeoutError:
            result.status = StepStatus.FAILED
            result.error = f"步骤超时 ({step.timeout_seconds}s)"
            logger.error("Step '%s' timed out after %.0fs", step_id, step.timeout_seconds)

        except Exception as exc:
            result.status = StepStatus.FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Step '%s' failed: %s", step_id, exc)

        finally:
            import time
            result.finished_at = time.time()

            if self._on_step_complete:
                try:
                    await self._on_step_complete(step, result)
                except Exception:
                    logger.exception("on_step_complete callback error (non-critical)")

            logger.info(
                "Step '%s' finished: status=%s duration=%.1fs",
                step_id, result.status.value, result.duration,
            )

    def _get_step(self, step_id: str) -> WorkflowStep | None:
        """获取步骤定义"""
        for s in self._plan.steps:
            if s.step_id == step_id:
                return s
        return None

    def get_agent(self, step_id: str) -> Agent | None:
        """获取步骤对应的 Agent 实例"""
        return self._agents.get(step_id)

    def get_summary(self) -> dict[str, Any]:
        """执行结果摘要"""
        return {
            "plan_name": self._plan.name,
            "total_steps": len(self._plan.steps),
            "completed": sum(1 for r in self._results.values() if r.success),
            "failed": sum(1 for r in self._results.values() if r.status == StepStatus.FAILED),
            "skipped": sum(1 for r in self._results.values() if r.status == StepStatus.SKIPPED),
            "steps": {
                sid: {
                    "status": r.status.value,
                    "duration": r.duration,
                    "error": r.error,
                }
                for sid, r in self._results.items()
            },
        }
