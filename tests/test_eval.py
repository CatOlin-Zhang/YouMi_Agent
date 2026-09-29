"""
Eval 基准测试 (eval01)

覆盖:
- 数据集：内置结构 / JSON·YAML 存取 roundtrip / select 过滤 / 模型校验
- 工具：确定性行为（天气 / 安全计算 / 反转）
- 执行器：真实链路单任务 / 多步 strict 任务 / 子集执行 / 脚本耗尽检测
- 评分器：完成判定 / 工具匹配（strict 与非 strict）/ missing·extra / 汇总与阈值
- CLI：--list / 内置数据集整体运行（退出码）/ --report JSON 输出
"""

from __future__ import annotations

import json

import pytest

from youmi.eval import (
    EvalDataset,
    EvalRunner,
    EvalStep,
    EvalTask,
    TaskRun,
    build_default_tools,
    builtin_dataset,
    score_task,
    summarize,
)
from youmi.eval.__main__ import main as cli_main
from youmi.eval.scorer import EvalSummary, _match_tools
from youmi.eval.tools import calculate, get_weather, reverse_text


# =========================================================================
# 数据集
# =========================================================================

class TestDataset:

    def test_builtin_structure(self):
        ds = builtin_dataset()
        assert ds.name == "builtin"
        assert len(ds.tasks) == 4
        assert ds.task_ids == [
            "weather_beijing", "calc_expression",
            "two_step_workflow", "direct_answer",
        ]
        # 每个任务都有脚本且最后一步是 final
        for t in ds.tasks:
            assert t.script, f"{t.task_id} 缺少脚本"
            assert t.script[-1].is_tool is False

    def test_get_and_select(self):
        ds = builtin_dataset()
        assert ds.get("weather_beijing") is not None
        assert ds.get("nope") is None

        sub = ds.select(["weather_beijing", "direct_answer"])
        assert sub.task_ids == ["weather_beijing", "direct_answer"]
        assert ds.select(None) is ds

    def test_json_roundtrip(self, tmp_path):
        ds = builtin_dataset()
        path = tmp_path / "ds.json"
        ds.save(path)
        loaded = EvalDataset.load(path)
        assert loaded.name == ds.name
        assert loaded.task_ids == ds.task_ids
        assert loaded.tasks[0].script[0].tool == "get_weather"

    def test_yaml_roundtrip(self, tmp_path):
        ds = builtin_dataset()
        path = tmp_path / "ds.yaml"
        ds.save(path)
        loaded = EvalDataset.load(path)
        assert loaded.task_ids == ds.task_ids
        assert loaded.tasks[2].tool_order_strict is True

    def test_step_model_validation(self):
        step = EvalStep(tool="get_weather", args={"city": "北京"})
        assert step.is_tool
        final = EvalStep(final="done")
        assert not final.is_tool


# =========================================================================
# 评测工具（确定性）
# =========================================================================

class TestEvalTools:

    def test_weather_deterministic(self):
        assert "25℃" in get_weather("北京")
        assert "28℃" in get_weather("上海")
        assert "暂无" in get_weather("火星")

    def test_calculate_safe(self):
        assert calculate("(12+8)*3") == "60"
        assert calculate("7/2") == "3.5"
        assert calculate("2**10") == "1024"
        # 非法表达式 → 错误信息（不抛异常、不执行代码）
        assert calculate("__import__('os')").startswith("计算错误")
        assert calculate("1;2").startswith("计算错误")

    def test_reverse_and_registry(self):
        assert reverse_text("weather") == "rehtaew"
        reg = build_default_tools()
        assert set(reg.tool_names) == {
            "get_weather", "calculate", "reverse_text", "word_count",
        }


# =========================================================================
# 执行器（真实链路：LLMClient → MockServer → Agent ReAct → ToolRegistry）
# =========================================================================

class TestRunnerExecution:

    async def test_single_task_success(self):
        ds = builtin_dataset().select(["weather_beijing"])
        runner = EvalRunner(ds)
        runs = await runner.run()

        assert len(runs) == 1
        run = runs[0]
        assert run.status == "completed"
        assert "25℃" in run.output
        assert run.actual_tools == ["get_weather"]
        assert run.tool_arguments == [{"city": "北京"}]
        assert "25℃" in run.tool_results[0]
        assert run.llm_calls == 2  # tool_call 轮 + final 轮
        assert run.total_tokens > 0
        assert not run.script_exhausted
        assert not run.error

    async def test_multi_step_strict_task(self):
        ds = builtin_dataset().select(["two_step_workflow"])
        runner = EvalRunner(ds)
        runs = await runner.run()

        run = runs[0]
        assert run.actual_tools == ["get_weather", "reverse_text"]
        assert run.llm_calls == 3
        assert "rehtaew" in run.output

        score = score_task(run, ds.tasks[0])
        assert score.completed
        assert score.tool_match
        assert score.missing_tools == []
        assert score.extra_tools == []

    async def test_run_subset_and_isolated_scripts(self):
        """连续跑多个任务时脚本互不泄漏"""
        ds = builtin_dataset().select(["weather_beijing", "calc_expression"])
        runner = EvalRunner(ds)
        runs = await runner.run()
        assert [r.task_id for r in runs] == ["weather_beijing", "calc_expression"]
        assert runs[0].actual_tools == ["get_weather"]
        assert runs[1].actual_tools == ["calculate"]
        assert "60" in runs[1].output

    async def test_script_exhausted_when_model_overacts(self):
        """脚本耗尽：ReAct 轮次超过脚本长度时被标记，任务判为未完成"""
        ds = EvalDataset(name="exhaust", max_iterations=4, tasks=[
            EvalTask(
                task_id="never_finish",
                task="随便做点什么",
                expected_tools=["get_weather"],
                expect_final_contains="完成",
                # 脚本只有工具步骤，永远不给 final → 第 2 轮起走 mock 兜底
                script=[EvalStep(tool="get_weather", args={"city": "北京"})],
            ),
        ])
        runner = EvalRunner(ds)
        runs = await runner.run()
        run = runs[0]

        assert run.script_exhausted
        score = score_task(run, ds.tasks[0])
        # 兜底文本 "mock-response" 不含预期关键词 → 任务未完成
        assert score.completed is False
        assert run.actual_tools[0] == "get_weather"

    async def test_tool_mismatch_detected(self):
        """脚本调用了与期望不同的工具 → 工具不匹配"""
        ds = EvalDataset(name="mismatch", tasks=[
            EvalTask(
                task_id="wrong_tool",
                task="查北京天气",
                expected_tools=["calculate"],  # 故意期望错误
                script=[
                    EvalStep(tool="get_weather", args={"city": "北京"}),
                    EvalStep(final="北京晴，25℃。"),
                ],
            ),
        ])
        runner = EvalRunner(ds)
        runs = await runner.run()
        score = score_task(runs[0], ds.tasks[0])
        assert score.completed
        assert not score.tool_match
        assert score.missing_tools == ["calculate"]
        assert score.extra_tools == ["get_weather"]


# =========================================================================
# 评分器
# =========================================================================

class TestScorer:

    def test_match_tools_non_strict_set_semantics(self):
        task = EvalTask(task_id="t", task="x", expected_tools=["a", "b"])
        match, missing, extra = _match_tools(["b", "a", "a"], task)
        assert match  # 顺序无关、重复忽略
        assert missing == [] and extra == []

        match, missing, extra = _match_tools(["a"], task)
        assert not match
        assert missing == ["b"]

        match, missing, extra = _match_tools(["a", "b", "c"], task)
        assert not match
        assert extra == ["c"]

    def test_match_tools_strict_order(self):
        task = EvalTask(
            task_id="t", task="x",
            expected_tools=["a", "b"], tool_order_strict=True,
        )
        assert _match_tools(["a", "b"], task)[0]
        assert not _match_tools(["b", "a"], task)[0]

    def test_completion_rules(self):
        task = EvalTask(task_id="t", task="x", expect_final_contains="OK")

        ok = TaskRun(task_id="t", status="completed", output="结果 OK")
        assert score_task(ok, task).completed

        # 缺少预期内容
        bad_content = TaskRun(task_id="t", status="completed", output="结果 NG")
        assert not score_task(bad_content, task).completed

        # 迭代耗尽
        exhausted = TaskRun(
            task_id="t", status="completed",
            output="达到最大迭代次数 (2)，任务可能未完成。",
        )
        assert not score_task(exhausted, task).completed

        # 执行异常
        errored = TaskRun(task_id="t", status="failed", output="", error="RuntimeError: x")
        assert not score_task(errored, task).completed

    def test_summary_aggregation_and_thresholds(self):
        ds = builtin_dataset().select(["weather_beijing", "calc_expression"])
        scores = [
            score_task(TaskRun(
                task_id="weather_beijing", status="completed",
                output="北京晴，25℃", actual_tools=["get_weather"],
                llm_calls=2, total_tokens=100, duration_ms=10.0,
            ), ds.tasks[0]),
            score_task(TaskRun(
                task_id="calc_expression", status="completed",
                output="结果是 60", actual_tools=["calculate"],
                llm_calls=2, total_tokens=200, duration_ms=20.0,
            ), ds.tasks[1]),
        ]
        summary = summarize(ds, scores)
        assert isinstance(summary, EvalSummary)
        assert summary.total == 2
        assert summary.completed == 2
        assert summary.completion_rate == 1.0
        assert summary.tool_accuracy == 1.0
        assert summary.total_tokens == 300
        assert summary.avg_tokens_per_task == 150.0
        assert summary.passed()

        # 阈值不达标
        assert not summary.passed(min_completion=1.01)
        summary.completed = 1
        assert not summary.passed(min_completion=1.0)

    def test_summary_text_report(self):
        ds = builtin_dataset().select(["weather_beijing"])
        score = score_task(TaskRun(
            task_id="weather_beijing", status="completed",
            output="北京晴，25℃", actual_tools=["get_weather"],
        ), ds.tasks[0])
        text = summarize(ds, [score]).format_text()
        assert "builtin" in text
        assert "100.0%" in text
        assert "[PASS] weather_beijing" in text

    def test_summary_dict_serializable(self):
        ds = builtin_dataset().select(["direct_answer"])
        score = score_task(TaskRun(
            task_id="direct_answer", status="completed",
            output="1 加 1 等于 2。", actual_tools=[],
        ), ds.tasks[0])
        data = summarize(ds, [score]).as_dict()
        json.dumps(data)  # 可序列化
        assert data["tasks"][0]["task_id"] == "direct_answer"


# =========================================================================
# CLI
# =========================================================================

class TestCLI:

    def test_list_output(self, capsys):
        rc = cli_main(["--list"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "weather_beijing" in out
        assert "two_step_workflow" in out

    def test_full_builtin_run_exit_zero(self, capsys, tmp_path):
        report = tmp_path / "report.json"
        rc = cli_main(["--report", str(report)])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "任务完成率: 4/4" in out
        assert "工具准确率: 4/4" in out
        assert report.exists()
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["completion_rate"] == 1.0
        assert len(data["tasks"]) == 4

    def test_unknown_task_exit_code(self, capsys):
        rc = cli_main(["--tasks", "not_exist"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "未知任务" in err

    def test_task_subset_run(self, capsys):
        rc = cli_main(["--tasks", "direct_answer"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "任务完成率: 1/1" in out
