# YouMi Agent

**[中文](#中文) | English**

YouMi 是一个**能力库 sidecar**：把「工具目录检索、Skill 索引、计划复用、失败经验」
四类能力以 MCP stdio 服务的形式提供给 agent 宿主。宿主（agent loop、工具执行、
审批、沙箱、会话、GUI）已由 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
承担，YouMi 不再重复实现。

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![MCP](https://img.shields.io/badge/MCP-2024--11--05-purple)

English summary: YouMi is a Python MCP stdio sidecar that adds tool-catalog
retrieval, skill indexing, plan reuse and failure-experience memory to any
agent host. The host runtime lives in DeepSeek Harness; YouMi only keeps the
retrieval and distillation loop.

---

# 中文

## 定位

| 能力 | 解决的问题 | 工具前缀 |
| --- | --- | --- |
| 工具目录 | 工具太多时按语义 / tag / 风险等级检索，而不是全量塞上下文 | `youmi_tool_*` |
| Skill 索引 | 复用已有 SOP，而不是每次让模型重新生成 | `youmi_skill_*` |
| 计划记忆 | 相似任务直接复用可行计划（相似度阈值 0.85） | `youmi_plan_*` |
| 失败经验 | 踩过的坑沉淀为可检索条目，避免重复犯错 | `youmi_memory_*` |

YouMi 不执行工具、不调度 agent、不管理会话。它只在模型需要「回忆」或
「沉淀」时被调用；检索不到就返回空结果，主链路继续。

## 架构

```
┌─────────────────────────────────────────────┐
│            DeepSeek Harness (TypeScript)     │
│  agent loop · tools · approval · sandbox     │
│  session · compaction · web GUI              │
└───────────────┬─────────────────────────────┘
                │  dsh-mcp-client (stdio)
                ▼
┌─────────────────────────────────────────────┐
│         YouMi sidecar (Python, 本仓库)        │
│  ToolStore / ToolVault      工具目录           │
│  SkillStore                 Skill 索引        │
│  PlanMemory                 计划复用           │
│  GlobalMemory               失败经验           │
└───────────────┬─────────────────────────────┘
                │  SQLite (+ 可选 sqlite-vec)
                ▼
           .youmi/*.db
```

## 项目结构

```
YouMi_Agent/
├── youmi/
│   ├── core/           # ToolDefinition / ToolParameter / 版本规则（纯数据）
│   ├── mcp/            # MCP 协议、能力 Provider、工具目录、Skill 库、版本路由
│   │   ├── capabilities.py   # 10 个 youmi_* 工具
│   │   ├── stdio_server.py   # MCP stdio JSON-RPC 服务（入口）
│   │   ├── tool_store.py     # SQLite 工具目录（版本树 / 标签 / 血缘）
│   │   ├── vault.py          # HOT/WARM/COLD 上下文分层
│   │   ├── skill_store.py    # Skill（SOP）索引
│   │   └── version_router.py # skill→root / direct→head 版本解析
│   ├── plan/           # WorkflowPlan 数据模型 + PlanMemory
│   ├── knowledge/      # GlobalMemory + ToolExperienceExtractor
│   ├── llm/            # EmbeddingClient（OpenAI 兼容 /v1/embeddings）
│   └── _vec_utils.py   # sqlite-vec 与 Python 余弦的共用工具
├── dsh-plugins/
│   └── youmi-bridge/   # DSH Cordis 插件：注入 youmi_* 使用指引
├── scripts/
│   └── start-dsh.ps1   # 一键装运行时 + 编译插件 + 启动 DSH web
├── tests/              # 233 个用例，全部针对 sidecar 能力
└── docs/               # 迁移计划与设计文档
```

## 安装

```bash
git clone https://github.com/CatOlin-Zhang/YouMi_Agent.git
cd YouMi_Agent

# 运行时只依赖 pydantic + httpx
pip install -e .

# 可选：sqlite-vec 加速向量检索
pip install -e ".[vec]"

# 可选：开发依赖
pip install -e ".[dev]"
```

## 运行 sidecar

作为独立 MCP 服务（stdin/stdout 协议，stderr 日志）：

```bash
python -m youmi.mcp.stdio_server
# 或安装后
youmi-sidecar
```

手动握手验证：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python -m youmi.mcp.stdio_server
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python -m youmi.mcp.stdio_server
```

## 挂载到 DSH

### 一键启动（推荐）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-dsh.ps1
```

脚本做四件事：把 `@deepseek-ai/dsh` 装到 `.dsh-runtime`（忽略入库）、按本机路径
生成 `cordis.local.patch.yml`、编译 `youmi-bridge`、启动 web profile。
首次约 3 分钟，之后直接秒级启动。浏览器会打开带一次性 token 的地址。

跑单轮任务（不开 UI）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-dsh.ps1 -HeadlessTask "读取 README 并总结"
```

### 手工挂载

把 `dsh-plugins/youmi-bridge/cordis.patch.yml` 合进 profile（或写到
`$DSH_HOME/cordis.patch.yml` 自动生效），替换 `<path-to-YouMi_Agent>` 并把插件的
`name` 指向**编译产物文件** `lib/index.js`（ESM 不支持目录 import）：

```yaml
    - id: youmi-bridge
      name: 'C:\path\to\YouMi_Agent\dsh-plugins\youmi-bridge\lib\index.js'
```

模型看到的工具名形如 `mcp__youmi__youmi_tool_search`。
三个加载约束（目录 import、peer 依赖、构建期类型覆盖）见
[`dsh-plugins/youmi-bridge/README.md`](dsh-plugins/youmi-bridge/README.md#三个必须知道的加载约束)。

## 工具清单

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `youmi_tool_search` | `query`、`top_k=5`、`risk_ceiling=""` | 语义 + tag + 风险等级检索工具目录 |
| `youmi_tool_get` | `name`、`version=""` | 读取工具定义与版本元数据 |
| `youmi_tool_resolve` | `name`、`call_path="direct"` | 按调用来源解析应使用的版本 |
| `youmi_skill_index` | `query`、`top_k=5` | 检索 Skill（SOP） |
| `youmi_skill_list` | `cursor=""`、`limit=20` | 分页列出 Skill |
| `youmi_skill_get` | `skill_id` | 读取 Skill 全文（`name` 或 `name@version`） |
| `youmi_plan_search` | `task`、`top_k=3` | 检索相似任务的可行计划 |
| `youmi_plan_save` | `task`、`plan_json`、`outcome="success"` | 存回 `WorkflowPlan` |
| `youmi_memory_search` | `query`、`top_k=5`、`tool_name=""` | 检索失败经验 |
| `youmi_memory_add` | `task`、`failure`、`fix`、`tags=""`、`tool_name=""` | 记录失败与修复方式 |

所有工具 fail-open：组件未初始化或检索异常时返回 `{"error": "..."}`，
不会中断 agent 主链路。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `YOUMI_DATA_DIR` | `.youmi` | 数据根目录 |
| `YOUMI_TOOLS_DB` | `<DATA_DIR>/tools.db` | 工具目录库 |
| `YOUMI_SKILLS_DB` | `<DATA_DIR>/skills.db` | Skill 库 |
| `YOUMI_PLANS_DB` | `<DATA_DIR>/plans.db` | 计划库 |
| `YOUMI_MEMORY_DB` | `<DATA_DIR>/knowledge.db` | 经验库 |
| `YOUMI_EMBEDDING_BASE_URL` | 空 | OpenAI 兼容 `/v1/embeddings`；空 = 关键词降级 |
| `YOUMI_EMBEDDING_API_KEY` | 空 | 本地模型可留空 |
| `YOUMI_EMBEDDING_MODEL` | `nomic-embed-text` | 向量模型名 |
| `YOUMI_EMBEDDING_DIM` | 自动探测 | 固定维度时填写 |
| `YOUMI_LOG_LEVEL` | `INFO` | 日志写 stderr，不污染协议流 |

本地 embedding 服务会绕开 HTTP 代理（`localhost` / `127.*` 直连）。

## 测试

```bash
pytest -q      # 233 passed
```

## 与旧版的关系

`dsh-shell` 分支把原多 Agent 框架中**由宿主承担**的部分交给了 DSH，因此删除了：

| 删除内容 | 现由谁负责 |
| --- | --- |
| `youmi/agents`、`core/agent.py`、`prompt.py`、`hooks.py`、`plugin.py` | DSH agent-loop / system-prompt |
| `youmi/bus` | DSH 会话与子代理机制 |
| `youmi/gateway`、`gui` | DSH web GUI 与 API |
| `youmi/security`（认证 / 沙箱） | DSH sandbox / approval |
| `youmi/observability`、`core/resilience.py` | DSH telemetry / retry policy |
| `youmi/memory`、`coordinator/post_task.py` | DSH 会话压缩与历史 |
| `youmi/tools`（9 个内置工具） | DSH 原生 fs / shell / web 工具 |
| `youmi/eval`、`experiments` | 迁移到 DSH 的 eval / benchmark |

保留并重构的部分：MCP 工具目录、Skill 索引、计划记忆、全局经验，
以及它们依赖的 `ToolDefinition` / 版本规则 / `EmbeddingClient`。
`coordinator/plan.py` 中的 `WorkflowPlan` 数据模型抽到 `youmi/plan/models.py`，
`coordinator/post_task.py` 中的 `ToolExperience` 并入 `youmi/knowledge/models.py`，
使这四个能力不再依赖已删除的宿主代码。

迁移计划与设计记录见 [`docs/dsh-shell-migration-plan.md`](docs/dsh-shell-migration-plan.md)。

## 技术选型

| 组件 | 技术 |
| --- | --- |
| 语言 | Python 3.10+，asyncio |
| 协议 | MCP 2024-11-05，stdio JSON-RPC |
| 数据校验 | pydantic >= 2.0 |
| HTTP 客户端 | httpx >= 0.27 |
| 持久化 | SQLite（可选 sqlite-vec 向量索引） |
| 测试 | pytest + pytest-asyncio |

## 许可证

MIT
