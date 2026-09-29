"""
Eval 数据集模型 (eval.dataset)

定义评测任务与数据集结构，并提供内置数据集与文件加载/保存：

- ``EvalStep``    — 一步脚本化行为（调用工具 或 给出最终回复）
- ``EvalTask``    — 一个评测任务（任务描述 + 期望工具 + 脚本 + 判定条件）
- ``EvalDataset`` — 任务集合（支持 JSON / YAML 加载与保存）
- ``builtin_dataset()`` — 内置确定性数据集（配合 mock LLM 使用）

数据集文件格式（JSON 或 YAML）::

    name: my-eval
    version: "1.0"
    max_iterations: 6
    tasks:
      - task_id: weather_bj
        task: 帮我查一下北京的天气并告诉我气温
        expected_tools: [get_weather]
        expect_final_contains: "25℃"
        script:
          - tool: get_weather
            args: {city: 北京}
          - final: 北京今天晴，25℃。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class EvalStep(BaseModel):
    """脚本化的一步：``tool`` 与 ``final`` 二选一。

    - 调用工具: ``{"tool": "get_weather", "args": {"city": "北京"}}``
    - 最终回复: ``{"final": "北京今天晴，25℃。"}``
    """

    tool: str = Field(default="", description="要调用的工具名（空 = 本步为最终回复）")
    args: dict[str, Any] = Field(default_factory=dict, description="工具参数")
    final: str = Field(default="", description="最终回复文本")

    @property
    def is_tool(self) -> bool:
        return bool(self.tool)


class EvalTask(BaseModel):
    """一个评测任务。

    Args:
        task_id: 任务唯一标识
        task: 发送给 Agent 的任务描述
        expected_tools: 期望调用的工具名列表
        script: mock LLM 的逐步脚本（每轮响应一个 EvalStep）
        tool_order_strict: True 时要求工具调用顺序与 expected_tools 完全一致；
            否则按集合匹配（去重后比较）
        expect_final_contains: 最终回复必须包含的子串（空 = 不检查）
        max_iterations: 覆盖数据集级 ReAct 最大迭代次数（0 = 用数据集默认值）
        category: 任务分类标签（可选，报告用）
    """

    task_id: str
    task: str
    expected_tools: list[str] = Field(default_factory=list)
    script: list[EvalStep] = Field(default_factory=list)
    tool_order_strict: bool = False
    expect_final_contains: str = ""
    max_iterations: int = 0
    category: str = ""


class EvalDataset(BaseModel):
    """评测任务集合。

    Args:
        name: 数据集名称
        version: 版本号
        description: 数据集说明
        max_iterations: 数据集级 ReAct 最大迭代次数默认值
        tasks: 任务列表
    """

    name: str = "custom"
    version: str = "1.0"
    description: str = ""
    max_iterations: int = 6
    tasks: list[EvalTask] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # 访问
    # ------------------------------------------------------------------

    @property
    def task_ids(self) -> list[str]:
        return [t.task_id for t in self.tasks]

    def get(self, task_id: str) -> EvalTask | None:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        return None

    def select(self, task_ids: list[str] | None) -> "EvalDataset":
        """返回仅含指定任务的数据集（None = 全部）"""
        if not task_ids:
            return self
        wanted = set(task_ids)
        return self.model_copy(update={
            "tasks": [t for t in self.tasks if t.task_id in wanted],
        })

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "EvalDataset":
        """从 JSON / YAML 文件加载数据集（按扩展名选择解析器）"""
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"数据集文件格式错误（应为对象）: {path}")
        return cls.model_validate(data)

    def save(self, path: str | Path) -> None:
        """保存数据集（.yaml/.yml → YAML，其余 → JSON）"""
        p = Path(path)
        data = self.model_dump(mode="json")
        if p.suffix.lower() in (".yaml", ".yml"):
            import yaml

            text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# 内置数据集
# ---------------------------------------------------------------------------

def builtin_dataset() -> EvalDataset:
    """内置确定性数据集。

    使用 ``youmi.eval.tools.build_default_tools()`` 注册的评测工具，
    配合 ``MockLLMServer`` 可离线、可复现地跑通完整 LLM 调用链路。
    覆盖：单工具 / 多工具多步 / 无工具直答 等典型形态。
    """
    return EvalDataset(
        name="builtin",
        version="1.0",
        description="内置评测集：确定性工具 + 脚本化 mock LLM",
        max_iterations=6,
        tasks=[
            EvalTask(
                task_id="weather_beijing",
                task="帮我查一下北京的天气，并告诉我气温",
                expected_tools=["get_weather"],
                expect_final_contains="25℃",
                category="single_tool",
                script=[
                    EvalStep(tool="get_weather", args={"city": "北京"}),
                    EvalStep(final="北京今天晴，25℃。"),
                ],
            ),
            EvalTask(
                task_id="calc_expression",
                task="计算 (12+8)*3 的结果",
                expected_tools=["calculate"],
                expect_final_contains="60",
                category="single_tool",
                script=[
                    EvalStep(tool="calculate", args={"expression": "(12+8)*3"}),
                    EvalStep(final="(12+8)*3 的结果是 60。"),
                ],
            ),
            EvalTask(
                task_id="two_step_workflow",
                task="先查询上海的天气，然后把文本 'weather' 反转，两个都要做",
                expected_tools=["get_weather", "reverse_text"],
                tool_order_strict=True,
                expect_final_contains="rehtaew",
                category="multi_tool",
                script=[
                    EvalStep(tool="get_weather", args={"city": "上海"}),
                    EvalStep(tool="reverse_text", args={"text": "weather"}),
                    EvalStep(final="上海多云，28℃；'weather' 反转后是 'rehtaew'。"),
                ],
            ),
            EvalTask(
                task_id="direct_answer",
                task="不用工具，直接回答：1 加 1 等于几？",
                expected_tools=[],
                expect_final_contains="2",
                category="no_tool",
                script=[
                    EvalStep(final="1 加 1 等于 2。"),
                ],
            ),
        ],
    )


__all__ = ["EvalStep", "EvalTask", "EvalDataset", "builtin_dataset"]
