# experiments/ — YouMi Agent 创新点验证与对比

本目录为 YouMi Agent 框架的两个核心创新点提供**可运行、可量化、可复现**的评测体系，
用确定性 mock LLM / embedding 替代真实模型，离线即可跑通消融实验、产出对比报告，
并配合单元测试锁定机制正确性。

> 目标不是替代真实 benchmark，而是给「计划复用」和「经验沉淀闭环」两个方向补上
> **验证架构 + 核心优化的量化对比**，作为后续接入真实模型 / 真实任务集的前置地基。

---

## 1. 验证的两个创新点

| 创新点 | 核心机制 | 对照的基线 | 关键结论 |
|--------|----------|-----------|----------|
| **计划复用** | Plan-then-Execute + `PlanMemory`（语义检索命中 → 只改 step.task） | 无记忆、逐任务全量规划 | 昂贵全量规划调用 -54.5%，规划成本 -53.9%，延迟 -47.3%，且骨架精度 100% |
| **经验沉淀闭环** | `GlobalMemory` + `FeedbackCollector` + `ToolGuardian`（负反馈累计到阈值 → 诊断修复 → mark_resolved） | 无修复的持续失败 | 阈值触发后工具成功率 0% → 100% |

---

## 2. 目录结构

```
experiments/
├── benchmark.py          # 带真值标签的合成任务基准（5 族 + 2 novel）
├── fake_llm.py           # 确定性 mock LLM / embedding + 成本/延迟记账
├── metrics.py            # 结果数据结构（PlanReuseResult / Comparison / ExperienceResult）
├── plan_reuse_eval.py    # 实验一：计划复用消融（baseline vs reuse）
├── experience_eval.py    # 实验二：经验沉淀闭环
├── report.py             # 控制台 / Markdown 报告渲染
├── run_all.py            # 一键入口（--md 输出 Markdown 报告）
├── REPORT.md             # 最近一次运行生成的报告
└── tests/                # 22 个单元测试，锁定机制正确性
```

---

## 3. 运行方式

> 依赖环境：真实 `youmi.llm.client` 依赖 `httpx`。当前默认 Python 3.13 环境未安装，
> 需使用已装依赖的 Python 3.11：

```bash
PY="C:/Users/阿根达斯/AppData/Local/Programs/Python/Python311/python.exe"

# 跑全部实验（控制台报告 + 写 Markdown）
"$PY" -m experiments.run_all --md experiments/REPORT.md

# 跑单元测试
"$PY" -m pytest experiments/tests/ -v
```

运行结果 100% 可复现（mock LLM / embedding 均为确定性实现，`test_deterministic_rerun` 锁定）。

---

## 4. 实验一：计划复用消融

### 设计

`Benchmark` 提供 **5 个任务族**（行业报告 / 数据分析 / 代码开发 / 网页爬取 / 翻译），
每族含 1 个 seed（冷启动）与 3 个 variants（语义等价、实体不同的改写），另加 2 个
novel 陌生任务。每个族给出 **gold plan**（正确的 `step_id + role + depends_on` 拓扑）。

流程：

1. **warmup**：seed 任务首次规划后写入 `PlanMemory`；
2. **test**：variants 应命中复用（`source == memory`），novel 应 miss；
3. 骨架正确性用 `skeleton_key` 判定（忽略 task 文本，只比角色 + 依赖拓扑）。

### 成本模型（对齐 Agentic Plan Caching）

- **全量规划 (full)**：昂贵推理模型，`price_per_token = 1.0`；
- **模板适配 (adapt)**：廉价小模型，`price_per_token = 0.05`（约 1/20）。

一次调用成本 = (输入 + 输出 token) × 模型单价。这正确体现了「复用省钱」：
适配调用虽因重传完整骨架导致**原始 token 总量上升**，但用的是廉价模型，**价格加权成本
仍大幅下降**。这正是本实验与 Agentic Plan Caching「昂贵大模型 vs 廉价小模型」对齐的核心论证。

### 指标

| 指标 | 含义 |
|------|------|
| `hit_rate` | 复用发生比例（命中 / 命中+未命中） |
| `reuse_precision` | 命中的骨架是否正确 |
| `reuse_recall` | 同族改写任务中被命中的比例 |
| `novel_precision` | 陌生任务正确 miss 比例（越低越易误复用） |
| `plan_validity` | Plan 合法率 |
| `cost_savings` | 价格加权成本节省比例（**核心省钱指标**） |
| `full_gen_call_savings` | 昂贵全量规划调用次数节省比例 |
| `latency_savings` | 延迟节省比例 |

---

## 5. 实验二：经验沉淀闭环

用确定性模拟驱动真实组件（`GlobalMemory` / `FeedbackCollector` / `ToolGuardian`）：

```
工具调用失败 → 沉淀负反馈 → 累计到阈值(3) → ToolGuardian 诊断修复
            → mark_resolved + BUG_FIX + best_practices → 后续任务成功
```

产出学习曲线 `[✗✗✗✓✓✓✓✓]`（前段故障期、后段全成功），量化
`time-to-fix`、修复前后成功率、已解决经验数、沉淀最佳实践数。

---

## 6. 与前沿文献的差异化

| 相关工作 | 复用对象 | 本项目的差异点 |
|----------|----------|----------------|
| Agentic Plan Caching (NeurIPS 2025) | 单 Agent 动作序列 | **多 Agent DAG 骨架**（角色 + 依赖拓扑），正确性判定维度不同 |
| AgentReuse / LEGOMem | Agent 记忆复用 | 面向 **Plan-then-Execute 的规划层**，复用的是 WorkflowPlan 骨架 |
| ExpeL / Reflexion / Voyager | 经验 / 反思 / 技能库 | 结合 **GlobalMemory + 工具级闭环修复**，强调负反馈 → 阈值 → mark_resolved 的闭环 |

> 这些差异化目前仍是**机制层**的（合成基准 + mock 模型）。要落到论文，仍需：
> 接入真实 LLM / embedding、真实任务集（如 SWE-bench 类、ToolBench），并补齐与
> 上述基线的同口径对比实验。

---

## 7. 真实模型接入（Ollama 接口）

规划层契约缺口已补齐：`LLMClient.complete()` 现已实现（见 `youmi/llm/client.py`），
`WorkflowPlanner` 可直接接真实模型。Ollama 通过 OpenAI 兼容 `/v1` 端点提供，
`experiments/real_llm.py` 集中封装了接入约定：

```python
from experiments.real_llm import make_ollama_llm, make_ollama_embedding, check_ollama

BASE = "http://localhost:11434/v1"

# 1) 先探活（模型未下载会返回友好提示，不抛异常）
status = await check_ollama(BASE, model="qwen2.5:3b")
print(status["hint"])   # 未就绪 → 提示 `ollama pull qwen2.5:3b`

# 2) 就绪后构造客户端，交给 run_plan_reuse_comparison 跑真实消融
llm = make_metered_ollama_llm(base_url=BASE, model="qwen2.5:7b-instruct-q3_K_M")
emb = make_ollama_embedding(base_url=BASE, model="bge-m3")

from experiments.plan_reuse_eval import run_plan_reuse_comparison
cmp = await run_plan_reuse_comparison(
    Benchmark(),
    llm_client=llm, embedding_client=emb,
    embedding_dim=1024,          # bge-m3 维度（nomic-embed-text 为 768）
    similarity_threshold=0.66,   # 真实 embedding 按模型定标阈值
)
```

要点：

- **`MeteredLLMClient`**：把真实客户端包装出与 `FakeLLMClient` 一致的记账接口
  （full/adapt 调用数、token、延迟、成本），让真实实验复用同一套 `metrics`。
- **成本语义**：本地单模型场景下 full/adapt 同价，复用收益主要体现在**延迟**，
  token 成本可能因重传骨架略增——这正是 APC 采用「大模型规划 + 小模型适配」
  分层的原因。需要省钱时，把 `make_metered_ollama_llm` 的 `cost_full` / `cost_adapt`
  设为不同单价即可表达分层定价。

### 一键真实评测入口

`experiments/run_real_eval.py` 是面向本地 Ollama 的完整入口（默认模型
`qwen2.5:7b-instruct-q3_K_M` + `bge-m3`）：

```bash
PY="C:/Users/阿根达斯/AppData/Local/Programs/Python/Python311/python.exe"

# 最小冒烟（1 个任务族 + 2 novel，约 3 分钟）
"$PY" -m experiments.run_real_eval --families 1

# 全量（5 族，约 10 分钟起）
"$PY" -m experiments.run_real_eval

# 分层：7b 规划 + 3b 适配（对应 APC「大模型规划 + 小模型适配」，成本才显著下降）
"$PY" -m experiments.run_real_eval --families 2 --adapt-model qwen2.5:3b

# 多次运行取均值 ± 标准差（抗真实模型推理波动）
"$PY" -m experiments.run_real_eval --families 2 --adapt-model qwen2.5:3b --runs 3
```

前置条件：

1. 启动 Ollama（`ollama serve`）；
2. `ollama pull qwen2.5:7b-instruct-q3_K_M`（规划大模型）；
3. `ollama pull bge-m3`（向量，生成模型无法走 `/v1/embeddings`，必须用专门
   embedding 模型；中文语义区分度实测优于 `nomic-embed-text`，见下）。

参数：`--families N` 子集、`--adapt-model` 分层、`--threshold` 命中阈值（默认按 emb
模型自适应：bge-m3=0.66 / nomic-embed-text=0.60，可显式覆盖）、`--runs N` 多次运行、
`--md` 额外输出 Markdown 报告。成本按「token × 参数量」估算（本地无计费）。

### Embedding 模型选择（实测区分度）

用 `compare_embeddings.py` 在本基准语料上实测「同族 vs 跨族」余弦相似度：

| embedding 模型 | 同族相似度 | 跨族相似度 | 区分度 gap | 阈值切分 |
|----------------|-----------|-----------|-----------|----------|
| nomic-embed-text | 0.66~1.00 (均值 0.88) | 0.57~0.91 (均值 0.67) | +0.21 | 重叠严重，0.60 只能偏保守 |
| **bge-m3** | 0.67~0.92 (均值 0.82) | 0.39~0.66 (均值 0.53) | **+0.29** | **0.66 附近干净切分** |

bge-m3 跨族相似度显著更低，gap 更大，是中文 PlanMemory 检索的更优选择，故设为默认。

### 真实模型的正确性口径（骨架保真率）

真实 LLM 生成的结构粒度与手写 gold 天然不同（如 7b 常把「分析」并入 researcher，
得到 researcher→writer 两步，而 gold 是 researcher→analyst→writer 三步），因此
gold 判定的「复用精度」在真实模型下失真。真实评测应以**骨架保真率**为准——它衡量
「适配后骨架是否忠实于命中的模板骨架」（`planner` 在复用路径的 metadata 里写入
`template_skeleton`，评测层对比适配前后骨架）。

### 适配小模型的选择（实测，含结构保留修复）

**关键发现**：小模型适配的瓶颈不是「写不好 task 文本」，而是「重生成结构时幻觉依赖」。
`planner._apply_plan_template` 已改为**骨架确定性保留**——step_id/role/depends_on 一律从
模板复制，适配模型只改写 task 文本。修复后 0.5b 从「fallback 风暴、延迟恶化 53%」翻转为
「0 fallback、延迟节省 26%」。

| 配置 | 命中率 | 召回 | 保真率 | 昂贵调用节省 | 成本节省 | 延迟节省 | 墙钟(2族) |
|------|--------|------|--------|-------------|---------|---------|-----------|
| 7b 规划 + 7b 适配（单模型） | — | — | — | — | 负 | 负 | ~5min |
| 7b 规划 + 0.5b 适配（修复前） | 37.5% | 50% | 100%* | +30% | +25% | -53.4% | ~5min |
| 7b 规划 + 0.5b 适配（修复后） | **75%** | **100%** | 100% | +60% | **+56%** | **+25.7%** | 4m24s |
| 7b 规划 + 3b 适配 | 75% | 100% | 100% | +60% | +23.7% | -8.4% | 4m56s |
| 3b 规划 + 0.5b 适配（双小模型） | 75% | 100% | 100% | +60% | +48.5% | -10.8% | **2m20s** |

结论：

- **修复后 0.5b 适配全面优于 3b 适配**（成本 +56% vs +23.7%，延迟 +25.7% vs -8.4%），
  且命中率/召回/保真率完全相同。之前「0.5b 质量崩」是适配路径让模型重生成结构导致的
  幻觉依赖，非 0.5b 本身能力不足。
- **绝对速度**选「3b 规划 + 0.5b 适配」（墙钟最快 2m20s）；**相对节省**选「7b 规划 +
  0.5b 适配」（延迟 +25.7%、成本 +56%）。3b 基线本身已快，故双小模型的「相对延迟节省」
  为负，但绝对墙钟最短。

> 「大模型规划 + 小模型适配」的实证贡献点：适配小模型的甜点区不在于参数量大小，而在于
> **是否对骨架做确定性保留**——一旦结构由模板锁定，0.5b 即可在成本与延迟上双双反超 3b。

---

## 8. 与论文成果的对比（HTML 图表）

`experiments/paper_comparison.py` 承载 7 个方案的画像（YouMi + APC / AgentReuse /
LEGOMem / ExpeL / Reflexion / Voyager），`render_html.py` 渲染为**自包含** HTML：

```bash
"$PY" -m experiments.run_paper_comparison --html experiments/comparison.html
```

生成的 `experiments/comparison.html` 本地双击即可查看，含三部分：

1. **定位散点图**：复用抽象粒度（动作序列 → 多 Agent 骨架）× 学习/自进化闭环，
   直观呈现 YouMi 的相对位置；
2. **定量收益条形图**：成本 / 延迟节省（相对不复用），已标注「合成 vs 真实」口径差异；
3. **机制对比矩阵表**：8 个定性维度（复用对象、架构、检索、学习信号、自进化、
   成本优化、验证数据、开源）。

> ⚠️ 定量图中 YouMi 为合成基准 + mock 估计值，APC / AgentReuse 为真实 benchmark，
> 三者口径不同、**不可直接比较**，仅作量级参考（图中已醒目标注）。

---

## 9. 下一步

- [x] `LLMClient` 补齐 `complete()` 契约
- [x] 预留 Ollama 真实模型接口（`real_llm.py`）
- [x] 接入真实模型（`qwen2.5:7b-instruct-q3_K_M` + `bge-m3`），跑通真实消融
- [x] 支持「大模型规划 + 小模型适配」分层（`--adapt-model`）
- [x] 补「骨架保真率」指标（真实模型下复用质量的正确口径）
- [x] 用 `bge-m3` 替换 `nomic-embed-text`（区分度 gap +0.29 vs +0.21，阈值 0.66 干净切分）
- [x] 多次运行取均值 ± 标准差（`--runs N`，抗真实模型推理波动）
- [ ] 接入真实任务集，与 APC / AgentReuse 等基线同口径对比
