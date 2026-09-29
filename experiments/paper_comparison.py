"""
与前沿论文成果的对比数据 (paper_comparison)

把 YouMi Agent 的两个创新点与真实论文基线放在同一坐标系下对比，供 HTML 渲染。

对比方案（基于已核实的论文事实）：

- **APC** — Agentic Plan Caching (NeurIPS 2025, Stanford)：从 agent 执行日志提取
  计划模板，关键词精确匹配 + 小模型适配。成本 -50.31%、延迟 -27.28%、精度保持
  96.61%。
- **AgentReuse** — (计算机研究与发展 2024, 中国科大)：意图分类 + 关键参数识别的
  计划复用。有效复用率 93%、相似性 F1 0.9718、延迟 -93.12%。
- **LEGOMem** — (AAMAS 2026, Microsoft)：多 Agent procedural memory，把任务轨迹
  拆成 full-task / subtask memory units，语义 embedding RAG，OfficeBench 评测。
- **ExpeL** — (AAAI 2024)：从成功/失败经验提取规则，Agent 自进化。
- **Reflexion** — (NeurIPS 2023)：语言化自我反思（verbal RL）。
- **Voyager** — (2023)：技能库（code-as-skills）+ 自动课程，开放世界持续学习。

.. warning::
    YouMi 的量化数字来自**合成基准 + 确定性 mock**，APC / AgentReuse 来自各自
    真实 benchmark。三者口径不同，**不可直接比较**，仅作量级参考——HTML 图中已标注。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MethodProfile:
    """一个方案（方法）的画像。"""

    key: str
    name: str
    venue: str
    year: int
    org: str
    one_line: str

    # ---- 定性机制维度（供对比矩阵）----
    reuse_target: str        # 复用/记忆对象
    architecture: str        # 单 / 多 Agent
    retrieval: str           # 检索 / 匹配方式
    learning_signal: str     # 什么触发记忆写入 / 学习
    self_evolving: str       # 自进化闭环能力
    cost_opt: str            # 成本优化方式
    benchmark: str           # 验证数据
    open_source: str         # 代码开源情况

    # ---- 定量指标（None = 论文未报告该口径）----
    cost_savings_pct: float | None = None      # 成本节省（相对不复用）
    latency_savings_pct: float | None = None   # 延迟节省（相对不复用）
    quality_keep: str = ""                     # 精度/成功率保持的文字描述

    # ---- 定位图坐标（0-100 示意，仅表相对位置）----
    x_granularity: int = 50   # 复用抽象粒度（低=动作序列 → 高=多Agent骨架）
    y_evolution: int = 50     # 学习/自进化闭环（低=静态缓存 → 高=主动自进化）

    is_ours: bool = False

    @property
    def cite(self) -> str:
        return f"{self.name} ({self.venue} {self.year})"


# ---------------------------------------------------------------------------
# 方案清单
# ---------------------------------------------------------------------------

METHODS: list[MethodProfile] = [
    MethodProfile(
        key="youmi",
        name="YouMi Agent",
        venue="本框架",
        year=2026,
        org="本项目",
        one_line="多 Agent DAG 骨架复用 + 工具经验沉淀，双闭环自进化",
        reuse_target="多 Agent WorkflowPlan 骨架（角色+依赖拓扑）",
        architecture="多 Agent（Plan-then-Execute）",
        retrieval="语义向量检索（PlanMemory，sqlite-vec）",
        learning_signal="成功执行 Plan 持久化；负反馈累计触发工具修复",
        self_evolving="双闭环：计划复用 + 经验沉淀（ToolGuardian 修复）",
        cost_opt="昂贵大模型规划 / 廉价小模型适配（可选分层）",
        benchmark="合成基准（5 族 + novel，确定性 mock）",
        open_source="MIT（本仓库）",
        cost_savings_pct=53.9,
        latency_savings_pct=47.3,
        quality_keep="骨架精度 100%、陌生任务拒绝 100%",
        x_granularity=85,
        y_evolution=80,
        is_ours=True,
    ),
    MethodProfile(
        key="apc",
        name="APC",
        venue="NeurIPS",
        year=2025,
        org="Stanford",
        one_line="test-time memory：提取计划模板，关键词匹配 + 小模型适配",
        reuse_target="单 Agent 动作序列的 plan template",
        architecture="单 Agent（Plan-Act / ReAct）",
        retrieval="关键词提取(GPT-4o-mini) + 精确匹配",
        learning_signal="完成执行后提取模板入缓存",
        self_evolving="静态缓存（无主动学习）",
        cost_opt="大模型规划 + 小模型(LLaMA-3.1-8B)适配",
        benchmark="多个真实 agent 应用",
        open_source="未开源",
        cost_savings_pct=50.31,
        latency_savings_pct=27.28,
        quality_keep="保持 96.61% 最优准确率",
        x_granularity=15,
        y_evolution=20,
    ),
    MethodProfile(
        key="agentreuse",
        name="AgentReuse",
        venue="计算机研究与发展",
        year=2024,
        org="中国科学技术大学",
        one_line="意图分类 + 关键参数识别的计划复用",
        reuse_target="意图 + 关键参数对应的执行计划",
        architecture="单 Agent（个人助手 / AIoT）",
        retrieval="意图分类 + 关键参数识别",
        learning_signal="请求间语义相似性界定后复用",
        self_evolving="静态复用（无主动学习）",
        cost_opt="毫秒级额外延迟实现复用",
        benchmark="真实数据集（个人助手）",
        open_source="未开源",
        cost_savings_pct=None,
        latency_savings_pct=93.12,
        quality_keep="有效复用率 93%、相似性 F1 0.9718",
        x_granularity=35,
        y_evolution=20,
    ),
    MethodProfile(
        key="legomem",
        name="LEGOMem",
        venue="AAMAS",
        year=2026,
        org="Microsoft",
        one_line="模块化 procedural memory：任务轨迹拆成可复用 memory units",
        reuse_target="full-task / subtask memory units（多 Agent 轨迹）",
        architecture="多 Agent（orchestrator + task agents）",
        retrieval="语义 embedding RAG（三种检索策略）",
        learning_signal="成功执行轨迹蒸馏为结构化记忆",
        self_evolving="记忆增强（RAG），非主动自进化",
        cost_opt="让 SLM 团队借记忆逼近 LLM 团队",
        benchmark="OfficeBench",
        open_source="未开源",
        cost_savings_pct=None,
        latency_savings_pct=None,
        quality_keep="多档成功率提升，SLM 团队显著受益",
        x_granularity=78,
        y_evolution=45,
    ),
    MethodProfile(
        key="expel",
        name="ExpeL",
        venue="AAAI",
        year=2024,
        org="UCLA / UIUC",
        one_line="从成功/失败经验提取规则，Agent 自进化",
        reuse_target="经验规则（insights）",
        architecture="单 Agent",
        retrieval="经验库检索",
        learning_signal="成功/失败经验 → 提取规则",
        self_evolving="强（经验 → 规则自进化）",
        cost_opt="无成本优化",
        benchmark="HotpotQA 等问答/交互",
        open_source="未开源",
        cost_savings_pct=None,
        latency_savings_pct=None,
        quality_keep="跨任务准确率提升",
        x_granularity=50,
        y_evolution=75,
    ),
    MethodProfile(
        key="reflexion",
        name="Reflexion",
        venue="NeurIPS",
        year=2023,
        org="Northeastern / MIT",
        one_line="语言化自我反思（verbal RL），从反馈改进",
        reuse_target="语言反思（verbal feedback）",
        architecture="单 Agent",
        retrieval="记忆中的反思轨迹",
        learning_signal="执行反馈 → 生成语言反思",
        self_evolving="中高（verbal RL）",
        cost_opt="无成本优化",
        benchmark="HumanEval / ALFWorld 等",
        open_source="未开源",
        cost_savings_pct=None,
        latency_savings_pct=None,
        quality_keep="pass@1 / 成功率提升",
        x_granularity=25,
        y_evolution=65,
    ),
    MethodProfile(
        key="voyager",
        name="Voyager",
        venue="(arXiv)",
        year=2023,
        org="NVIDIA / Caltech",
        one_line="技能库（code-as-skills）+ 自动课程，开放世界持续学习",
        reuse_target="可复用技能代码",
        architecture="单 Agent（具身，Minecraft）",
        retrieval="技能库检索",
        learning_signal="试错成功 → 沉淀技能",
        self_evolving="强（技能库 + 自动课程）",
        cost_opt="无成本优化",
        benchmark="Minecraft 开放世界",
        open_source="部分开源",
        cost_savings_pct=None,
        latency_savings_pct=None,
        quality_keep="独特物品解锁数 / 探索里程",
        x_granularity=60,
        y_evolution=85,
    ),
]


def ours() -> MethodProfile:
    return next(m for m in METHODS if m.is_ours)


# ---------------------------------------------------------------------------
# 定性对比维度（供矩阵表渲染）
# ---------------------------------------------------------------------------

# 每个元素: (维度中文名, MethodProfile 字段名)
QUALITATIVE_DIMENSIONS: list[tuple[str, str]] = [
    ("复用/记忆对象", "reuse_target"),
    ("架构模式", "architecture"),
    ("检索/匹配方式", "retrieval"),
    ("学习信号", "learning_signal"),
    ("自进化闭环", "self_evolving"),
    ("成本优化", "cost_opt"),
    ("验证数据", "benchmark"),
    ("开源", "open_source"),
]

# 有定量数字的方案（成本/延迟节省，相对不复用 baseline）
QUANT_SCHEMES = [
    m for m in METHODS
    if m.cost_savings_pct is not None or m.latency_savings_pct is not None
]


def get(key: str) -> MethodProfile:
    return next(m for m in METHODS if m.key == key)


__all__ = [
    "MethodProfile",
    "METHODS",
    "QUALITATIVE_DIMENSIONS",
    "QUANT_SCHEMES",
    "ours",
    "get",
]
