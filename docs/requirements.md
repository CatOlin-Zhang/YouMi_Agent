# YouMi Agent — 多 Agent 协作框架 需求文档

> 版本：v0.6.0（需求状态与代码对齐，对应 Phase 6 全局记忆闭环落地）
> 更新日期：2026-08-31
> 原始草案：v0.1.0（2026-08-04）

---

## 1. 项目概述

YouMi Agent 是一个多 Agent 协作框架，由 `MasterAgent` 编排层接收用户任务，**自动创建子 Agent**，通过 **MCP 工具层**（Server/Client/Vault/Store）统一暴露和管理工具，为每个 Agent **独立管理记忆**，并通过 **消息总线**（WorkflowMessage + InProcessBroker）实现 Agent 间通信，从而实现灵活、可扩展的智能任务协作系统。

### 1.1 核心设计目标与实现状态

| 目标 | 描述 | 状态 |
|------|------|------|
| **任务驱动的 Agent 实例化** | 根据用户提交的任务自动分析、拆解，并创建对应的 Agent 实例 | ✅ MasterAgent.create_sub_agent() |
| **统一 MCP 工具服务** | 通过 MCP 协议统一暴露工具，Agent 通过 ToolBridge 调用 | ✅ MCPServer/Client/Bridge |
| **独立记忆管理** | 每个 Agent 拥有独立的 MemoryManager，支持多种策略与后端 | ✅ MemoryManager + Strategy + Backend |
| **多 Agent 协作** | Agent 间通过消息总线通信，MasterAgent 调度协作 | ✅ InProcessBroker + WorkflowPlan |
| **工具经验积累** | 跨任务沉淀工具使用经验，供 ToolGuardian 诊断修复 | ✅ GlobalMemory + PostTaskPipeline |

---

## 2. 系统架构概览（真实）

```
┌──────────────────────────────────────────────────┐
│              用户接口层                             │
│        Web GUI (aiohttp) / Python API              │
└───────────────────┬──────────────────────────────┘
                    │ 用户任务
                    ▼
┌──────────────────────────────────────────────────┐
│              编排层 (coordinator/)                 │
│  MasterAgent  WorkflowPlan  HandoffProtocol       │
│  ToolGuardianAgent  PostTaskPipeline              │
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
│  MCP 层  │  │  记忆层      │  │  全局知识层    │
│ Vault   │  │ Strategy    │  │ GlobalMemory  │
│ Store   │  │ Backend     │  │ KnowledgeEntry│
│ Approval│  │ Compactor   │  │ ExperienceExt │
└─────────┘  └─────────────┘  └───────────────┘
```

---

## 3. 功能需求

### 3.1 任务驱动的 Agent 自动实例化

| 需求 ID | 描述 | 实现状态 |
|---------|------|---------|
| **F1.1** | 系统接收自然语言任务，分析所需 Agent 角色 | ✅ MasterAgent 内置任务分析推理 |
| **F1.2** | 任务拆解包含：Agent 角色列表、职责、工具需求 | ✅ WorkflowPlan 步骤定义 |
| **F1.3** | 提供 Agent 工厂，根据角色自动创建 Agent 实例 | ✅ `create_sub_agent(name, role, task)` |
| **F1.4** | Agent 实例包含唯一标识、角色、初始工具集 | ✅ AgentConfig（agent_id / role / allowed_tools）|
| **F1.5** | 支持预定义 Agent 角色模板 | ✅ YAML config.yaml（youmi/agents/）|
| **F1.6** | 支持用户自定义 Agent 角色模板 | ✅ YAML 声明式扩展 |

### 3.2 工具管理（原「动态 Skill 与 Tool 装载」）

> 注：框架中无独立 Skill 模块，工具通过 MCP 协议统一注册，按三级状态（HOT/WARM/COLD）动态管理。

| 需求 ID | 描述 | 实现状态 |
|---------|------|---------|
| **F2.1** | 维护全局工具注册中心，管理所有可用工具 | ✅ ToolVault + ToolStore（SQLite）|
| **F2.2** | 工具定义包含：名称、描述、参数 Schema | ✅ MCPServer 工具注册 + BuiltinToolProvider |
| **F2.3** | Agent 可在运行时动态加载/卸载工具 | ✅ ToolBridge + AgentToolContext（HOT/WARM/COLD）|
| **F2.4** | 工具语义搜索，按需为 Agent 补充工具 | ✅ ToolVault.search() + EmbeddingClient + sqlite-vec |
| **F2.5** | 工具版本管理与变更日志 | ✅ ToolStore（version/parent_version_id/tool_changelogs）|
| **F2.6** | 工具调用超时控制 | 🔲 仅 shell_exec 有超时，其他工具未统一 |
| **F2.7** | Skill 模块（声明式技能组合） | 🔲 未实现（Phase 5，见路线图）|

### 3.3 统一 MCP 工具服务

| 需求 ID | 描述 | 实现状态 |
|---------|------|---------|
| **F3.1** | 提供统一 MCPServer，作为工具调用网关 | ✅ `youmi/mcp/server.py` |
| **F3.2** | 实现标准 MCP 协议（工具发现/描述/调用）| ✅ JSON-RPC 协议 + `protocol.py` |
| **F3.3** | 所有工具通过 MCPServer 统一注册，Agent 通过 MCPClient 调用 | ✅ LocalFunctionProvider + BuiltinToolProvider |
| **F3.4** | 工具访问权限控制（三级审批） | ✅ ApprovalManager（AUTO/MANUAL/MASTER）|
| **F3.5** | 工具调用日志记录 | ✅ Hook（BEFORE/AFTER_TOOL_CALL）+ 审批审计日志 |
| **F3.6** | 工具插件化扩展 | ✅ ToolProvider 抽象接口 |
| **F3.7** | 统一超时控制与重试 | 🔲 未统一实现 |

### 3.4 独立记忆管理

| 需求 ID | 描述 | 实现状态 |
|---------|------|---------|
| **F4.1** | 每个 Agent 拥有独立的记忆空间，彼此隔离 | ✅ MemoryManager（per-agent）|
| **F4.2** | 支持短期记忆（会话上下文） | ✅ FullMemoryStrategy / SummaryMemoryStrategy / LSTMMemoryStrategy |
| **F4.3** | 支持长期记忆（跨任务知识积累） | ✅ GlobalMemory（工具经验，仅供 ToolGuardian）|
| **F4.4** | 记忆向量语义检索 | ✅ MemoryManager.search() + EmbeddingClient |
| **F4.5** | 记忆写入/读取/持久化/归档 | ✅ SQLiteBackend / FileBackend |
| **F4.6** | 超 token 时自动压缩上下文 | ✅ ContextCompactor |
| **F4.7** | 可插拔记忆后端 | ✅ PersistenceBackend 抽象接口（SQLite/File）|
| **F4.8** | Agent 间共享记忆 | 🔲 GlobalMemory 仅供 ToolGuardian，暂无通用共享记忆空间 |

### 3.5 多 Agent 协作

| 需求 ID | 描述 | 实现状态 |
|---------|------|---------|
| **F5.1** | Agent 间消息传递（点对点 + 广播）| ✅ InProcessBroker + WorkflowMessage |
| **F5.2** | 调度器管理任务依赖与执行顺序 | ✅ WorkflowPlan + WorkflowExecutor（DAG）|
| **F5.3** | 支持顺序/并行执行模式 | ✅ WorkflowExecutor(mode="serial"|"parallel") |
| **F5.4** | Agent 可委托子任务给其他 Agent | ✅ HandoffProtocol + request_tool() |
| **F5.5** | 异常处理（工具修复闭环） | ✅ ToolGuardianAgent + FixStrategiesMixin |
| **F5.6** | 可观测性（日志 + 状态追踪） | ✅ Hook 系统 + AgentStatus + WorkflowTracker（GUI）|
| **F5.7** | 进程隔离执行 | ✅ SubProcessAgentRunner（asyncio 子进程）|
| **F5.8** | 工具申请流程（子 Agent 申请新工具）| ✅ TOOL_REQUEST/TOOL_RESPONSE 消息类型 |
| **F5.9** | 层级架构（Sub-Master） | 🔲 未实现（Phase 7，见路线图）|

---

## 4. 非功能需求

### 4.1 可扩展性

| 需求 ID | 描述 | 状态 |
|---------|------|------|
| **NF1.1** | 插件化架构，核心模块可被替换或扩展 | ✅ Plugin / MemoryStrategy / PersistenceBackend 均为可替换插件 |
| **NF1.2** | 新增 Agent 角色、Tool 不需修改框架核心代码 | ✅ YAML 声明 + ToolProvider 扩展 |
| **NF1.3** | 水平扩展，支持同时运行大量 Agent 实例 | 🔲 单机单进程（容器化/多 Worker 未实现）|

### 4.2 可靠性

| 需求 ID | 描述 | 状态 |
|---------|------|------|
| **NF2.1** | LLM 调用重试与退避 | 🔲 未实现（P0 优先级）|
| **NF2.2** | 持久化任务队列（崩溃恢复） | 🔲 消息总线为内存态 |
| **NF2.3** | 记忆数据持久性 | ✅ SQLiteBackend / FileBackend |
| **NF2.4** | 工具修复后自动版本更新 | ✅ PostTaskPipeline 阈值触发 trigger_tool_version_update() |
| **NF2.5** | 优雅降级（向量化/LLM 失败时降级）| ✅ 全链路均有降级路径 |

### 4.3 安全性

| 需求 ID | 描述 | 状态 |
|---------|------|------|
| **NF3.1** | 工具调用权限控制，最小权限原则 | ✅ 三级审批（AUTO/MANUAL/MASTER）|
| **NF3.2** | 总线认证与传输安全 | 🔲 BusServer 无认证（P0 优先级）|
| **NF3.3** | 执行沙箱 | 🔲 shell_ops 无隔离（P0 优先级）|

### 4.4 可观测性

| 需求 ID | 描述 | 状态 |
|---------|------|------|
| **NF4.1** | 完整执行日志（Agent 决策 + 工具调用链路）| ✅ Hook 系统 + logging |
| **NF4.2** | Agent 执行状态实时监控 | ✅ AgentStatus + GUI WorkflowTracker |
| **NF4.3** | 分布式追踪（OpenTelemetry）| 🔲 未实现（P0 优先级）|
| **NF4.4** | 审计日志 | ✅ ApprovalManager 审批审计日志 |

---

## 5. 技术选型（实际）

| 组件 | 实际技术 | 说明 |
|------|---------|------|
| 开发语言 | Python 3.10+ | asyncio 全栈 |
| Agent 运行时 | 自研（youmi/core/） | ReAct 循环 + Hook + Plugin |
| MCP 协议 | 自研 JSON-RPC 实现 | youmi/mcp/（Server/Client/Bridge）|
| LLM 接口 | OpenAI 兼容 API / Ollama | LLMClient 支持多 provider |
| 工具持久化 | SQLite + sqlite-vec | ToolStore（6 张表 + 向量索引）|
| 会话记忆 | SQLite / JSON 文件 | SQLiteBackend / FileBackend |
| 全局知识库 | SQLite + sqlite-vec | GlobalMemory（向量语义检索）|
| 任务编排 | 自研 DAG 调度器 | WorkflowPlan + WorkflowExecutor |
| 消息总线 | asyncio.Queue（进程内）| InProcessBroker + BusServer/Client |
| GUI | aiohttp + 原生 HTML/CSS/JS | WebSocket + REST 双模式 |
| 配置管理 | YAML | Agent 角色 config.yaml |

---

## 6. 已完成里程碑

| 里程碑 | 核心交付 | 状态 |
|--------|---------|------|
| **P1 — 消息总线** | WorkflowMessage + InProcessBroker + BusServer/Client | ✅ |
| **P2 — 内置工具** | 9 个内置工具 + BuiltinToolProvider | ✅ |
| **P3 — 编排层** | MasterAgent + WorkflowPlan + ToolGuardian + 三级审批 + 进程隔离 | ✅ |
| **P4 — 工具向量化** | ToolVault + ToolStore（sqlite-vec）+ AgentToolContext + ApprovalManager | ✅ |
| **P5 — Skill 导入** | Skill 模块 | 🔲 未实现 |
| **P6 — 全局记忆** | GlobalMemory + ToolExperienceExtractor + PostTaskPipeline + ToolGuardian 闭环 | ✅ |
| **GUI** | aiohttp Web 应用 + EngineBridge + MCPService + GUIHooks | ✅ |

---

## 7. 待实现需求（路线图参见 implementation_plan.md）

| 优先级 | 需求 |
|--------|------|
| **P0（生产前置）** | LLM 重试退避、持久化任务队列、总线认证、执行沙箱、OTel 追踪 |
| **P1（功能闭环）** | AgentToolContext 轮次自动推进、召回确认闭环、FastAPI 网关 |
| **P2（工程化）** | Skill 导入（Phase 5）、CI/CD、成本计量 |
| **P3（扩展）** | 层级架构（Sub-Master）、A2A/AG-UI 协议互操作 |

---

## 8. 术语说明（变更）

原草案中「Skill」概念在当前实现中由 **PromptLayer 动态注入 + 工具组合** 代替，没有独立的 Skill 注册中心或 YAML skill 配置文件；原「AgentFactory」功能由 **MasterAgent.create_sub_agent()** 承担，没有独立的 AgentFactory 类。
