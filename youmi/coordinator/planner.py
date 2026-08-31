"""
WorkflowPlanner — Plan-then-Execute 规划器

将 MasterAgent 的"LLM 自由调工具编排"升级为"LLM 先生成结构化 WorkflowPlan，
引擎再确定性执行"的两阶段模式。

流程::

    planner = WorkflowPlanner(master_agent, plan_memory=plan_memory)
    plan = await planner.generate_plan(user_task)
    # plan 是合法的 WorkflowPlan，可直接交给 WorkflowExecutor 执行

命中 PlanMemory 时走 fast path（复用骨架 + LLM 微调 task 文本），
未命中时调 LLM 全量生成，失败后最多重试 max_retries 次。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TYPE_CHECKING

from youmi.coordinator.plan import WorkflowPlan, WorkflowStep

if TYPE_CHECKING:
    from youmi.coordinator.plan_memory import PlanMemory

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM prompt 模板
# ---------------------------------------------------------------------------

_PLAN_SYSTEM_PROMPT = """\
你是一个工作流规划专家。根据用户任务，生成一个结构化的 WorkflowPlan JSON。

规则：
1. 简单任务（一个角色即可完成）→ 单步 Plan（steps 只有一个元素）
2. 复杂任务（需要多角色协作）→ 多步 DAG，用 depends_on 表达依赖关系
3. role 可以是任意角色名（如 coder、researcher、analyst、writer），无需预配置
4. 每个 step 的 task 字段用简明中文描述该 Agent 的具体任务（不超过 200 字）
5. depends_on 列出前置步骤的 step_id（无依赖时为空列表 []）
6. allowed_tools 列出该步骤 Agent 需要的工具名（不确定时留空列表 []）
7. timeout_seconds 填 0 表示使用默认超时

输出严格 JSON（不包含任何解释或 markdown 标记），格式：
{
  "name": "工作流名称",
  "description": "工作流描述",
  "steps": [
    {
      "step_id": "唯一标识（英文，如 step1 或语义化名称）",
      "role": "角色名",
      "task": "任务描述",
      "depends_on": [],
      "allowed_tools": [],
      "timeout_seconds": 0
    }
  ]
}
"""

_PLAN_USER_TEMPLATE = "请为以下任务生成 WorkflowPlan：\n\n{task}"

_TEMPLATE_ADAPT_SYSTEM = """\
你是一个工作流适配专家。给定一个已有的 WorkflowPlan 骨架和一个新任务，
请将骨架中每个步骤的 task 字段替换为适合新任务的描述，
保持角色（role）、依赖关系（depends_on）、工具列表不变。

输出严格 JSON，格式与输入骨架相同（只修改 steps[*].task 字段，其他字段保持原值）。
"""

_TEMPLATE_ADAPT_USER = """\
新任务：{task}

已有骨架：
{plan_json}

请输出适配后的 WorkflowPlan JSON（仅修改各步骤的 task 字段）：
"""


# ---------------------------------------------------------------------------
# WorkflowPlanner
# ---------------------------------------------------------------------------

class WorkflowPlanner:
    """工作流规划器 — 生成 WorkflowPlan

    Args:
        master_agent: MasterAgent 实例（用于访问 LLM 客户端和可用角色列表）
        plan_memory: PlanMemory 实例（可选），有则先语义检索相似任务复用
        max_retries: LLM 生成失败后最大重试次数（默认 2）
    """

    def __init__(
        self,
        master_agent: Any,
        plan_memory: PlanMemory | None = None,
        max_retries: int = 2,
    ) -> None:
        self._master = master_agent
        self._plan_memory = plan_memory
        self._max_retries = max_retries

    # -----------------------------------------------------------------------
    # 主入口
    # -----------------------------------------------------------------------

    async def generate_plan(self, user_task: str) -> WorkflowPlan:
        """生成 WorkflowPlan

        流程：
        1. 若有 plan_memory，先语义检索相似任务 → 命中则走 fast path（复用骨架）
        2. 未命中则调 LLM 全量生成，最多重试 max_retries 次
        3. 返回前在 metadata 写入 source / task 字段

        Args:
            user_task: 用户任务描述

        Returns:
            WorkflowPlan（已通过 validate()）

        Raises:
            ValueError: LLM 反复重试仍无法生成合法 Plan
        """
        # fast path：PlanMemory 命中
        if self._plan_memory is not None:
            candidates = await self._plan_memory.search_plan(user_task, top_k=1)
            if candidates:
                template_plan, similarity = candidates[0]
                logger.info(
                    "WorkflowPlanner: PlanMemory hit (similarity=%.3f), applying template",
                    similarity,
                )
                try:
                    plan = await self._apply_plan_template(template_plan, user_task)
                    errors = plan.validate()
                    if not errors:
                        plan = plan.model_copy(update={
                            "metadata": {
                                **plan.metadata,
                                "source": "memory",
                                "task": user_task,
                                "template_similarity": similarity,
                            }
                        })
                        logger.info(
                            "WorkflowPlanner: fast path plan ready, steps=%d",
                            len(plan.steps),
                        )
                        return plan
                    logger.warning(
                        "WorkflowPlanner: template-adapted plan validation failed: %s; "
                        "falling back to LLM full generation",
                        errors,
                    )
                except Exception as exc:
                    logger.warning(
                        "WorkflowPlanner: template adaptation failed (%s); "
                        "falling back to LLM full generation",
                        exc,
                    )

        # 正常路径：LLM 全量生成
        plan = await self._plan_from_llm(user_task)
        plan = plan.model_copy(update={
            "metadata": {
                **plan.metadata,
                "source": "llm",
                "task": user_task,
            }
        })
        logger.info(
            "WorkflowPlanner: LLM plan ready, steps=%d",
            len(plan.steps),
        )
        return plan

    # -----------------------------------------------------------------------
    # LLM 全量生成
    # -----------------------------------------------------------------------

    async def _plan_from_llm(self, user_task: str) -> WorkflowPlan:
        """调 LLM 生成 Plan，失败后最多重试 max_retries 次"""
        llm_client = getattr(self._master, '_llm_client', None)
        if llm_client is None:
            raise ValueError("WorkflowPlanner: MasterAgent 没有可用的 LLM 客户端，无法生成 Plan")

        last_error: str = ""
        for attempt in range(self._max_retries + 1):
            try:
                messages = [
                    {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
                    {"role": "user", "content": _PLAN_USER_TEMPLATE.format(task=user_task)},
                ]
                if last_error and attempt > 0:
                    # 上一次失败原因反馈给 LLM，引导修正
                    messages.append({
                        "role": "user",
                        "content": f"上一次生成的 JSON 验证失败：{last_error}\n请修正后重新输出合法的 WorkflowPlan JSON。",
                    })

                response = await llm_client.complete(messages)
                plan = self._parse_plan_json(response)
                errors = plan.validate()
                if not errors:
                    return plan
                last_error = "; ".join(errors)
                logger.warning(
                    "WorkflowPlanner: plan validation failed (attempt %d/%d): %s",
                    attempt + 1, self._max_retries + 1, last_error,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "WorkflowPlanner: LLM call failed (attempt %d/%d): %s",
                    attempt + 1, self._max_retries + 1, exc,
                )

        raise ValueError(
            f"WorkflowPlanner: 经过 {self._max_retries + 1} 次尝试仍无法生成合法的 WorkflowPlan。"
            f"最后错误：{last_error}"
        )

    # -----------------------------------------------------------------------
    # 模板复用（fast path）
    # -----------------------------------------------------------------------

    async def _apply_plan_template(
        self,
        template: WorkflowPlan,
        user_task: str,
    ) -> WorkflowPlan:
        """将模板 Plan 的 task 字段替换为适合新任务的描述

        若没有 LLM 客户端，直接用 user_task 填充第一个步骤的 task，
        其余步骤保持原 task（降级策略）。

        Args:
            template: 历史 Plan 骨架
            user_task: 新任务描述

        Returns:
            适配后的 WorkflowPlan
        """
        llm_client = getattr(self._master, '_llm_client', None)

        if llm_client is None:
            # 降级：直接替换第一个步骤 task，保留其余结构
            new_steps = []
            for i, step in enumerate(template.steps):
                if i == 0:
                    new_steps.append(step.model_copy(update={"task": user_task[:500]}))
                else:
                    new_steps.append(step)
            return template.model_copy(update={"steps": new_steps})

        # 使用 LLM 微调各步骤 task
        plan_json = template.model_dump_json(indent=2)
        messages = [
            {"role": "system", "content": _TEMPLATE_ADAPT_SYSTEM},
            {"role": "user", "content": _TEMPLATE_ADAPT_USER.format(
                task=user_task,
                plan_json=plan_json,
            )},
        ]
        response = await llm_client.complete(messages)
        return self._parse_plan_json(response)

    # -----------------------------------------------------------------------
    # JSON 解析
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_plan_json(text: str) -> WorkflowPlan:
        """从 LLM 响应中提取并解析 WorkflowPlan JSON

        支持 LLM 输出中包含 markdown 代码块的情况。

        Args:
            text: LLM 输出文本

        Returns:
            WorkflowPlan 实例

        Raises:
            ValueError: JSON 解析或 schema 不匹配
        """
        # 去除可能的 markdown 代码块包裹
        cleaned = text.strip()
        # 匹配 ```json ... ``` 或 ``` ... ```
        md_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
        if md_match:
            cleaned = md_match.group(1).strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM 输出不是合法 JSON: {exc}\n原始输出: {text[:500]}") from exc

        try:
            return WorkflowPlan(**data)
        except Exception as exc:
            raise ValueError(f"WorkflowPlan schema 不匹配: {exc}") from exc


__all__ = ["WorkflowPlanner"]
