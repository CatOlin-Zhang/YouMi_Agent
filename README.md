# YouMi Agent

**YouMi Agent** 是一个基于 Python 的轻量级多 Agent 协作框架，由 `MasterAgent` 编排层接收用户任务，自动创建子 Agent，通过自研 MCP 工具层统一管理工具，为每个 Agent 独立维护记忆系统，并通过消息总线实现 Agent 间通信，从而实现灵活、可扩展的智能任务协作。

## 核心特性

- **Plan-then-Execute 编排** — LLM 先生成结构化 `WorkflowPlan`，由确定性的 `WorkflowExecutor` 按 DAG 拓扑序调度执行，不确定性限制在规划层
- **Plan 记忆复用** — 相似任务的执行方案持久化到独立 SQLite 存储（`PlanMemory`），命中时直接复用骨架，减少重复规划开销
- **统一 MCP 工具层** — 自研 JSON-RPC 协议，所有工具通过 `MCPServer` 统一注册，Agent 经 `ToolBridge` 调用，支持动态加载/卸载和语义向量搜索
- **独立记忆管理** — 每个 Agent 拥有独立的 `MemoryManager`，支持可插拔策略（Full / Summary / LSTM）和后端（SQLite / 文件），超 token 时自动压缩
- **三级工具审批** — `ApprovalManager` 实现 AUTO / MANUAL / MASTER 三级审批，最小权限原则，生成完整审计日志
- **全局知识沉淀** — `PostTaskPipeline` 在任务结束后自动提取工具经验，写入 `GlobalMemory`，供 `ToolGuardianAgent` 诊断修复
- **进程隔离执行** — `SubProcessAgentRunner` 基于 `asyncio` 子进程，隔离崩溃传播
- **零侵入可观测** — Hook 系统 + GUI WebSocket 实时推送，无需修改引擎代码即可观测 Agent 行为
- **全栈异步** — 基于 `asyncio`，全链路非阻塞 I/O，优雅降级（向量化 / LLM 失败时自动回退规则路径）

## 系统架构

```
┌──────────────────────────────────────────────────┐
│              用户接口层                             │
│        Web GUI (aiohttp) / Python API              │
└───────────────────┬──────────────────────────────┘
                    │ 用户任务
                    ▼
┌──────────────────────────────────────────────────┐
│              编排层 (coordinator/)                 │
│  MasterAgent  WorkflowPlanner  WorkflowPlan       │
│  PlanMemory   ToolGuardianAgent  PostTaskPipeline │
└───────────────────┬──────────────────────────────┘
                    │ 创建 & 调度 SubAgent
                    ▼
┌──────────────────────────────────────────────────┐
│           Agent 运行时 (core/)                     │
│  Agent + ReAct循环  LLMClient  ToolBridge         │
│  MemoryManager  HookRegistry  PromptAssembler     │
└────┬──────────────┬─────────────────┬────────────┘
     │              │                 │
┌────▼────┐  ┌──────▼──────┐  ┌──────▼────────┐
│  MCP 层  │  │   记忆层     │  │  全局知识层    │
│ Vault   │  │ Strategy    │  │ GlobalMemory  │
│ Store   │  │ Backend     │  │ KnowledgeEntry│
│ Approval│  │ Compactor   │  │ ExperienceExt │
└─────────┘  └─────────────┘  └───────────────┘
```

## 项目结构

```
YouMi_Agent/
├── youmi/                  # 核心框架包
│   ├── core/               # Agent 运行时（ReAct 循环、Hook、Plugin、Prompt 组装）
│   ├── coordinator/        # 编排层（MasterAgent、WorkflowPlan/Executor/Planner、PlanMemory）
│   ├── mcp/                # MCP 工具协议层（Server/Client/Bridge/Vault/Store/Approval）
│   ├── memory/             # 记忆系统（Strategy、Backend、ContextCompactor）
│   ├── bus/                # 消息总线（InProcessBroker、BusServer/Client）
│   ├── tools/              # 内置工具提供者（文件、Shell、Web、数据处理等 9 个工具）
│   ├── llm/                # LLM 客户端（OpenAI 兼容 API / Ollama）
│   └── agents/             # Agent 角色 YAML 配置（master、tool_guardian）
├── gui/                    # Web GUI（aiohttp + WebSocket + REST API）
│   ├── engine/             # 引擎桥接层（EngineBridge、GUIHooks、MCPService）
│   ├── static/             # 前端资源（HTML / CSS / JS，三栏聊天布局）
│   └── server.py           # Web 服务器入口
├── tests/                  # 测试套件（480+ 测试用例）
└── docs/                   # 文档（需求、架构设计、模块详解）
```

## 快速开始

### 环境要求

- Python >= 3.10
- （可选）支持 OpenAI 兼容接口的 LLM 服务，或本地 Ollama

### 安装

```bash
# 克隆仓库
git clone https://github.com/CatOlin-Zhang/YouMi_Agent.git
cd YouMi_Agent

# 安装核心依赖
pip install -e .

# 安装 GUI 依赖（可选）
pip install -e ".[web]"

# 安装向量检索支持（可选，用于工具语义搜索和 PlanMemory 向量复用）
pip install -e ".[vec]"

# 安装开发依赖
pip install -e ".[dev]"
```

### 启动 Web GUI

```bash
# Windows
gui\start_gui.bat

# 或直接启动
python -m gui
```

启动后访问 `http://localhost:8080`。

### Python API 快速示例

```python
import asyncio
from youmi.coordinator.master import MasterAgent
from youmi.llm.client import LLMClient

async def main():
    llm = LLMClient(base_url="http://localhost:11434/v1", api_key="ollama")
    master = MasterAgent(llm_client=llm)
    await master.initialize()

    # 提交任务，自动走 Plan-then-Execute 流程
    result = await master.run("帮我分析 data.csv 并生成摘要报告")
    print(result)

asyncio.run(main())
```

## 主要模块说明

### 编排层 (`youmi/coordinator/`)

| 文件 | 职责 |
|------|------|
| `master.py` | `MasterAgent` 顶层协调器，子 Agent 工厂，Plan-then-Execute 主流程 |
| `planner.py` | `WorkflowPlanner`，调用 LLM 生成结构化 `WorkflowPlan`，支持 PlanMemory fast path |
| `plan.py` | `WorkflowPlan` / `WorkflowExecutor`，DAG 拓扑调度，步骤级重试与超时兜底 |
| `plan_memory.py` | `PlanMemory`，独立 SQLite 双表，向量余弦相似度检索 + 关键词降级 |
| `tool_guardian.py` | `ToolGuardianAgent`，工具问题诊断与修复 |
| `post_task.py` | `PostTaskPipeline`，4 阶段后台流水线（工具统计、摘要、Guardian、GlobalMemory）|
| `handoff.py` | `HandoffProtocol`，Agent 间任务委派协议 |
| `subprocess_agent.py` | `SubProcessAgentRunner`，进程隔离执行 |
| `tool_approval.py` | `ToolApprovalMixin`，三级审批集成 |

### MCP 工具层 (`youmi/mcp/`)

所有工具通过 `MCPServer` 以 JSON-RPC 2.0 协议统一注册，Agent 经 `MCPClient` → `ToolBridge` → `AgentToolContext` 调用。`ToolStore` 使用 SQLite + `sqlite-vec` 持久化（6 张表：tools、vec_tools、tool_changelogs、tool_aliases、tool_tags、tool_dependencies）。

### 记忆系统 (`youmi/memory/`)

| 策略 | 说明 |
|------|------|
| `FullMemoryStrategy` | 保留全部历史消息 |
| `SummaryMemoryStrategy` | 超限时调用 LLM 压缩旧消息为摘要 |
| `LSTMMemoryStrategy` | 双通道：近期消息 + 重要事件长期保留 |

后端支持 `SQLiteBackend`（持久化）和 `FileBackend`（JSON 文件）。

### 内置工具 (`youmi/tools/`)

框架内置 9 个标准工具：`file_read`、`file_write`、`file_list`、`shell_exec`、`web_fetch`、`data_parse`、`data_transform` 等，并提供 `search_new_tools` 兜底工具用于动态发现新工具。

## 技术选型

| 组件 | 技术 |
|------|------|
| 开发语言 | Python 3.10+，全栈 asyncio |
| 数据验证 | pydantic >= 2.0 |
| LLM 接口 | OpenAI 兼容 API / Ollama |
| 工具与知识持久化 | SQLite + sqlite-vec（向量索引）|
| 消息总线 | asyncio.Queue（进程内）+ WebSocket（跨进程）|
| Web GUI | aiohttp >= 3.9 + 原生 HTML/CSS/JS |
| 配置管理 | YAML |
| 测试框架 | pytest + pytest-asyncio |

## 运行测试

```bash
# 运行全部测试
pytest tests/

# 排除需要外部服务的测试
pytest tests/ --ignore=tests/test_ollama_integration.py --ignore=tests/test_ollama_multi_agent.py
```

## 文档

详细文档位于 `docs/` 目录：

- [`docs/requirements.md`](docs/requirements.md) — 功能需求与实现状态
- [`docs/technical_design.md`](docs/technical_design.md) — 完整技术设计
- [`docs/structure.md`](docs/structure.md) — 模块结构与协作架构
- [`docs/details/Master_Introduction.md`](docs/details/Master_Introduction.md) — MasterAgent 编排层详解
- [`docs/details/MCP_Introduction.md`](docs/details/MCP_Introduction.md) — MCP 工具层详解
- [`docs/details/Message_Introduction.md`](docs/details/Message_Introduction.md) — 消息总线详解
- [`docs/details/AgentFactory_Introduction.md`](docs/details/AgentFactory_Introduction.md) — Agent 工厂详解

## 里程碑

| 阶段 | 核心交付 | 状态 |
|------|---------|------|
| P1 — 消息总线 | WorkflowMessage + InProcessBroker + BusServer/Client | ✅ |
| P2 — 内置工具 | 9 个内置工具 + BuiltinToolProvider | ✅ |
| P3 — 编排层 | MasterAgent + WorkflowPlan + ToolGuardian + 三级审批 + 进程隔离 | ✅ |
| P4 — 工具向量化 | ToolVault + ToolStore + AgentToolContext + ApprovalManager | ✅ |
| P5 — 记忆闭环 | GlobalMemory + PostTaskPipeline + ToolExperienceExtractor | ✅ |
| P6 — Plan 编排改造 | WorkflowPlanner + PlanMemory + 步骤重试/超时兜底 | ✅ |
| P7 — 层级 Sub-Master | Sub-Master 嵌套编排 | 规划中 |

## License

本项目暂未设置 License，请联系作者获取使用许可。
