"""
Eval 执行器 (eval.runner)

用**真实生产链路**执行评测任务：
``LLMClient``（htpx → HTTP）→ ``MockLLMServer``（脚本化 OpenAI 兼容响应）
→ 真实 ``Agent`` ReAct 循环（Agent 子类，跳过任务自检）→ ``ToolRegistry`` 执行。

设计要点：
- 每个评测任务一个独立 ``AuditLogger``，从中汇总 token / 调用次数（与生产口径一致）
- mock 脚本按 ``EvalTask.script`` 逐轮返回；脚本耗尽后由 mock server 兜底，
  ``script_exhausted`` 标记模型行为超出脚本预期的任务
- ReAct 循环使用的 Agent 是真实基类子类（``EvalAgent``），仅覆写任务自检
  （避免自检产生的额外 LLM 调用干扰脚本化序列）
- 评测开始前重置进程级熔断注册表，保证任务间隔离
- ``MockLLMServer`` 延迟导入（aiohttp 为可选依赖）：数据集 / 评分 / 执行器
  构造不要求安装 aiohttp，仅实际启动 mock 服务器时要求
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from youmi.core.agent import Agent
from youmi.core.models import _TaskSelfCheck
from youmi.core.models import AgentConfig
from youmi.core.resilience import CircuitBreakerRegistry, get_breaker_registry
from youmi.core.tool import ToolRegistry
from youmi.core.types import LLMConfig, RetryPolicy
from youmi.eval.dataset import EvalDataset, EvalTask, EvalStep
from youmi.eval.tools import build_default_tools
from youmi.llm.client import LLMClient
from youmi.observability import AuditLogger

if TYPE_CHECKING:  # mock server 依赖可选 aiohttp，仅类型检查时导入
    from youmi.llm.mock_server import MockLLMServer, MockResponse

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "你是一个评测用助手。请根据任务要求调用合适的工具，"
    "并在获得工具结果后给出简洁的最终回复。"
)


class EvalAgent(Agent):
    """评测专用 Agent — 跳过任务自检。

    真实 ``Agent.run()`` 在 ReAct 循环前会调用 ``_self_check_task()``，
    该自检在已注册工具且 LLM 可用时会额外发起一次 LLM 调用，干扰
    脚本化 mock 序列。评测场景直接判定工具充足，其余流程与生产一致。
    """

    async def _self_check_task(self, task: str) -> _TaskSelfCheck:
        return _TaskSelfCheck(is_sufficient=True)


@dataclass
class TaskRun:
    """单个任务的执行轨迹与原始观测数据"""

    task_id: str
    output: str = ""
    status: str = ""
    iterations: int = 0
    # 工具轨迹（按调用顺序）
    actual_tools: list[str] = field(default_factory=list)
    tool_arguments: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[str] = field(default_factory=list)
    # 成本观测（来自审计日志，与生产口径一致）
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # 其他
    duration_ms: float = 0.0
    script_exhausted: bool = False
    error: str = ""


class EvalRunner:
    """评测执行器。

    Args:
        dataset: 评测数据集
        model: 传给 mock server / LLMConfig 的模型名（仅标识用）
        tools: 评测工具注册表（None = ``build_default_tools()``）
        system_prompt: 发送给 Agent 的系统提示词
        max_iterations: ReAct 最大迭代次数默认值（任务可覆盖，再默认用数据集值）
    """

    def __init__(
        self,
        dataset: EvalDataset,
        *,
        model: str = "mock-model",
        tools: ToolRegistry | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_iterations: int | None = None,
    ) -> None:
        self._dataset = dataset
        self._model = model
        self._tools = tools if tools is not None else build_default_tools()
        self._system_prompt = system_prompt
        self._max_iterations = max_iterations or dataset.max_iterations
        self._server: MockLLMServer | None = None

    # ------------------------------------------------------------------
    # 单任务执行
    # ------------------------------------------------------------------

    async def run_task(self, task: EvalTask) -> TaskRun:
        """执行单个评测任务（需先 ``start()`` 或经 ``run()`` 调用）"""
        if self._server is None:
            raise RuntimeError("EvalRunner 未启动：请通过 run() 调用，或先 await start()")

        # 每个任务：重置 mock 脚本，防止上一任务剩余脚本泄漏
        self._server.reset()
        self._server.script([_step_to_response(s) for s in task.script])

        audit = AuditLogger()
        llm_client = LLMClient(
            LLMConfig(
                model=self._model,
                base_url=self._server.base_url,
                api_key="sk-eval",
            ),
            retry_policy=RetryPolicy(max_retries=0, base_delay_s=0.0),
            breaker_registry=CircuitBreakerRegistry(),
            audit=audit,
        )

        max_iterations = task.max_iterations or self._max_iterations
        agent = EvalAgent(AgentConfig(
            name=f"eval-{task.task_id}",
            system_prompt=self._system_prompt,
            llm_config=LLMConfig(model=self._model, base_url=self._server.base_url),
            max_iterations=max_iterations,
        ))
        agent._llm_client = llm_client
        agent._tool_registry = self._tools

        run = TaskRun(task_id=task.task_id)
        start = time.monotonic()
        try:
            await agent.initialize()
            result = await agent.run(task.task, task_id=task.task_id)
            run.status = result.status.value
            run.output = str(result.output or "")
            run.iterations = result.iterations
            if result.error:
                run.error = result.error
        except Exception as exc:
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Eval task '%s' crashed", task.task_id)
        finally:
            run.duration_ms = (time.monotonic() - start) * 1000.0
            await llm_client.close()

        # 轨迹解析（真实 Agent conversation）
        _extract_trace(agent._conversation, run)

        # 成本汇总（审计日志 — 与生产口径一致）
        events = audit.get_recent(event_type="llm_call")
        run.llm_calls = len(events)
        for ev in events:
            tokens = ev.detail.get("tokens") or {}
            run.prompt_tokens += int(tokens.get("prompt_tokens") or 0)
            run.completion_tokens += int(tokens.get("completion_tokens") or 0)
            run.total_tokens += int(tokens.get("total_tokens") or 0)
        run.script_exhausted = run.llm_calls > len(task.script)

        return run

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> "EvalRunner":
        """启动 mock LLM 服务器并重置熔断状态"""
        from youmi.llm.mock_server import MockLLMServer  # 延迟导入（aiohttp 可选依赖）

        get_breaker_registry().reset_all()
        self._server = MockLLMServer(model=self._model)
        await self._server.start()
        return self

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def __aenter__(self) -> "EvalRunner":
        return await self.start()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # 全量执行
    # ------------------------------------------------------------------

    async def run(self, task_ids: list[str] | None = None) -> list[TaskRun]:
        """按顺序执行数据集任务，返回每个任务的执行轨迹"""
        dataset = self._dataset.select(task_ids)
        runs: list[TaskRun] = []
        await self.start()
        try:
            for task in dataset.tasks:
                runs.append(await self.run_task(task))
        finally:
            await self.stop()
        return runs


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _step_to_response(step: EvalStep) -> MockResponse:
    """把数据集脚本步骤转换为 mock server 响应定义"""
    from youmi.llm.mock_server import MockResponse  # 延迟导入（aiohttp 可选依赖）

    if step.is_tool:
        return MockResponse.tool_call(step.tool, step.args)
    return MockResponse.text(step.final)


def _extract_trace(conversation: list[dict[str, Any]], run: TaskRun) -> None:
    """从 Agent conversation 提取工具调用轨迹。

    轨迹来源为真实 ReAct 循环写入的消息：
    - ``assistant`` 消息的 ``tool_calls`` → 调用（名称 + 参数）
    - ``tool`` 消息 → 执行结果（按发生后顺序配对）
    """
    pending = 0  # 已记录但未配对结果的 tool_call 数
    for msg in conversation:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {"_raw": raw_args}
                run.actual_tools.append(name)
                run.tool_arguments.append(args if isinstance(args, dict) else {"_raw": args})
                pending += 1
        elif role == "tool":
            run.tool_results.append(str(msg.get("content", "")))
            if pending > 0:
                pending -= 1


__all__ = ["EvalAgent", "EvalRunner", "TaskRun", "DEFAULT_SYSTEM_PROMPT"]
