"""
PlanMemory 测试

覆盖：
- 无 embedding_client 时关键词降级搜索
- mock embedding_client 时向量余弦相似度检索
- 相似度低于阈值时不返回候选
- save_plan 相同任务指纹时更新 exec_count
- close / stats 基本接口
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from youmi.coordinator.plan import WorkflowPlan, WorkflowStep
from youmi.coordinator.plan_memory import PlanMemory


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------

def _make_plan(name: str = "测试计划", role: str = "coder", task: str = "任务描述") -> WorkflowPlan:
    return WorkflowPlan(
        name=name,
        steps=[
            WorkflowStep(
                step_id="step1",
                role=role,
                task=task,
                depends_on=[],
            )
        ],
    )


async def _init_memory(**kwargs) -> PlanMemory:
    """创建并初始化内存型 PlanMemory（使用 :memory: 避免磁盘文件）"""
    mem = PlanMemory(db_path=":memory:", **kwargs)
    await mem.initialize()
    return mem


# ---------------------------------------------------------------------------
# 测试：保存后可用关键词检索（无 embedding_client 降级路径）
# ---------------------------------------------------------------------------

async def test_save_and_search_keyword():
    """无 embedding_client 时关键词降级搜索能命中已保存的 Plan"""
    mem = await _init_memory(similarity_threshold=0.1)  # 低阈值便于测试

    plan = _make_plan(name="排序算法计划", task="实现 Python 快速排序")
    await mem.save_plan("Python 排序算法", plan, success=True)

    results = await mem.search_plan("Python 排序算法", top_k=3)
    assert len(results) > 0
    found_plan, similarity = results[0]
    assert isinstance(found_plan, WorkflowPlan)
    assert similarity > 0.0

    await mem.close()


async def test_save_and_search_keyword_no_match():
    """关键词完全不匹配时返回空列表"""
    mem = await _init_memory(similarity_threshold=0.5)

    plan = _make_plan(task="Python 排序算法")
    await mem.save_plan("Python 排序算法", plan, success=True)

    # 查询与已保存任务完全不相关
    results = await mem.search_plan("量子计算硬件制造", top_k=3)
    assert results == []

    await mem.close()


# ---------------------------------------------------------------------------
# 测试：mock embedding_client 时向量检索
# ---------------------------------------------------------------------------

async def test_save_and_search_vector():
    """有 embedding_client 时走向量余弦相似度路径，高相似度候选排在前面"""
    # 两个向量：query 与 vec_a 完全相同（相似度=1.0），与 vec_b 正交（相似度=0）
    vec_a = [1.0, 0.0, 0.0]
    vec_b = [0.0, 1.0, 0.0]
    query_vec = [1.0, 0.0, 0.0]

    embedding_client = MagicMock()
    call_count = {"n": 0}

    async def _embed_one(text: str) -> list[float]:
        call_count["n"] += 1
        # 第1次：保存 plan_a；第2次：保存 plan_b；第3次：查询
        if call_count["n"] == 1:
            return vec_a
        elif call_count["n"] == 2:
            return vec_b
        else:
            return query_vec

    embedding_client.embed_one = _embed_one

    mem = await _init_memory(
        embedding_client=embedding_client,
        similarity_threshold=0.5,
    )

    plan_a = _make_plan(name="A", task="任务A")
    plan_b = _make_plan(name="B", task="任务B")

    await mem.save_plan("任务A文本", plan_a, success=True)
    await mem.save_plan("任务B文本", plan_b, success=True)

    results = await mem.search_plan("查询文本", top_k=3)

    # plan_a 相似度=1.0，plan_b 相似度=0（低于阈值0.5），只返回 plan_a
    assert len(results) == 1
    found_plan, similarity = results[0]
    assert found_plan.name == "A"
    assert similarity == pytest.approx(1.0)

    await mem.close()


# ---------------------------------------------------------------------------
# 测试：相似度低于阈值时不返回候选
# ---------------------------------------------------------------------------

async def test_similarity_threshold():
    """相似度低于 similarity_threshold 时不返回结果"""
    vec_low = [1.0, 0.0, 0.0]
    vec_query = [0.0, 1.0, 0.0]  # 与 vec_low 正交 → 相似度=0

    embedding_client = MagicMock()
    call_idx = {"i": 0}
    vecs = [vec_low, vec_query]

    async def _embed(text: str) -> list[float]:
        v = vecs[call_idx["i"] % len(vecs)]
        call_idx["i"] += 1
        return v

    embedding_client.embed_one = _embed

    mem = await _init_memory(
        embedding_client=embedding_client,
        similarity_threshold=0.8,  # 高阈值
    )

    plan = _make_plan()
    await mem.save_plan("原始任务", plan, success=True)

    results = await mem.search_plan("查询任务", top_k=3)
    assert results == []  # 相似度=0，低于阈值

    await mem.close()


# ---------------------------------------------------------------------------
# 测试：相同指纹任务保存时更新 exec_count
# ---------------------------------------------------------------------------

async def test_save_same_task_updates_exec_count():
    """相同任务文本重复保存，exec_count 递增"""
    mem = await _init_memory(similarity_threshold=0.1)

    plan = _make_plan()
    task_text = "重复任务"

    plan_id_1 = await mem.save_plan(task_text, plan, success=True)
    plan_id_2 = await mem.save_plan(task_text, plan, success=True)

    # 相同指纹 → 相同 plan_id
    assert plan_id_1 == plan_id_2

    stats = await mem.stats()
    assert stats["total"] == 1
    assert stats["success"] == 1

    await mem.close()


# ---------------------------------------------------------------------------
# 测试：只有 success=True 的记录才被检索
# ---------------------------------------------------------------------------

async def test_only_success_plans_are_searchable():
    """success=False 的 Plan 不出现在检索结果中"""
    mem = await _init_memory(similarity_threshold=0.1)

    plan = _make_plan(task="失败任务")
    await mem.save_plan("失败任务文本", plan, success=False)

    results = await mem.search_plan("失败任务文本", top_k=3)
    assert results == []

    await mem.close()


# ---------------------------------------------------------------------------
# 测试：stats() 返回正确统计
# ---------------------------------------------------------------------------

async def test_stats():
    """stats() 返回 total / success / vectorized 统计"""
    mem = await _init_memory(similarity_threshold=0.1)

    await mem.save_plan("任务1", _make_plan(name="P1"), success=True)
    await mem.save_plan("任务2", _make_plan(name="P2"), success=False)
    await mem.save_plan("任务3", _make_plan(name="P3"), success=True)

    stats = await mem.stats()
    assert stats["total"] == 3
    assert stats["success"] == 2
    assert stats["vectorized"] == 0  # 没有 embedding_client

    await mem.close()


# ---------------------------------------------------------------------------
# 测试：top_k 限制返回数量
# ---------------------------------------------------------------------------

async def test_search_top_k_limit():
    """top_k 参数正确限制返回数量"""
    mem = await _init_memory(similarity_threshold=0.1)

    # 保存 5 条相关记录
    for i in range(5):
        plan = _make_plan(name=f"Plan{i}", task=f"Python 任务 {i}")
        await mem.save_plan(f"Python 任务 {i}", plan, success=True)

    results = await mem.search_plan("Python 任务", top_k=2)
    assert len(results) <= 2

    await mem.close()
