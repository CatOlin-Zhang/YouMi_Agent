# YouMi Agent

**[中文](#中文) | English**

A lightweight, Python-based multi-agent collaboration framework. A `MasterAgent` orchestration layer receives user tasks, automatically spawns sub-agents, manages tools through a custom MCP (Model Context Protocol) tool layer, maintains independent memory for each agent, and enables inter-agent communication via a message bus — delivering flexible, extensible intelligent task collaboration.

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

---

## Key Features

- **Plan-then-Execute Orchestration** — LLM generates a structured `WorkflowPlan`, then a deterministic `WorkflowExecutor` schedules steps by DAG topology; non-determinism is confined to the planning layer
- **Plan Memory Reuse** — Execution plans for similar tasks are persisted in a dedicated SQLite store (`PlanMemory`, vectorized via sqlite-vec); cache hits skip redundant planning, with keyword fallback
- **Unified MCP Tool Layer** — Custom JSON-RPC 2.0 protocol; all tools register via `MCPServer`, agents invoke through `ToolBridge` (`MCPClient` → `ToolBridge` → `AgentToolContext`); supports HOT/WARM/COLD dynamic load/unload and semantic vector search (`ToolVault` + `ToolStore`)
- **Dynamic Tool Discovery** — `search_new_tools` meta-tool lets agents discover and authorize additional tools on demand
- **Coordinator Tools for MasterAgent** — 6 orchestration tools (`create_sub_agent`, `run_sub_agent`, `list_sub_agents`, `list_available_roles`, `approve_tool_request`, `deny_tool_request`) enable `MasterAgent` to create, run and manage sub-agents
- **Independent Memory Management** — Each agent owns a `MemoryManager` with pluggable strategies (Full / Summary / LSTM) and backends (SQLite / File); auto-compaction on token overflow
- **Three-Level Tool Approval** — `ApprovalManager` implements AUTO / MANUAL / MASTER approval tiers with least-privilege principle and full audit logging
- **Global Knowledge Distillation** — `PostTaskPipeline` automatically extracts tool experience after task completion, writes to `GlobalMemory`, and feeds `ToolGuardianAgent` for diagnosis and repair; human feedback is collected through `FeedbackCollector`
- **Process-Isolated Execution** — `SubProcessAgentRunner` based on `asyncio` subprocesses isolates crash propagation
- **Zero-Intrusion Observability** — Hook system + GUI WebSocket real-time push (including `WorkflowTracker` step tracking); observe agent behavior without modifying engine code
- **Fully Async** — Built on `asyncio` end-to-end; non-blocking I/O with graceful degradation (vector / LLM failures fall back to rule-based paths)

## Architecture

```
┌──────────────────────────────────────────────────┐
│                  User Interface Layer             │
│        Web GUI (aiohttp REST + WebSocket)         │
│        Python API                                 │
└───────────────────┬──────────────────────────────┘
                    │ User Task
                    ▼
┌──────────────────────────────────────────────────┐
│              Orchestration (coordinator/)          │
│  MasterAgent  WorkflowPlanner/Plan  PlanMemory    │
│  ToolGuardianAgent  PostTaskPipeline               │
│  HandoffProtocol  SubProcessAgentRunner            │
│  ToolApprovalMixin  FixStrategiesMixin             │
└───────────────────┬──────────────────────────────┘
                    │ Create & Schedule SubAgents
                    ▼
┌──────────────────────────────────────────────────┐
│             Agent Runtime (core/)                 │
│  Agent + ReAct Loop  LLMClient  MemoryManager     │
│  HookRegistry  PluginManager  PromptAssembler     │
│  ToolRegistry  ToolExecutionMixin  MCPIntegration │
└────┬──────────────┬─────────────────┬────────────┘
     │              │                 │
┌────▼────┐  ┌──────▼──────┐  ┌──────▼────────┐
│ MCP Layer│  │Memory Layer │  │Global Knowledge│
│ Vault   │  │ Strategy    │  │ GlobalMemory  │
│ Store   │  │ Backend     │  │ KnowledgeEntry│
│ Approval│  │ Compactor   │  │ ExperienceExt │
│ Bridge  │  │             │  │ Feedback      │
└─────────┘  └─────────────┘  └───────────────┘
```

## Project Structure

```
YouMi_Agent/
├── youmi/                  # Core framework package
│   ├── core/               # Agent runtime: Agent base + ReAct loop, Hooks, Plugin system,
│   │                       #   ToolRegistry, Prompt assembly, ToolExecution & MCP Integration mixins
│   ├── coordinator/        # Orchestration: MasterAgent, WorkflowPlan/Executor/Planner, PlanMemory,
│   │                       #   ToolGuardianAgent, PostTaskPipeline, HandoffProtocol,
│   │                       #   SubProcessAgentRunner, ToolApprovalMixin, FixStrategiesMixin
│   ├── mcp/                # MCP protocol: Server / Client / Bridge / Vault / Store / Approval / Provider
│   ├── memory/             # Memory system: Strategy (full/summary/lstm), Backend (sqlite/file), Compactor
│   ├── knowledge/          # Global knowledge: GlobalMemory, ToolExperienceExtractor, FeedbackCollector
│   ├── bus/                # Message bus: WorkflowMessage, InProcessBroker, BusServer/Client
│   ├── tools/              # Built-in tool providers: file ops, shell, web, data ops + coordinator tools
│   ├── llm/                # LLM client (OpenAI-compatible API / Ollama) + Embedding client
│   ├── scheduler/          # Heartbeat scheduler
│   ├── agents/             # Agent role YAML configs (master, tool_guardian)
│   └── _vec_utils.py       # Shared sqlite-vec helpers (ToolStore / GlobalMemory / PlanMemory)
├── gui/                    # Web GUI (aiohttp REST + WebSocket)
│   ├── engine/             # Engine bridge: EngineBridge, GUIHooks, MCPService, WorkflowTracker
│   ├── hub/                # WebSocket hub: event definitions, connection management
│   ├── persistence/        # Session & message JSON persistence
│   ├── static/             # Frontend assets (HTML / CSS / JS, three-column chat layout)
│   ├── config.py           # Runtime config via environment variables
│   ├── server.py           # aiohttp server (REST + WebSocket, --mock mode)
│   └── mock_engine.py      # Mock engine for UI development
├── tests/                  # Test suite
└── docs/                   # Documentation (requirements, architecture, module guides)
```

## Quick Start

### Prerequisites

- Python >= 3.10
- (Optional) An OpenAI-compatible LLM service, or local [Ollama](https://ollama.com)

### Installation

```bash
# Clone the repository
git clone https://github.com/CatOlin-Zhang/YouMi_Agent.git
cd YouMi_Agent

# Install core dependencies
pip install -e .

# Install Web GUI dependencies (optional)
pip install -e ".[web]"

# Install vector search support (optional, for semantic tool search & PlanMemory vector reuse)
pip install -e ".[vec]"

# Install dev dependencies (optional)
pip install -e ".[dev]"
```

### Launch Web GUI

```bash
# Windows
gui\start_gui.bat

# Or directly
python -m gui

# Optional: run with a mock engine (no LLM needed)
python -m gui --mock
```

Then open `http://localhost:8766` in your browser.

Configuration via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `YOUMI_GUI_HOST` | `127.0.0.1` | Listen address |
| `YOUMI_GUI_PORT` | `8766` | HTTP listen port |
| `YOUMI_GUI_MASTER` | `master` | Master agent name (maps to `youmi/agents/<name>/config.yaml`) |
| `YOUMI_GUI_MCP` | `1` | Enable MCP tool layer |
| `YOUMI_GUI_BUS` | `1` | Enable in-process message bus |
| `YOUMI_GUI_VAULT` | `1` | Enable ToolVault + ToolStore (sqlite-vec) |
| `YOUMI_GUI_VAULT_DB` | *(empty)* | ToolStore database path |
| `YOUMI_GUI_EMBEDDING_URL` | `http://localhost:11434/v1` | Embedding service URL |
| `YOUMI_GUI_EMBEDDING_MODEL` | `nomic-embed-text` | Embedding model name |

### Python API Example

```python
import asyncio
from youmi.coordinator.master import MasterAgent

async def main():
    # Loads config from youmi/agents/master/config.yaml
    # (llm_config points to a local Ollama instance by default)
    master = MasterAgent.from_config_dir()
    await master.initialize()

    # Submit a task — automatically follows Plan-then-Execute flow
    result = await master.run("Analyze data.csv and generate a summary report")
    print(result)

asyncio.run(main())
```

For explicit configuration:

```python
import asyncio
from youmi.coordinator.master import MasterAgent
from youmi.core.models import AgentConfig
from youmi.core.types import LLMConfig, LLMProvider

async def main():
    config = AgentConfig(
        name="master",
        llm_config=LLMConfig(
            provider=LLMProvider.LOCAL,
            model="qwen2.5:0.5b",
            base_url="http://localhost:11434/v1",
            api_key="ollama",
        ),
    )
    master = MasterAgent(config)   # the LLM client is created from config.llm_config on initialize
    await master.initialize()
    result = await master.run("Summarize the key findings in report.pdf")
    print(result)

asyncio.run(main())
```

## Module Overview

### Orchestration Layer (`youmi/coordinator/`)

| File | Responsibility |
|------|----------------|
| `master.py` | `MasterAgent` — top-level coordinator, sub-agent factory, Plan-then-Execute main flow |
| `planner.py` | `WorkflowPlanner` — LLM-driven structured `WorkflowPlan` generation with PlanMemory fast path |
| `plan.py` | `WorkflowPlan` / `WorkflowExecutor` — DAG topology scheduling, per-step retry & timeout guard |
| `plan_memory.py` | `PlanMemory` — dedicated SQLite store with vector cosine similarity + keyword fallback |
| `tool_guardian.py` | `ToolGuardianAgent` — tool issue diagnosis & repair with global memory feedback loop |
| `post_task.py` | `PostTaskPipeline` — 4-stage background pipeline (tool stats, summary, Guardian, GlobalMemory) |
| `handoff.py` | `HandoffProtocol` — inter-agent task delegation |
| `subprocess_agent.py` | `SubProcessAgentRunner` — process-isolated agent execution |
| `tool_approval.py` | `ToolApprovalMixin` — three-level approval integration |
| `fix_strategies.py` | `FixStrategiesMixin` — recovery strategies invoked by the tool-guardian loop |

### MCP Tool Layer (`youmi/mcp/`)

All tools register via `MCPServer` using JSON-RPC 2.0. Agents invoke through `MCPClient` → `ToolBridge` → `AgentToolContext`. `ToolStore` persists to SQLite + `sqlite-vec` (6 tables: tools, vec_tools, tool_changelogs, tool_aliases, tool_tags, tool_dependencies); `ToolVault` keeps the HOT/WARM/COLD inventory; `ApprovalManager` enforces the three approval tiers.

### Memory System (`youmi/memory/`)

| Strategy | Description |
|----------|-------------|
| `FullMemoryStrategy` | Retains all historical messages |
| `SummaryMemoryStrategy` | Compresses old messages to summaries via LLM on overflow |
| `LSTMMemoryStrategy` | Dual-channel: recent messages + long-term important events |

Backends: `SQLiteBackend` (persistent) and `FileBackend` (JSON files). `ContextCompactor` triggers auto-compaction when the token budget is exceeded.

### Global Knowledge (`youmi/knowledge/`)

| Module | Responsibility |
|--------|----------------|
| `global_memory.py` | `GlobalMemory` — tool-experience knowledge base with vector search |
| `experience_extractor.py` | `ToolExperienceExtractor` — distills tool experience from task completion |
| `feedback.py` | `FeedbackCollector` — human positive/negative feedback writes back to global memory; threshold triggers Guardian repair |

### LLM Layer (`youmi/llm/`)

- `client.py` — `LLMClient`, async HTTP client for any OpenAI Chat Completions compatible endpoint (OpenAI / Anthropic proxies / Ollama / vLLM / llama.cpp)
- `embeddings.py` — `EmbeddingClient`, used for tool & memory vectorization

### Built-in Tools (`youmi/tools/`)

9 standard built-in tools (registered by `BuiltinToolProvider`):

| Tool | Purpose |
|------|---------|
| `file_search` | Find files by glob pattern in the workspace |
| `file_read` | Read a text file (with offset/limit) |
| `file_write` | Write / append text to a file |
| `list_directory` | List directory entries |
| `text_search` | Full-text search inside files |
| `shell_exec` | Run a shell command with timeout |
| `web_fetch` | Fetch a URL and return its text content |
| `get_datetime` | Current date / time |
| `json_tool` | JSON parse / transform / format |

In addition:

- **Coordinator tools** (`coordinator_ops.py`) are registered for `MasterAgent`: `create_sub_agent`, `run_sub_agent`, `list_sub_agents`, `list_available_roles`, `approve_tool_request`, `deny_tool_request`.
- `search_new_tools` is a **meta-tool** (not a builtin tool) provided via the MCP `ToolBridge` — agents call it to discover and authorize additional tools at runtime.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.10+, fully async (asyncio) |
| Validation | pydantic >= 2.0 |
| HTTP / Async Client | httpx >= 0.27 |
| LLM Interface | OpenAI-compatible API / Ollama (provider: openai / anthropic / local / custom) |
| Tool & Knowledge Persistence | SQLite + sqlite-vec (vector indexing) |
| Message Bus | asyncio.Queue (in-process) + WebSocket via `websockets` (cross-process) |
| Web GUI | aiohttp >= 3.9 + vanilla HTML/CSS/JS (REST + WebSocket) |
| Configuration | YAML + environment variables |
| Testing | pytest + pytest-asyncio |

## Testing

```bash
# Run all tests
pytest tests/

# Exclude tests requiring external LLM services
pytest tests/ --ignore=tests/test_ollama_integration.py --ignore=tests/test_ollama_multi_agent.py
```

## Documentation

Detailed documentation in `docs/`:

- [`docs/requirements.md`](docs/requirements.md) — Feature requirements & implementation status
- [`docs/technical_design.md`](docs/technical_design.md) — Full technical design
- [`docs/structure.md`](docs/structure.md) — Module structure & collaboration architecture
- [`docs/implementation_plan.md`](docs/implementation_plan.md) — Roadmap & milestone plan
- [`docs/gui_chat_redesign.md`](docs/gui_chat_redesign.md) — Web GUI chat redesign
- [`docs/details/Agent_Introduction.md`](docs/details/Agent_Introduction.md) — Agent base class & ReAct runtime
- [`docs/details/Master_Introduction.md`](docs/details/Master_Introduction.md) — MasterAgent orchestration
- [`docs/details/MCP_Introduction.md`](docs/details/MCP_Introduction.md) — MCP tool layer
- [`docs/details/Memory_Introduction.md`](docs/details/Memory_Introduction.md) — Memory system
- [`docs/details/Message_Introduction.md`](docs/details/Message_Introduction.md) — Message bus
- [`docs/details/GlobalMemory_Introduction.md`](docs/details/GlobalMemory_Introduction.md) — Global knowledge & experience distillation
- [`docs/details/GUI_Introduction.md`](docs/details/GUI_Introduction.md) — Web GUI

## Roadmap

| Phase | Deliverables | Status |
|-------|-------------|--------|
| P1 — Message Bus | WorkflowMessage + InProcessBroker + BusServer/Client + tool-request flow (TOOL_REQUEST/TOOL_RESPONSE) | ✅ |
| P2 — Built-in Tools | 9 built-in tools + BuiltinToolProvider | ✅ |
| P3 — Orchestration | MasterAgent + WorkflowPlan + ToolGuardian + 3-level approval + process isolation | ✅ |
| P4 — Tool Vectorization | ToolVault + ToolStore + AgentToolContext + ApprovalManager | ✅ |
| P5 — Skill Import | Skill loader, manifest model, doc auto-generation | Planned |
| P6 — Global Memory | GlobalMemory + PostTaskPipeline + ToolGuardian feedback loop + FeedbackCollector | ✅ |
| P7 — Plan Orchestration | WorkflowPlanner + PlanMemory + step retry/timeout guard | ✅ |
| P8 — Hierarchical Sub-Master | Sub-Master nested orchestration | Planned |

## License

This project is licensed under the MIT License.

---

# 中文

**English | [中文](#中文)**

**YouMi Agent** 是一个基于 Python 的轻量级多 Agent 协作框架。由 `MasterAgent` 编排层接收用户任务，自动创建子 Agent，通过自研 MCP（Model Context Protocol）工具层统一管理工具，为每个 Agent 独立维护记忆系统，并通过消息总线实现 Agent 间通信，从而实现灵活、可扩展的智能任务协作。

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

---

## 核心特性

- **Plan-then-Execute 编排** — LLM 先生成结构化 `WorkflowPlan`，由确定性的 `WorkflowExecutor` 按 DAG 拓扑序调度执行，不确定性限制在规划层
- **Plan 记忆复用** — 相似任务的执行方案持久化到独立 SQLite 存储（`PlanMemory`，基于 sqlite-vec 向量化），命中时直接复用骨架，关键词降级兜底
- **统一 MCP 工具层** — 自研 JSON-RPC 2.0 协议，所有工具通过 `MCPServer` 统一注册，Agent 经 `ToolBridge`（`MCPClient` → `ToolBridge` → `AgentToolContext`）调用，支持 HOT/WARM/COLD 动态加载/卸载与语义向量搜索（`ToolVault` + `ToolStore`）
- **动态工具发现** — `search_new_tools` 元工具让 Agent 按需发现并授权加载更多工具
- **MasterAgent 协调器工具** — 6 个编排工具（`create_sub_agent`、`run_sub_agent`、`list_sub_agents`、`list_available_roles`、`approve_tool_request`、`deny_tool_request`），使 MasterAgent 可以创建、运行并管理子 Agent
- **独立记忆管理** — 每个 Agent 拥有独立的 `MemoryManager`，支持可插拔策略（Full / Summary / LSTM）和后端（SQLite / 文件），超 token 时自动压缩
- **三级工具审批** — `ApprovalManager` 实现 AUTO / MANUAL / MASTER 三级审批，最小权限原则，生成完整审计日志
- **全局知识沉淀** — `PostTaskPipeline` 在任务结束后自动提取工具经验，写入 `GlobalMemory`，供 `ToolGuardianAgent` 诊断修复；人工反馈通过 `FeedbackCollector` 回写
- **进程隔离执行** — `SubProcessAgentRunner` 基于 `asyncio` 子进程，隔离崩溃传播
- **零侵入可观测** — Hook 系统 + GUI WebSocket 实时推送（含 `WorkflowTracker` 步骤追踪），无需修改引擎代码即可观测 Agent 行为
- **全栈异步** — 基于 `asyncio`，全链路非阻塞 I/O，优雅降级（向量化 / LLM 失败时自动回退规则路径）

## 系统架构

```
┌──────────────────────────────────────────────────┐
│                  用户接口层                        │
│        Web GUI (aiohttp REST + WebSocket)         │
│        Python API                                 │
└───────────────────┬──────────────────────────────┘
                    │ 用户任务
                    ▼
┌──────────────────────────────────────────────────┐
│              编排层 (coordinator/)                 │
│  MasterAgent  WorkflowPlanner/Plan  PlanMemory    │
│  ToolGuardianAgent  PostTaskPipeline               │
│  HandoffProtocol  SubProcessAgentRunner            │
│  ToolApprovalMixin  FixStrategiesMixin             │
└───────────────────┬──────────────────────────────┘
                    │ 创建 & 调度 SubAgent
                    ▼
┌──────────────────────────────────────────────────┐
│           Agent 运行时 (core/)                     │
│  Agent + ReAct循环  LLMClient  MemoryManager      │
│  HookRegistry  PluginManager  PromptAssembler     │
│  ToolRegistry  ToolExecutionMixin  MCPIntegration │
└────┬──────────────┬─────────────────┬────────────┘
     │              │                 │
┌────▼────┐  ┌──────▼──────┐  ┌──────▼────────┐
│  MCP 层  │  │   记忆层     │  │  全局知识层    │
│ Vault   │  │ Strategy    │  │ GlobalMemory  │
│ Store   │  │ Backend     │  │ KnowledgeEntry│
│ Approval│  │ Compactor   │  │ ExperienceExt │
│ Bridge  │  │             │  │ Feedback      │
└─────────┘  └─────────────┘  └───────────────┘
```

## 项目结构

```
YouMi_Agent/
├── youmi/                  # 核心框架包
│   ├── core/               # Agent 运行时：Agent 基类 + ReAct 循环、Hook、Plugin 系统、
│   │                       #   ToolRegistry、Prompt 组装、工具执行 / MCP 集成 Mixin
│   ├── coordinator/        # 编排层：MasterAgent、WorkflowPlan/Executor/Planner、PlanMemory、
│   │                       #   ToolGuardianAgent、PostTaskPipeline、HandoffProtocol、
│   │                       #   SubProcessAgentRunner、ToolApprovalMixin、FixStrategiesMixin
│   ├── mcp/                # MCP 协议层：Server / Client / Bridge / Vault / Store / Approval / Provider
│   ├── memory/             # 记忆系统：Strategy（full/summary/lstm）、Backend（sqlite/file）、Compactor
│   ├── knowledge/          # 全局知识：GlobalMemory、ToolExperienceExtractor、FeedbackCollector
│   ├── bus/                # 消息总线：WorkflowMessage、InProcessBroker、BusServer/Client
│   ├── tools/              # 内置工具提供者：文件、Shell、Web、数据处理 + 协调器编排工具
│   ├── llm/                # LLM 客户端（OpenAI 兼容 API / Ollama） + Embedding 客户端
│   ├── scheduler/          # 心跳调度器
│   ├── agents/             # Agent 角色 YAML 配置（master、tool_guardian）
│   └── _vec_utils.py       # sqlite-vec 共享工具（ToolStore / GlobalMemory / PlanMemory 复用）
├── gui/                    # Web GUI（aiohttp REST + WebSocket）
│   ├── engine/             # 引擎桥接层：EngineBridge、GUIHooks、MCPService、WorkflowTracker
│   ├── hub/                # WebSocket 中心：事件定义、连接管理
│   ├── persistence/        # 会话与消息 JSON 持久化
│   ├── static/             # 前端资源（HTML/CSS/JS，三栏聊天布局）
│   ├── config.py           # 环境变量运行时配置
│   ├── server.py           # aiohttp 服务端（REST + WebSocket，支持 --mock 模式）
│   └── mock_engine.py      # 前端开发用 Mock 引擎
├── tests/                  # 测试套件
└── docs/                   # 文档（需求、架构设计、模块详解）
```

## 快速开始

### 环境要求

- Python >= 3.10
- （可选）支持 OpenAI 兼容接口的 LLM 服务，或本地 [Ollama](https://ollama.com)

### 安装

```bash
# 克隆仓库
git clone https://github.com/CatOlin-Zhang/YouMi_Agent.git
cd YouMi_Agent

# 安装核心依赖
pip install -e .

# 安装 Web GUI 依赖（可选）
pip install -e ".[web]"

# 安装向量检索支持（可选，用于工具语义搜索和 PlanMemory 向量复用）
pip install -e ".[vec]"

# 安装开发依赖（可选）
pip install -e ".[dev]"
```

### 启动 Web GUI

```bash
# Windows
gui\start_gui.bat

# 或直接启动
python -m gui

# 可选：以 Mock 引擎启动（无需 LLM）
python -m gui --mock
```

启动后访问 `http://localhost:8766`。

可通过环境变量配置：

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `YOUMI_GUI_HOST` | `127.0.0.1` | 监听地址 |
| `YOUMI_GUI_PORT` | `8766` | HTTP 监听端口 |
| `YOUMI_GUI_MASTER` | `master` | 主 Agent 名称（对应 `youmi/agents/<name>/config.yaml`）|
| `YOUMI_GUI_MCP` | `1` | 启用 MCP 工具调用层 |
| `YOUMI_GUI_BUS` | `1` | 启用进程内消息总线 |
| `YOUMI_GUI_VAULT` | `1` | 启用 ToolVault + ToolStore（sqlite-vec）|
| `YOUMI_GUI_VAULT_DB` | （空）| ToolStore 数据库路径 |
| `YOUMI_GUI_EMBEDDING_URL` | `http://localhost:11434/v1` | Embedding 服务地址 |
| `YOUMI_GUI_EMBEDDING_MODEL` | `nomic-embed-text` | Embedding 模型名 |

### Python API 快速示例

```python
import asyncio
from youmi.coordinator.master import MasterAgent

async def main():
    # 从 youmi/agents/master/config.yaml 加载配置
    # （默认 llm_config 指向本地 Ollama 实例）
    master = MasterAgent.from_config_dir()
    await master.initialize()

    # 提交任务，自动走 Plan-then-Execute 流程
    result = await master.run("帮我分析 data.csv 并生成摘要报告")
    print(result)

asyncio.run(main())
```

显式配置示例：

```python
import asyncio
from youmi.coordinator.master import MasterAgent
from youmi.core.models import AgentConfig
from youmi.core.types import LLMConfig, LLMProvider

async def main():
    config = AgentConfig(
        name="master",
        llm_config=LLMConfig(
            provider=LLMProvider.LOCAL,
            model="qwen2.5:0.5b",
            base_url="http://localhost:11434/v1",
            api_key="ollama",
        ),
    )
    master = MasterAgent(config)   # initialize() 时根据 config.llm_config 创建 LLM 客户端
    await master.initialize()
    result = await master.run("总结 report.pdf 中的关键结论")
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
| `plan_memory.py` | `PlanMemory`，独立 SQLite 存储，向量余弦相似度检索 + 关键词降级 |
| `tool_guardian.py` | `ToolGuardianAgent`，工具问题诊断与修复，全局记忆反馈闭环 |
| `post_task.py` | `PostTaskPipeline`，4 阶段后台流水线（工具统计、摘要、Guardian、GlobalMemory）|
| `handoff.py` | `HandoffProtocol`，Agent 间任务委派协议 |
| `subprocess_agent.py` | `SubProcessAgentRunner`，进程隔离执行 |
| `tool_approval.py` | `ToolApprovalMixin`，三级审批集成 |
| `fix_strategies.py` | `FixStrategiesMixin`，工具守护闭环调用的修复策略 |

### MCP 工具层 (`youmi/mcp/`)

所有工具通过 `MCPServer` 以 JSON-RPC 2.0 协议统一注册，Agent 经 `MCPClient` → `ToolBridge` → `AgentToolContext` 调用。`ToolStore` 使用 SQLite + `sqlite-vec` 持久化（6 张表：tools、vec_tools、tool_changelogs、tool_aliases、tool_tags、tool_dependencies）；`ToolVault` 维护 HOT/WARM/COLD 工具清单；`ApprovalManager` 执行三级审批。

### 记忆系统 (`youmi/memory/`)

| 策略 | 说明 |
|------|------|
| `FullMemoryStrategy` | 保留全部历史消息 |
| `SummaryMemoryStrategy` | 超限时调用 LLM 压缩旧消息为摘要 |
| `LSTMMemoryStrategy` | 双通道：近期消息 + 重要事件长期保留 |

后端支持 `SQLiteBackend`（持久化）和 `FileBackend`（JSON 文件）。`ContextCompactor` 在超出 token 预算时自动触发压缩。

### 全局知识 (`youmi/knowledge/`)

| 模块 | 职责 |
|------|------|
| `global_memory.py` | `GlobalMemory`，工具经验知识库，支持向量语义检索 |
| `experience_extractor.py` | `ToolExperienceExtractor`，从任务完成中沉淀工具经验 |
| `feedback.py` | `FeedbackCollector`，人工正/负反馈回写全局记忆，达到阈值触发 Guardian 修复 |

### LLM 层 (`youmi/llm/`)

- `client.py` — `LLMClient`，异步 HTTP 客户端，兼容任意 OpenAI Chat Completions 接口（OpenAI / Anthropic 代理 / Ollama / vLLM / llama.cpp）
- `embeddings.py` — `EmbeddingClient`，用于工具与记忆向量化

### 内置工具 (`youmi/tools/`)

`BuiltinToolProvider` 注册 9 个标准内置工具：

| 工具 | 用途 |
|------|------|
| `file_search` | 按 glob 模式在工作区中查找文件 |
| `file_read` | 读取文本文件（支持偏移/行数限制）|
| `file_write` | 写入 / 追加文本到文件 |
| `list_directory` | 列出目录内容 |
| `text_search` | 在文件内做全文本搜索 |
| `shell_exec` | 执行 Shell 命令（带超时）|
| `web_fetch` | 抓取 URL 并返回文本内容 |
| `get_datetime` | 当前日期 / 时间 |
| `json_tool` | JSON 解析 / 转换 / 格式化 |

此外：

- **协调器工具**（`coordinator_ops.py`）为 `MasterAgent` 注册：`create_sub_agent`、`run_sub_agent`、`list_sub_agents`、`list_available_roles`、`approve_tool_request`、`deny_tool_request`。
- `search_new_tools` 是**元工具**（非内置工具），由 MCP `ToolBridge` 提供 —— Agent 可在运行时调用它发现并授权加载更多工具。

## 技术选型

| 组件 | 技术 |
|------|------|
| 开发语言 | Python 3.10+，全栈 asyncio |
| 数据验证 | pydantic >= 2.0 |
| HTTP / 异步客户端 | httpx >= 0.27 |
| LLM 接口 | OpenAI 兼容 API / Ollama（provider：openai / anthropic / local / custom）|
| 工具与知识持久化 | SQLite + sqlite-vec（向量索引）|
| 消息总线 | asyncio.Queue（进程内）+ WebSocket（`websockets`，跨进程）|
| Web GUI | aiohttp >= 3.9 + 原生 HTML/CSS/JS（REST + WebSocket）|
| 配置管理 | YAML + 环境变量 |
| 测试框架 | pytest + pytest-asyncio |

## 运行测试

```bash
# 运行全部测试
pytest tests/

# 排除需要外部 LLM 服务的测试
pytest tests/ --ignore=tests/test_ollama_integration.py --ignore=tests/test_ollama_multi_agent.py
```

## 文档

详细文档位于 `docs/` 目录：

- [`docs/requirements.md`](docs/requirements.md) — 功能需求与实现状态
- [`docs/technical_design.md`](docs/technical_design.md) — 完整技术设计
- [`docs/structure.md`](docs/structure.md) — 模块结构与协作架构
- [`docs/implementation_plan.md`](docs/implementation_plan.md) — 路线图与里程碑计划
- [`docs/gui_chat_redesign.md`](docs/gui_chat_redesign.md) — Web GUI 聊天界面重构
- [`docs/details/Agent_Introduction.md`](docs/details/Agent_Introduction.md) — Agent 基类与 ReAct 运行时
- [`docs/details/Master_Introduction.md`](docs/details/Master_Introduction.md) — MasterAgent 编排层
- [`docs/details/MCP_Introduction.md`](docs/details/MCP_Introduction.md) — MCP 工具层
- [`docs/details/Memory_Introduction.md`](docs/details/Memory_Introduction.md) — 记忆系统
- [`docs/details/Message_Introduction.md`](docs/details/Message_Introduction.md) — 消息总线
- [`docs/details/GlobalMemory_Introduction.md`](docs/details/GlobalMemory_Introduction.md) — 全局知识与经验沉淀
- [`docs/details/GUI_Introduction.md`](docs/details/GUI_Introduction.md) — Web GUI

## 里程碑

| 阶段 | 核心交付 | 状态 |
|------|---------|------|
| P1 — 消息总线 | WorkflowMessage + InProcessBroker + BusServer/Client + 工具申请流程（TOOL_REQUEST/TOOL_RESPONSE）| ✅ |
| P2 — 内置工具 | 9 个内置工具 + BuiltinToolProvider | ✅ |
| P3 — 编排层 | MasterAgent + WorkflowPlan + ToolGuardian + 三级审批 + 进程隔离 | ✅ |
| P4 — 工具向量化 | ToolVault + ToolStore + AgentToolContext + ApprovalManager | ✅ |
| P5 — Skill 导入 | Skill 加载器、清单模型、文档自动生成 | 规划中 |
| P6 — 全局记忆 | GlobalMemory + PostTaskPipeline + ToolGuardian 反馈闭环 + FeedbackCollector | ✅ |
| P7 — Plan 编排改造 | WorkflowPlanner + PlanMemory + 步骤重试/超时兜底 | ✅ |
| P8 — 层级 Sub-Master | Sub-Master 嵌套编排 | 规划中 |

## 许可证

本项目基于 MIT 许可证开源。
