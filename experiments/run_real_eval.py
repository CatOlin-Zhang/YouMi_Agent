"""
真实模型评测入口 (run_real_eval)

用本地 Ollama 跑真实「计划复用」消融，与 mock 版 ``run_all.py`` 的差异：

- LLM：真实模型生成（默认 ``qwen2.5:7b-instruct-q3_K_M``）
- Embedding：真实向量（默认 ``nomic-embed-text``，768 维）
- 成本 / 延迟：真实 usage + 真实计时；本地无计费，成本按「token × 参数量」估算
- 支持分层：``--adapt-model qwen2.5:0.5b`` 实现「大模型规划 + 小模型适配」

用法::

    python -m experiments.run_real_eval --families 2
    python -m experiments.run_real_eval --families 2 --adapt-model qwen2.5:0.5b
    python -m experiments.run_real_eval --md experiments/REPORT_REAL.md

注意：

- 7b 每次生成约 10~25s，全量 5 族 × 2 轮约 10 分钟起，建议先 ``--families 2`` 跑通。
- 单模型（不传 ``--adapt-model``）时复用主要省**延迟**，token 成本可能因重传骨架略增；
  要真正「省钱」需分层（7b 规划 + 小模型适配），对应 Agentic Plan Caching 的逻辑。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics

from experiments.benchmark import Benchmark
from experiments.fake_llm import CostModel
from experiments.metrics import Comparison
from experiments.plan_reuse_eval import run_plan_reuse
from experiments.real_llm import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LLM_MODEL,
    DEFAULT_OLLAMA_BASE_URL,
    check_ollama,
    make_metered_ollama_llm,
    make_ollama_embedding,
)
from experiments.report import render_comparison

# 各 embedding 模型的向量维度（sqlite-vec 建表需要精确维度）
_EMBEDDING_DIMS = {
    "nomic-embed-text": 768,
    "bge-m3": 1024,
}

# 各 embedding 模型的默认命中阈值（由 compare_embeddings 实测的「同族/跨族」分布定标）：
# - nomic-embed-text: 同族 0.66~1.00 / 跨族 0.57~0.91，重叠严重 → 阈值 0.60 只能偏保守
# - bge-m3:           同族 0.67~0.92 / 跨族 0.39~0.66，干净切分 → 阈值 0.66 完美分离
_MODEL_THRESHOLDS = {
    "nomic-embed-text": 0.60,
    "bge-m3": 0.66,
}
DEFAULT_THRESHOLD = 0.66  # 默认按 bge-m3 定标


def _embedding_dim(model: str) -> int:
    """按 embedding 模型名返回向量维度（未知默认 768）。"""
    for name, dim in _EMBEDDING_DIMS.items():
        if name in model:
            return dim
    return 768


def _threshold_for(model: str) -> float:
    """按 embedding 模型名返回推荐命中阈值（未知默认 DEFAULT_THRESHOLD）。"""
    for name, th in _MODEL_THRESHOLDS.items():
        if name in model:
            return th
    return DEFAULT_THRESHOLD


def _params_from_name(model: str) -> float:
    """从模型名估算参数量（B），作为相对计价权重（未知按 1B 计）。"""
    m = re.search(r"(\d+(?:\.\d+)?)b", model, re.IGNORECASE)
    return float(m.group(1)) if m else 1.0


def _aggregate(comparisons: list[Comparison]) -> str:
    """对多次运行的关键指标取均值 ± 标准差，渲染为可读文本。

    真实模型推理时间受系统负载影响有波动（如 3b 适配延迟节省 +6.6% ↔ -0.8%），
    多次运行取均值 ± 标准差能给出更稳健的结论。
    """
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append(f"多次运行聚合（n={len(comparisons)}，均值 ± 标准差）")
    lines.append("=" * 64)

    def _fmt(vals: list[float]) -> str:
        mean = statistics.mean(vals)
        if len(vals) > 1:
            sd = statistics.stdev(vals)
            return f"{mean * 100:+.1f}% ± {sd * 100:.1f}%"
        return f"{mean * 100:+.1f}%"

    lines.append(f"{'节省指标(正=节省)':<24}{'值':>20}")
    lines.append("-" * 64)
    for name, getter in (
        ("规划成本(价格加权)", lambda c: c.cost_savings),
        ("延迟", lambda c: c.latency_savings),
        ("全量规划调用", lambda c: c.full_gen_call_savings),
    ):
        lines.append(f"{name:<24}{_fmt([getter(c) for c in comparisons]):>20}")

    lines.append("")
    lines.append(f"{'复用质量指标(reuse侧)':<24}{'值':>20}")
    lines.append("-" * 64)
    for name, getter in (
        ("命中率", lambda c: c.reuse.hit_rate),
        ("骨架保真率", lambda c: c.reuse.fidelity_rate),
        ("复用召回", lambda c: c.reuse.reuse_recall),
        ("陌生任务拒绝率", lambda c: c.reuse.novel_precision),
    ):
        lines.append(f"{name:<24}{_fmt([getter(c) for c in comparisons]):>20}")
    return "\n".join(lines)


async def _ensure_ready(
    base_url: str,
    llm_model: str,
    emb_model: str,
) -> bool:
    """探活 LLM + embedding 模型，返回是否全部就绪。"""
    ok = True
    for model, kind in ((llm_model, "规划 LLM"), (emb_model, "Embedding")):
        st = await check_ollama(base_url, model)
        print(f"[探活] {kind} {model}: {'OK' if st['ok'] else '缺失'} — {st['hint']}")
        ok = ok and st["ok"]
    return ok


async def run_real_comparison(
    benchmark: Benchmark,
    *,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    llm_model: str = DEFAULT_LLM_MODEL,
    adapt_model: str | None = None,
    emb_model: str = DEFAULT_EMBEDDING_MODEL,
    threshold: float = DEFAULT_THRESHOLD,
) -> Comparison:
    """跑真实 baseline vs reuse 对比（每轮独立客户端，记账不叠加）。"""
    # 按参数量计价（本地无计费，用参数量近似「昂贵 vs 廉价」）
    cost_full = CostModel(price_per_token=_params_from_name(llm_model))
    cost_adapt = CostModel(price_per_token=_params_from_name(adapt_model or llm_model))
    emb_dim = _embedding_dim(emb_model)

    print("\n== baseline（无记忆，全量规划）==")
    llm_base = make_metered_ollama_llm(
        base_url=base_url, model=llm_model, adapt_model=adapt_model,
        cost_full=cost_full, cost_adapt=cost_adapt,
    )
    emb_base = make_ollama_embedding(base_url=base_url, model=emb_model)
    baseline = await run_plan_reuse(
        benchmark, use_memory=False,
        similarity_threshold=threshold, embedding_dim=emb_dim,
        llm_client=llm_base, embedding_client=emb_base,
        use_role_skeleton=True,
    )
    await llm_base.close()
    await emb_base.close()

    print("\n== reuse（有记忆，命中复用）==")
    llm_reuse = make_metered_ollama_llm(
        base_url=base_url, model=llm_model, adapt_model=adapt_model,
        cost_full=cost_full, cost_adapt=cost_adapt,
    )
    emb_reuse = make_ollama_embedding(base_url=base_url, model=emb_model)
    reuse = await run_plan_reuse(
        benchmark, use_memory=True,
        similarity_threshold=threshold, embedding_dim=emb_dim,
        llm_client=llm_reuse, embedding_client=emb_reuse,
        use_role_skeleton=True,
    )
    await llm_reuse.close()
    await emb_reuse.close()

    return Comparison(baseline=baseline, reuse=reuse)


def main() -> None:
    parser = argparse.ArgumentParser(description="真实模型（Ollama）计划复用消融")
    parser.add_argument("--families", type=int, default=None,
                        help="只取前 N 个任务族（默认 None = 全量 5 族，真实模型建议 2）")
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL,
                        help=f"规划大模型（默认 {DEFAULT_LLM_MODEL}）")
    parser.add_argument("--adapt-model", default=None,
                        help="适配小模型（默认 None = 与规划同模型；传 qwen2.5:0.5b 分层）")
    parser.add_argument("--emb-model", default=DEFAULT_EMBEDDING_MODEL,
                        help=f"embedding 模型（默认 {DEFAULT_EMBEDDING_MODEL}）")
    parser.add_argument("--threshold", type=float, default=None,
                        help="复用命中相似度阈值（默认按 emb 模型自适应：bge-m3=0.66 / nomic=0.60）")
    parser.add_argument("--base-url", default=DEFAULT_OLLAMA_BASE_URL,
                        help="Ollama OpenAI 兼容端点")
    parser.add_argument("--runs", type=int, default=1,
                        help="重复运行次数（>1 时对节省指标取均值 ± 标准差，抗波动）")
    parser.add_argument("--md", metavar="PATH", default=None,
                        help="额外输出 Markdown 报告路径")
    args = parser.parse_args()

    threshold = args.threshold if args.threshold is not None else _threshold_for(args.emb_model)

    async def _run() -> None:
        if not await _ensure_ready(args.base_url, args.llm_model, args.emb_model):
            print("\n[中止] 模型未就绪，请先 `ollama pull` 对应模型。")
            return

        if args.adapt_model is not None:
            if not await _ensure_ready(args.base_url, args.adapt_model, args.emb_model):
                print("\n[中止] 适配模型未就绪。")
                return

        benchmark = Benchmark(n_families=args.families)
        s = benchmark.summary()
        print(
            f"\n[基准] 任务族={s['families']} warmup={s['warmup_tasks']} "
            f"variant={s['variant_tasks']} novel={s['novel_tasks']} "
            f"总计={s['total_tasks']}"
        )
        print(
            f"[配置] LLM={args.llm_model} adapt={args.adapt_model or '(同模型)'} "
            f"emb={args.emb_model} 阈值={threshold}"
        )

        cmp = await run_real_comparison(
            benchmark,
            base_url=args.base_url,
            llm_model=args.llm_model,
            adapt_model=args.adapt_model,
            emb_model=args.emb_model,
            threshold=threshold,
        )

        print("\n" + render_comparison(cmp))

        if args.runs > 1:
            print("\n[多次运行] 继续重复以估算波动（共 %d 次）…" % args.runs)
            comparisons = [cmp]
            for i in range(2, args.runs + 1):
                print(f"\n=== 第 {i}/{args.runs} 次运行 ===")
                comparisons.append(await run_real_comparison(
                    benchmark,
                    base_url=args.base_url,
                    llm_model=args.llm_model,
                    adapt_model=args.adapt_model,
                    emb_model=args.emb_model,
                    threshold=threshold,
                ))
            print("\n" + _aggregate(comparisons))
            cmp = comparisons[-1]

        print(
            "\n[口径说明] 本地单模型（无 --adapt-model）下，复用主要省延迟、token 成本可能"
            "略增；传 --adapt-model（如 qwen2.5:0.5b）分层后成本节省才显著（对齐 APC 论文）。"
        )
        print(
            "[口径说明] 真实模型下「复用精度(骨架正确)」按手写 gold 判定会失真（真实模型"
            "生成的结构粒度与 gold 不同，如 7b 常把分析并入 researcher 得到 2 步、gold 为 "
            "3 步），仅作参考；请以「骨架保真率(适配忠实)」为准——它衡量适配后骨架是否"
            "忠实于命中的模板骨架。"
        )

        if args.md:
            from experiments.report import render_markdown
            from experiments.experience_eval import run_experience_loop
            exp = await run_experience_loop()
            with open(args.md, "w", encoding="utf-8") as f:
                f.write(render_markdown(cmp, exp))
            print(f"\nMarkdown 报告已写入: {args.md}")

    asyncio.run(_run())


if __name__ == "__main__":
    main()
