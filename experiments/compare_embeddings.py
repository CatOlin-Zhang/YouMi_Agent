"""
Embedding 模型中文语义区分度对比 (compare_embeddings)

对比 nomic-embed-text（768 维）与 bge-m3（1024 维）在本基准任务语料上的
「同族 vs 跨族」余弦相似度分布，回答哪个模型更适合做 PlanMemory 语义检索：

- 同族相似度：seed 与它自己的 variants（应命中复用）的相似度，越高越好；
- 跨族相似度：seed 与其他族 seed（不应误命中）的相似度，越低越好；
- 区分度 gap = mean(同族) - mean(跨族)，gap 越大说明检索区分度越强。

用法::

    python -m experiments.compare_embeddings

只读探活 + 算相似度，不跑 LLM，几秒内完成。
"""

from __future__ import annotations

import asyncio
import statistics

from experiments.benchmark import Benchmark
from experiments.real_llm import make_ollama_embedding

BASE_URL = "http://localhost:11434/v1"
MODELS = ["nomic-embed-text", "bge-m3"]


def _report(name: str, same: list[float], cross: list[float]) -> None:
    def stats(vals: list[float]) -> str:
        return (
            f"mean={statistics.mean(vals):.3f} "
            f"min={min(vals):.3f} max={max(vals):.3f}"
        )

    gap = statistics.mean(same) - statistics.mean(cross)
    print(f"\n[{name}]")
    print(f"  同族相似度  {stats(same)}")
    print(f"  跨族相似度  {stats(cross)}")
    print(f"  区分度 gap   {gap:+.3f}  {'★ 较好' if gap >= 0.15 else '△ 一般' if gap >= 0.10 else '✗ 偏弱'}")


async def main() -> None:
    b = Benchmark(n_families=5)
    seeds = b.warmup_tasks()  # 5 个族的 seed
    variants = b.variant_tasks()  # 5 族 × 3 = 15 个改写

    # seed -> 同族 variants 索引
    fam_of = {f.seed: f.family_id for f in b.families}
    var_of = {v: b.family_of(v) for v in variants}

    for model in MODELS:
        client = make_ollama_embedding(base_url=BASE_URL, model=model)
        # 一次性编码所有文本（seeds + variants）
        all_texts = seeds + variants
        vecs = await client.embed(all_texts)
        await client.close()

        seed_vecs = vecs[: len(seeds)]
        var_vecs = vecs[len(seeds):]

        same: list[float] = []
        cross: list[float] = []
        for i, seed in enumerate(seeds):
            sv = seed_vecs[i]
            for j, var in enumerate(variants):
                sim = client.cosine_similarity(sv, var_vecs[j])
                if var_of[var] == fam_of[seed]:
                    same.append(sim)
                else:
                    cross.append(sim)

        _report(model, same, cross)


if __name__ == "__main__":
    asyncio.run(main())
