"""
YouMi Agent 实验与评测包 (experiments/)

针对框架核心创新点提供可运行、可量化对比的验证体系：

1. **计划复用 (Plan-then-Execute + PlanMemory)**
   对照基线（无记忆全量规划）评测：规划 LLM 调用次数、token 成本、
   延迟、缓存命中率、复用精度/召回。

2. **经验沉淀闭环 (GlobalMemory + FeedbackCollector + ToolGuardian)**
   模拟「负反馈累积 → 阈值触发 → 诊断修复 → 标记解决」闭环，
   产出学习曲线（修复前后工具成功率对比）。

3. **真实模型接入（Ollama）**：`real_llm.py` 预留本地 Ollama 接口，
   补齐 `LLMClient.complete()` 契约，模型下载后可跑真实消融。

4. **与前沿论文对比**：`paper_comparison.py` + `render_html.py` 生成
   自包含 HTML 对比报告（定位图 / 定量收益 / 机制矩阵）。

5. **工具缓存 / 三级审批等工程机制** 的单元级验证见 tests/。

mock 实验不依赖网络或真实模型，可离线复现；真实模型接口见 README.md 第 7 节。
"""

__version__ = "0.1.0"

__all__ = [
    "benchmark",
    "fake_llm",
    "real_llm",
    "metrics",
    "plan_reuse_eval",
    "experience_eval",
    "paper_comparison",
    "render_html",
    "report",
]
