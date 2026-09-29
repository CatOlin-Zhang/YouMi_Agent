"""
合成基准任务集 (benchmark)

为验证「计划复用」创新点提供**带真值标签**的任务语料：

- 每个 *任务族 (task family)* 包含一个 seed（冷启动任务）和若干 variants
  （语义等价、实体不同的改写，理应复用同一套 Plan 骨架）。
- 每个族给出 *gold plan*（正确骨架：step_id / role / depends_on 拓扑）。
- 另含 novel 任务（不属于任何族），用于验证「陌生任务不应误命中」。

真值标签使评测可以计算：
- 命中率 hit_rate（复用发生比例）
- 复用精度 precision（命中的骨架是否真的正确）
- 复用召回 recall（同族改写任务中被命中的比例）

对比现有文献（Agentic Plan Caching, NeurIPS 2025; AgentReuse; LEGOMem），
本基准的关键差异是**多 Agent DAG 骨架复用**（而非单 Agent 动作序列），
因此正确性判定聚焦于「角色 + 依赖拓扑」而非单步动作。

用法::

    from experiments.benchmark import Benchmark
    b = Benchmark()
    b.warmup_tasks()   # -> [seed1, seed2, ...]
    b.variant_tasks()  # -> [v1a, v1b, ...]
    b.novel_tasks()    # -> [novel1, novel2]
"""

from __future__ import annotations

from dataclasses import dataclass, field

from youmi.coordinator.plan import WorkflowPlan, WorkflowStep


# ---------------------------------------------------------------------------
# 骨架等价性判定
# ---------------------------------------------------------------------------

def skeleton_key(plan: WorkflowPlan) -> tuple:
    """提取 Plan 的骨架签名（忽略 task 文本，只保留结构与角色）。

    骨架 = 有序 (step_id, role, 排序后的 depends_on) 元组列表。
    """
    return tuple(
        sorted(
            (s.step_id, s.role, tuple(sorted(s.depends_on)))
            for s in plan.steps
        )
    )


def role_skeleton_key(plan: WorkflowPlan) -> tuple:
    """提取「角色 + 依赖拓扑」骨架（忽略 step_id 与 task 文本）。

    真实 LLM 会自由命名 step_id（如把 researcher 直接当 step_id），与手写 gold 的
    step_id（如 research）不一致，导致 `skeleton_key` 误判为「骨架不同」。本函数把
    depends_on 引用的 step_id 映射回角色，得到角色级依赖图，作为真实模型评测下的
    正确性判定口径（回答「角色组成与依赖结构是否等价」）。

    注意：同 role 且同依赖的两个步骤会坍缩，属可接受的边界（真实任务少见）。
    """
    id2role = {s.step_id: s.role for s in plan.steps}
    return tuple(sorted(
        (s.role, tuple(sorted(id2role.get(d, d) for d in s.depends_on)))
        for s in plan.steps
    ))


def same_skeleton(a: WorkflowPlan, b: WorkflowPlan) -> bool:
    """两个 Plan 的骨架（角色 + 依赖拓扑）是否一致。"""
    return skeleton_key(a) == skeleton_key(b)


def same_role_skeleton(a: WorkflowPlan, b: WorkflowPlan) -> bool:
    """两个 Plan 的角色级骨架是否一致（忽略 step_id 命名差异）。"""
    return role_skeleton_key(a) == role_skeleton_key(b)


# ---------------------------------------------------------------------------
# 便捷构造
# ---------------------------------------------------------------------------

def _plan(name: str, steps: list[dict]) -> WorkflowPlan:
    """从 dict 列表构造 WorkflowPlan。"""
    return WorkflowPlan(
        name=name,
        steps=[WorkflowStep(**s) for s in steps],
    )


# ---------------------------------------------------------------------------
# 任务族
# ---------------------------------------------------------------------------

@dataclass
class TaskFamily:
    """一个可复用 Plan 骨架的任务族。"""

    family_id: str
    seed: str                      # 冷启动任务（首次规划后写入记忆）
    variants: list[str]            # 语义等价改写（应命中复用）
    gold_plan: WorkflowPlan        # 正确骨架
    # 便于对照展示的角色链
    role_chain: str = ""


@dataclass
class NovelTask:
    """不属于任何族的陌生任务（应走全量规划，不应误命中）。"""

    task: str
    gold_plan: WorkflowPlan


# ---------------------------------------------------------------------------
# 基准
# ---------------------------------------------------------------------------

class Benchmark:
    """合成任务基准，提供真值标签与任务流。"""

    def __init__(self, n_families: int | None = None) -> None:
        # n_families 用于真实模型小规模跑通：只取前 N 个任务族（novel 保留，
        # 以便仍能验证「陌生任务应 miss」）。默认 None = 全量 5 族。
        all_families = _build_families()
        self.families: list[TaskFamily] = (
            all_families[:n_families] if n_families is not None else all_families
        )
        self.novel: list[NovelTask] = _build_novel()

        # task 文本 -> gold plan（含 seed / variants / novel）
        self._plans_by_task: dict[str, WorkflowPlan] = {}
        self._family_by_task: dict[str, str] = {}

        for fam in self.families:
            self._plans_by_task[fam.seed] = fam.gold_plan
            self._family_by_task[fam.seed] = fam.family_id
            for v in fam.variants:
                self._plans_by_task[v] = fam.gold_plan
                self._family_by_task[v] = fam.family_id

        for n in self.novel:
            self._plans_by_task[n.task] = n.gold_plan
            # novel 任务 family 为 None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def plans_by_task(self) -> dict[str, WorkflowPlan]:
        return dict(self._plans_by_task)

    def gold_plan_for(self, task: str) -> WorkflowPlan | None:
        return self._plans_by_task.get(task)

    def family_of(self, task: str) -> str | None:
        """返回任务所属族 id；novel 任务返回 None。"""
        return self._family_by_task.get(task)

    # ------------------------------------------------------------------
    # 任务流
    # ------------------------------------------------------------------

    def warmup_tasks(self) -> list[str]:
        """冷启动任务（各族的 seed）。"""
        return [f.seed for f in self.families]

    def variant_tasks(self) -> list[str]:
        """同族改写任务（应命中复用的主体）。"""
        out: list[str] = []
        for f in self.families:
            out.extend(f.variants)
        return out

    def novel_tasks(self) -> list[str]:
        """陌生任务（应 miss）。"""
        return [n.task for n in self.novel]

    def test_tasks(self) -> list[str]:
        """评测主体 = variants + novel。"""
        return self.variant_tasks() + self.novel_tasks()

    def all_tasks(self) -> list[str]:
        """完整任务流 = warmup + test。"""
        return self.warmup_tasks() + self.test_tasks()

    def summary(self) -> dict:
        return {
            "families": len(self.families),
            "warmup_tasks": len(self.warmup_tasks()),
            "variant_tasks": len(self.variant_tasks()),
            "novel_tasks": len(self.novel_tasks()),
            "total_tasks": len(self.all_tasks()),
        }


# ---------------------------------------------------------------------------
# 任务族定义
# ---------------------------------------------------------------------------

def _build_families() -> list[TaskFamily]:
    return [
        TaskFamily(
            family_id="industry_report",
            seed="调研 HBM 行业现状并生成投资分析报告",
            variants=[
                "调研 GPU 行业现状并生成投资分析报告",
                "调研锂电池行业现状并生成投资分析报告",
                "调研光伏行业现状并生成投资分析报告",
            ],
            role_chain="researcher → analyst → writer",
            gold_plan=_plan("行业分析报告", [
                {"step_id": "research", "role": "researcher",
                 "task": "调研目标行业现状、主要厂商与技术趋势", "depends_on": []},
                {"step_id": "analyze", "role": "analyst",
                 "task": "分析竞争格局与投资价值", "depends_on": ["research"]},
                {"step_id": "report", "role": "writer",
                 "task": "撰写完整的行业分析报告", "depends_on": ["analyze"]},
            ]),
        ),
        # 与 industry_report 共享「分析/报告」词汇，用于制造跨族误命中
        # （验证复用精度 < 100% 的代价）
        TaskFamily(
            family_id="data_analysis",
            seed="分析销售数据并生成分析报告",
            variants=[
                "分析用户行为数据并生成分析报告",
                "分析营收数据并生成分析报告",
                "分析库存数据并生成分析报告",
            ],
            role_chain="data_engineer → data_engineer → data_analyst → writer",
            gold_plan=_plan("数据分析报告", [
                {"step_id": "collect", "role": "data_engineer",
                 "task": "采集原始数据", "depends_on": []},
                {"step_id": "clean", "role": "data_engineer",
                 "task": "清洗并标准化数据", "depends_on": ["collect"]},
                {"step_id": "analyze", "role": "data_analyst",
                 "task": "统计分析与洞察", "depends_on": ["clean"]},
                {"step_id": "report", "role": "writer",
                 "task": "输出数据分析报告", "depends_on": ["analyze"]},
            ]),
        ),
        TaskFamily(
            family_id="code_dev",
            seed="开发一个用户登录模块并完成代码审查",
            variants=[
                "开发一个订单管理模块并完成代码审查",
                "开发一个支付模块并完成代码审查",
                "开发一个消息通知模块并完成代码审查",
            ],
            role_chain="architect → coder → reviewer → tester",
            gold_plan=_plan("软件开发与审查", [
                {"step_id": "design", "role": "architect",
                 "task": "设计模块接口与数据结构", "depends_on": []},
                {"step_id": "code", "role": "coder",
                 "task": "实现模块代码", "depends_on": ["design"]},
                {"step_id": "review", "role": "reviewer",
                 "task": "代码审查并给出意见", "depends_on": ["code"]},
                {"step_id": "test", "role": "tester",
                 "task": "编写并运行测试", "depends_on": ["review"]},
            ]),
        ),
        TaskFamily(
            family_id="web_crawl",
            seed="爬取新闻网站并生成内容摘要",
            variants=[
                "爬取电商网站并生成内容摘要",
                "爬取博客网站并生成内容摘要",
                "爬取论坛网站并生成内容摘要",
            ],
            role_chain="crawler → parser → summarizer",
            gold_plan=_plan("网页爬取与摘要", [
                {"step_id": "crawl", "role": "crawler",
                 "task": "抓取目标网页", "depends_on": []},
                {"step_id": "parse", "role": "parser",
                 "task": "解析网页结构提取正文", "depends_on": ["crawl"]},
                {"step_id": "summarize", "role": "summarizer",
                 "task": "生成内容摘要", "depends_on": ["parse"]},
            ]),
        ),
        TaskFamily(
            family_id="translation",
            seed="翻译一份技术文档并进行校对",
            variants=[
                "翻译一份法律合同并进行校对",
                "翻译一份产品手册并进行校对",
                "翻译一份学术论文并进行校对",
            ],
            role_chain="translator → proofreader",
            gold_plan=_plan("翻译与校对", [
                {"step_id": "translate", "role": "translator",
                 "task": "翻译目标文本", "depends_on": []},
                {"step_id": "proofread", "role": "proofreader",
                 "task": "校对译文并润色", "depends_on": ["translate"]},
            ]),
        ),
    ]


def _build_novel() -> list[NovelTask]:
    return [
        NovelTask(
            task="帮我写一首关于春天的五言绝句",
            gold_plan=_plan("诗词创作", [
                {"step_id": "compose", "role": "poet",
                 "task": "创作五言绝句", "depends_on": []},
            ]),
        ),
        NovelTask(
            task="将摄氏 30 度换算成华氏度",
            gold_plan=_plan("单位换算", [
                {"step_id": "convert", "role": "generalist",
                 "task": "执行单位换算", "depends_on": []},
            ]),
        ),
    ]


__all__ = [
    "Benchmark",
    "TaskFamily",
    "NovelTask",
    "same_skeleton",
    "skeleton_key",
    "same_role_skeleton",
    "role_skeleton_key",
]
