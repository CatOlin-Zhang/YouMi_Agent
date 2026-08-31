"""
WorkflowPlanner 测试

覆盖：
- 简单任务生成单步 Plan
- 复杂任务生成多步 DAG（依赖顺序正确）
- LLM 输出不合法时自动重试
- PlanMemory 命中时走 fast path
- Planner 无 LLM 客户端时抛出明确错误
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from youmi.coordinator.plan import WorkflowPlan, WorkflowStep
from youmi.coordinator.planner import WorkflowPlanner


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------

def _make_master(llm_response: str | None = None) -> MagicMock:
    """构建 mock MasterAgent，可配置 LLM 客户端返回值"""
    master = MagicMock()
    if llm_response is not None:
        llm_client = MagicMock()
        llm_client.complete = AsyncMock(return_value=llm_response)
        master._llm_client = llm_client
    else:
        master._llm_client = None
    return master


def _simple_plan_json() -> str:
    """单步 Plan JSON"""
    return json.dumps({
        "name": "简单排序任务",
        "description": "实现 Python 排序算法",
        "steps": [
            {
                "step_id": "step1",
                "role": "coder",
                "task": "用 Python 实现快速排序算法，并附上测试用例",
                "depends_on": [],
                "allowed_tools": [],
                "timeout_seconds": 0,
            }
        ],
    })


def _complex_plan_json() -> str:
    """多步 DAG Plan JSON（调研 → 分析 → 撰写）"""
    return json.dumps({
        "name": "HBM行业分析报告",
        "description": "调研并生成分析报告",
        "steps": [
            {
                "step_id": "research",
                "role": "researcher",
                "task": "调研 HBM 行业现状、主要厂商和技术趋势",
                "depends_on": [],
                "allowed_tools": ["web_search"],
                "timeout_seconds": 0,
            },
            {
                "step_id": "analyze",
                "role": "analyst",
                "task": "基于调研结果分析竞争格局和投资价值",
                "depends_on": ["research"],
                "allowed_tools": [],
                "timeout_seconds": 0,
            },
            {
                "step_id": "report",
                "role": "writer",
                "task": "撰写完整的行业分析报告",
                "depends_on": ["analyze"],
                "allowed_tools": [],
                "timeout_seconds": 0,
            },
        ],
    })


# ---------------------------------------------------------------------------
# 测试：简单任务生成单步 Plan
# ---------------------------------------------------------------------------

async def test_generate_plan_simple():
    """简单任务 → 生成单步 Plan，validate() 无错误"""
    master = _make_master(llm_response=_simple_plan_json())
    planner = WorkflowPlanner(master, plan_memory=None, max_retries=1)

    plan = await planner.generate_plan("写一个 Python 排序算法")

    assert isinstance(plan, WorkflowPlan)
    assert len(plan.steps) == 1
    assert plan.steps[0].step_id == "step1"
    assert plan.steps[0].role == "coder"
    assert not plan.validate()  # 无验证错误
    assert plan.metadata.get("source") == "llm"
    assert "写一个 Python 排序算法" in plan.metadata.get("task", "")


# ---------------------------------------------------------------------------
# 测试：复杂任务生成多步 DAG
# ---------------------------------------------------------------------------

async def test_generate_plan_complex():
    """复杂任务 → 多步 DAG，依赖顺序正确"""
    master = _make_master(llm_response=_complex_plan_json())
    planner = WorkflowPlanner(master, plan_memory=None, max_retries=1)

    plan = await planner.generate_plan("调研 HBM 行业并生成分析报告")

    assert len(plan.steps) == 3
    step_ids = {s.step_id for s in plan.steps}
    assert step_ids == {"research", "analyze", "report"}

    # 依赖关系正确
    analyze_step = next(s for s in plan.steps if s.step_id == "analyze")
    assert "research" in analyze_step.depends_on

    report_step = next(s for s in plan.steps if s.step_id == "report")
    assert "analyze" in report_step.depends_on

    # 拓扑顺序：第一层无依赖，最后层在末尾
    layers = plan.get_execution_order()
    assert layers[0] == ["research"]
    assert ["analyze"] in layers
    assert ["report"] in layers

    assert not plan.validate()


# ---------------------------------------------------------------------------
# 测试：LLM 输出不合法时自动重试，最终成功
# ---------------------------------------------------------------------------

async def test_generate_plan_llm_retry_then_success():
    """第一次返回无效 JSON，第二次成功 → 最多 max_retries 次重试"""
    invalid_response = "这不是 JSON 格式的输出，我只是在测试。"
    valid_response = _simple_plan_json()

    # 使用有 LLM 客户端的 master，然后替换 complete 的 side_effect
    master = _make_master(llm_response=invalid_response)  # 先用随便一个值初始化 llm_client
    master._llm_client.complete = AsyncMock(
        side_effect=[invalid_response, valid_response]
    )

    planner = WorkflowPlanner(master, plan_memory=None, max_retries=2)
    plan = await planner.generate_plan("写一个排序算法")

    assert isinstance(plan, WorkflowPlan)
    assert len(plan.steps) == 1
    # complete 被调用了两次（第一次失败，第二次成功）
    assert master._llm_client.complete.call_count == 2


# ---------------------------------------------------------------------------
# 测试：LLM 反复失败，最终抛出 ValueError
# ---------------------------------------------------------------------------

async def test_generate_plan_llm_all_fail():
    """LLM 始终返回无效输出 → 超出重试次数后抛出 ValueError"""
    master = _make_master(llm_response="不是JSON")
    planner = WorkflowPlanner(master, plan_memory=None, max_retries=1)

    with pytest.raises(ValueError, match="WorkflowPlanner"):
        await planner.generate_plan("任务")

    # max_retries=1 → 最多尝试 2 次
    assert master._llm_client.complete.call_count == 2


# ---------------------------------------------------------------------------
# 测试：PlanMemory 命中时走 fast path
# ---------------------------------------------------------------------------

async def test_plan_memory_hit_fast_path():
    """PlanMemory 命中（similarity > threshold）→ 走 fast path，复用骨架"""
    # 构建一个 template plan
    template = WorkflowPlan(
        name="调研模板",
        steps=[
            WorkflowStep(
                step_id="research",
                role="researcher",
                task="调研原始任务",
                depends_on=[],
            ),
            WorkflowStep(
                step_id="report",
                role="writer",
                task="撰写原始报告",
                depends_on=["research"],
            ),
        ],
    )

    # mock plan_memory：search_plan 返回高相似度候选
    plan_memory = MagicMock()
    plan_memory.search_plan = AsyncMock(return_value=[(template, 0.92)])

    # master 有 LLM 客户端（供 _apply_plan_template 使用）
    # 返回一个适配后的 plan JSON
    adapted_json = json.dumps({
        "name": "调研模板",
        "description": "",
        "steps": [
            {
                "step_id": "research",
                "role": "researcher",
                "task": "调研新任务的具体内容",
                "depends_on": [],
                "allowed_tools": [],
                "timeout_seconds": 0,
            },
            {
                "step_id": "report",
                "role": "writer",
                "task": "撰写新任务的报告",
                "depends_on": ["research"],
                "allowed_tools": [],
                "timeout_seconds": 0,
            },
        ],
    })
    master = _make_master(llm_response=adapted_json)

    planner = WorkflowPlanner(master, plan_memory=plan_memory, max_retries=1)
    plan = await planner.generate_plan("新任务：调研量子计算市场")

    # 确认走了 fast path（source == "memory"）
    assert plan.metadata.get("source") == "memory"
    assert plan.metadata.get("template_similarity") == pytest.approx(0.92)
    # 骨架保留：角色和依赖不变
    roles = {s.role for s in plan.steps}
    assert "researcher" in roles
    assert "writer" in roles

    # PlanMemory 被查询了，LLM 被调用了一次（用于 _apply_plan_template）
    plan_memory.search_plan.assert_awaited_once()


# ---------------------------------------------------------------------------
# 测试：PlanMemory 未命中时走 LLM 全量生成
# ---------------------------------------------------------------------------

async def test_plan_memory_miss_falls_back_to_llm():
    """PlanMemory 未命中（空结果）→ 降级为 LLM 全量生成"""
    plan_memory = MagicMock()
    plan_memory.search_plan = AsyncMock(return_value=[])  # 未命中

    master = _make_master(llm_response=_simple_plan_json())
    planner = WorkflowPlanner(master, plan_memory=plan_memory, max_retries=1)

    plan = await planner.generate_plan("写一个排序算法")

    assert plan.metadata.get("source") == "llm"
    master._llm_client.complete.assert_awaited_once()


# ---------------------------------------------------------------------------
# 测试：无 LLM 客户端时抛出错误
# ---------------------------------------------------------------------------

async def test_no_llm_client_raises_error():
    """没有 LLM 客户端（master._llm_client=None）→ 抛出 ValueError"""
    master = _make_master(llm_response=None)  # 无 LLM 客户端
    planner = WorkflowPlanner(master, plan_memory=None, max_retries=0)

    with pytest.raises(ValueError, match="没有可用的 LLM 客户端"):
        await planner.generate_plan("任务")


# ---------------------------------------------------------------------------
# 测试：markdown 代码块中的 JSON 可以正确解析
# ---------------------------------------------------------------------------

async def test_parse_plan_json_with_markdown_wrapper():
    """LLM 返回 ```json ... ``` 包裹时也能正确解析"""
    plan_json = _simple_plan_json()
    wrapped = f"```json\n{plan_json}\n```"

    master = _make_master(llm_response=wrapped)
    planner = WorkflowPlanner(master, plan_memory=None, max_retries=0)

    plan = await planner.generate_plan("排序算法")
    assert isinstance(plan, WorkflowPlan)
    assert len(plan.steps) == 1
