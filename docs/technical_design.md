# YouMi Agent — 多 Agent 协作框架 技术设计

> 版本：v0.6.0（对应 Phase 6 全局记忆闭环落地）
> 更新日期：2026-08-31
> 状态：**与真实代码对齐**（替换 v0.1.0 草案）

---

## 1. 设计原则

| 原则 | 说明 | 实现状态 |
|------|------|---------|
| **插件化优先** | 核心组件（Agent、Memory Strategy、Persistence Backend、Hook）均为可替换插件 | ✅ |
| **声明式配置** | Agent 角色通过 YAML 声明，零代码扩展 | ✅（YAML config.yaml）|
| **异步驱动** | 全栈 asyncio，Agent 执行、工具调用、消息传递均为非阻塞 | ✅ |
| **协议标准** | 内置 MCP 协议层，工具统一注册与调用 | ✅ |
| **最小权限** | 三级审批模型，Agent 仅能访问被授权的工具 | ✅ |
| **零侵入观测** | Hook 系统无需修改引擎代码即可观测 Agent 行为 | ✅ |
| **优雅降级** | 向量搜索/LLM 分析失败时自动降级为关键词/规则路径 | ✅ |

---

## 2. 整体架构

```
┌─────────────────────────────────────────────┐
│              用户接口层                       │
│   Web GUI (aiohttp)  或  直接 Python API      │
└────────────────┬────────────────────────────┘
                 │
┌────────────────▼────────────────────────────┐
│            编排层 (coordinator/)             │
│                                             │
│  MasterAgent  ─────────────────────────     │
│    │ create_sub_agent()                     │
│    │ WorkflowPlan / WorkflowExecutor (DAG)  │
│    │ HandoffProtocol                        │
│    │ ToolGuardianAgent                      │
│    └─ PostTaskPipeline (任务后台流水线)       │
└────────────────┬────────────────────────────┘
                 │ 创建 & 调度 SubAgent
┌────────────────▼────────────────────────────┐
│          Agent 运行时 (core/)                │
│                                             │
│  Agent ─── ReAct 四阶段循环                  │
│    ├─ LLMClient          (llm/)             │
│    ├─ ToolBridge/MCP     (mcp/)             │
│    ├─ MemoryManager      (memory/)          │
│    ├─ HookRegistry       (core/hooks.py)    │
│    ├─ PluginManager      (core/plugin.py)   │
│    └─ PromptAssembler    (core/prompt.py)   │
└────┬───────────┬───────────────┬────────────┘
     │           │               │
┌────▼──┐ ┌─────▼──────┐ ┌──────▼──────────┐
│  MCP  │ │  Memory    │ │  GlobalMemory   │
│ Layer │ │  System    │ │ (knowledge/)    │
│       │ │            │ │                 │
│Server │ │Strategy    │ │ KnowledgeEntry  │
│Client │ │ Full/Sum.  │ │ ToolKnowledge   │
│Bridge │ │ /LSTM      │ │ ExperienceExtr. │
│Vault  │ │Backend     │ │                 │
│Store  │ │ SQLite/File│ │                 │
└───────┘ └────────────┘ └─────────────────┘
```

### 2.1 分层职责

| 层 | 职责 | 核心组件 |
|----|------|---------|
| **用户接口层** | 接收任务、展示结果、管理会话 | GUI (aiohttp)、Python API |
| **编排层** | 任务分析、子 Agent 工厂、协作调度、工具守护 | MasterAgent、WorkflowPlan、ToolGuardianAgent、PostTaskPipeline |
| **Agent 运行时** | ReAct 循环、工具调用、记忆管理、Hook/Plugin | Agent、LLMClient、ToolBridge、MemoryManager |
| **MCP 工具层** | 工具注册、发现、版本管理、审批、向量搜索 | MCPServer/Client、ToolVault、ToolStore、ApprovalManager |
| **记忆层** | 会话上下文管理（Session 记忆） | MemoryManager、MemoryStrategy、PersistenceBackend、ContextCompactor |
| **全局知识层** | 跨任务工具经验沉淀（仅供 ToolGuardian 消费） | GlobalMemory、KnowledgeEntry、ToolExperienceExtractor |

---

## 3. Agent 核心（youmi/core/agent.py）

### 3.1 Agent 状态机

```python
class AgentStatus(str, Enum):
    IDLE      = "idle"       # 空闲，等待任务
    RUNNING   = "running"    # 正在执行 ReAct 循环
    WAITING   = "waiting"    # 等待工具审批或外部事件
    COMPLETED = "completed"  # 任务完成
    FAILED    = "failed"     # 任务失败
    DESTROYED = "destroyed"  # 已销毁
```

### 3.2 ReAct 循环（四阶段）

```
任务输入
    ↓
_observe()   ← 读取消息队列 + 调用 MemoryManager.get_context()
    ↓
_think()     ← 调用 LLMClient，生成工具调用列表或文本响应
    ↓         （PromptAssembler 组装系统提示 + 历史上下文）
_act()       ← 执行 ToolBridge.call_tool() / 写入记忆
    ↓         （ContextCompactor.maybe_compact() 超 token 时压缩）
_reflect()   ← 判断是否继续循环，或输出最终答案
    ↓
（未达成目标则返回 _observe，否则结束）
```

### 3.3 核心配置（AgentConfig）

```python
@dataclass
class AgentConfig:
    agent_id: str                    # UUID，全局唯一
    name: str                        # Agent 名称
    role: str                        # 角色标识
    system_prompt: str               # 系统提示词（可含 PromptLayer 模板）
    llm_config: LLMConfig            # LLM 配置（model/temperature/max_tokens/...）
    allowed_tools: list[str]         # 初始授权工具列表
    memory_config: MemoryConfig      # 记忆配置（strategy/backend/...）
    max_iterations: int = 20         # ReAct 最大迭代次数
    token_budget: int = 0            # 上下文 token 预算（0 = 不限制）
```

### 3.4 Hook 系统（youmi/core/hooks.py）

7 个挂载点，全部为异步回调：

| HookType | 触发时机 | 典型用途 |
|----------|---------|---------|
| `BEFORE_PROMPT_BUILD` | PromptAssembler 组装前 | 动态注入 system 指令 |
| `BEFORE_MODEL_CALL` | LLM 调用前 | Token 计量、请求审计 |
| `AFTER_MODEL_CALL` | LLM 响应后 | 响应日志、流式转发 |
| `BEFORE_TOOL_CALL` | 工具调用前 | GUI 推送工具卡片 |
| `AFTER_TOOL_CALL` | 工具调用后 | GUI 推送工具结果 |
| `MESSAGE_RECEIVED` | 收到消息时 | 消息路由 |
| `MESSAGE_SENDING` | 发送消息前 | GUI 流式气泡推送 |

3 个 Decision 类型（可干预引擎流程）：`BLOCK_TOOL`、`MODIFY_PROMPT`、`SKIP_TOOL`。

### 3.5 双模式工具接入

```
本地模式：agent.register_tool(fn)  →  LocalFunctionProvider  →  MCPServer
MCP 模式：agent.connect_mcp(server) →  MCPClient  →  ToolBridge  →  [HOT/WARM/COLD 工具池]
```

两种模式可并存，`ToolBridge.to_openai_tools()` 统一向 LLM 暴露工具列表。

### 3.6 消息总线集成

`Agent.connect_bus(broker, workflow_id)` 接入 `InProcessBroker`，提供：

| 方法/属性 | 说明 |
|---------|------|
| `await send_message(type, to, payload)` | 发送消息到总线 |
| `await wait_for_message(type, timeout)` | 等待特定类型消息 |
| `await request_tool(tool_name, reason)` | 发起工具申请（TOOL_REQUEST） |
| `workflow_id` | 当前工作流 ID |
| `bus_connected` | 是否已接入总线 |

### 3.7 工作流自检（_TaskSelfCheck）

`run()` 前执行 `_self_check_task()`：检查当前已接入工具是否足以完成任务。若工具不足，Agent 可通过 `search_new_tools` 主动从 ToolVault 向量搜索补充工具，或向 MasterAgent 发起 `TOOL_REQUEST`。

---

## 4. 编排层（youmi/coordinator/）

### 4.1 MasterAgent（master.py）

编排层顶层入口，负责接收用户任务、创建子 Agent、调度工作流：

```python
master = MasterAgent(config, global_memory=GlobalMemory(...))

# 从 YAML 目录加载（推荐）
master = MasterAgent.from_config_dir("youmi/agents/master/")
```

**核心方法**：

| 方法 | 说明 |
|------|------|
| `await run(task)` | 单次任务执行 |
| `await conversation_loop()` | 多轮对话入口（持续接收新任务） |
| `create_sub_agent(name, role, task, ...)` | 子 Agent 工厂 |
| `reset_for_new_task()` | 新任务前重置工具权限与状态 |
| `await on_stop()` | 任务结束，触发 PostTaskPipeline |

**三级审批**：

| 级别 | 触发条件 | 处理方式 |
|------|---------|---------|
| AUTO（自动）| 工具在 `auto_approve_list` 中 | 立即批准 |
| MANUAL（人工）| 工具在 `sensitive_tools` 中 | 加入 `manual_review_queue`，等待人工 |
| MASTER（Master 审批）| 其他工具申请 | MasterAgent 内置工具 `approve/deny_tool_request` 决策 |

### 4.2 WorkflowPlan / WorkflowExecutor（plan.py）

```python
plan = WorkflowPlan(name="myflow")
plan.add_step(step_id="A", agent_id="agent_1", depends_on=[])
plan.add_step(step_id="B", agent_id="agent_2", depends_on=["A"])

executor = WorkflowExecutor(plan)
results = await executor.run(mode="parallel")  # "serial" | "parallel"
```

- `WorkflowPlan.validate()` 检测循环依赖（DAG 校验）
- `WorkflowExecutor` 按拓扑序分层，同层步骤并行执行

### 4.3 HandoffProtocol（handoff.py）

Agent 间任务委派协议，`HandoffMessage` 携带：`from_agent_id / to_agent_id / task / context / callback_required`。

### 4.4 ToolGuardianAgent（tool_guardian.py）

专职工具问题诊断与修复：

1. 接收子 Agent 上报的 `ToolIssueReport`
2. 调用 `_load_tool_knowledge(tool_name)` 从 GlobalMemory 查历史经验
3. 调用 `FixStrategiesMixin._generate_fix()` 生成修复方案（LLM 路径注入历史经验段）
4. 执行 MCPServer 工具描述更新或代码建议
5. 调用 `_persist_fix_to_memory()` 写回 BUG_FIX，标记历史问题 resolved

内置工具 `search_tool_experience`：ToolGuardian LLM 可主动检索全局记忆。

### 4.5 PostTaskPipeline（post_task.py）

任务结束后后台流水线，4 个阶段：

| 阶段 | 说明 |
|------|------|
| 1. 工具统计 | 统计工具调用成功率，生成 `ToolExperienceSummary` |
| 2. 任务摘要 | LLM 生成任务摘要，写入 MasterAgent 记忆 |
| 3. ToolGuardian 汇报 | 失败工具 + 低成功率工具上报 ToolGuardian |
| 4. GlobalMemory 沉淀 | `update_global_memory()`：写 TOOL_EXPERIENCE + 高失败率分析 + 阈值版本更新 |

### 4.6 SubProcessAgentRunner（subprocess_agent.py）

进程隔离执行器，`SubProcessAgentRunner` + `SubProcessHandle`，基于 `asyncio.create_subprocess_exec`，通过临时 JSON 文件传递配置与结果。

---

## 5. MCP 工具层（youmi/mcp/）

### 5.1 四层架构

```
LocalFunctionProvider / BuiltinToolProvider
  → MCPServer（统一 JSON-RPC 路由）
       → MCPClient（进程内客户端）
            → ToolBridge（Agent 侧适配器）
                 └─ AgentToolContext（Agent 侧三级状态）
                      └─ ToolVault（共享内存 + ToolStore 持久化）
```

### 5.2 ToolVault 三级状态（vault.py）

| 状态 | 含义 | 向量化 |
|------|------|------|
| HOT | 当前 LLM 上下文中（已展开工具定义） | ✅ |
| WARM | 最近使用，保留在内存 | ✅ |
| COLD | 已从上下文移除，仅保留元数据 | metadata only |

主要方法：`add_tool()` / `search(query, top_k)` / `get_hot_tools()` / `set_temperature()`。

### 5.3 ToolStore（tool_store.py）

SQLite + sqlite-vec 持久化层，6 张表：

| 表名 | 说明 |
|------|------|
| `tools` | 工具元数据 + 版本链（version / parent_version_id） |
| `vec_tools` | 工具向量索引（sqlite-vec） |
| `tool_changelogs` | 同版本内 bug 修复说明 |
| `tool_aliases` | 工具别名 |
| `tool_tags` | 工具标签 |
| `tool_dependencies` | 工具依赖关系 |

关键接口：`create_version()` / `get_version_chain()` / `search_by_embedding()` / `trigger_version_update()`。

### 5.4 AgentToolContext（context.py）

Agent 侧三级状态视图，与共享 ToolVault 解耦：

```python
# 通过 ToolBridge.attach_vault() 自动初始化
bridge.attach_vault(shared_vault)
# → 创建 AgentToolContext，白名单/协调器工具自动标记 HOT
# → 每个 Agent 拥有独立的 HOT/WARM/COLD 视图
```

### 5.5 审批（approval.py / tool_approval.py）

`ApprovalManager` 统一管理三级审批决策与审计日志：

```python
decision = await approval_mgr.request_approval(
    agent_id, tool_name, reason, sensitivity
)
# → ApprovalDecision: AUTO_APPROVED / PENDING_MANUAL / MASTER_DECISION
```

`ToolApprovalMixin` 混入 MasterAgent，全部审批路径经 ApprovalManager，`get_approval_audit_log()` 提供审计记录。

### 5.6 内置工具（youmi/tools/builtin.py）

`BuiltinToolProvider` 注册 9 个工具 + 1 个兜底工具：

| 工具名 | 说明 | 模块 |
|--------|------|------|
| `file_read` | 读取文件内容 | file_ops |
| `file_write` | 写入/追加文件 | file_ops |
| `file_list` | 列出目录内容 | file_ops |
| `shell_exec` | 执行 shell 命令（含超时） | shell_ops |
| `web_fetch` | 抓取网页内容 | web_ops |
| `data_parse` | 解析 JSON/CSV 数据 | data_ops |
| `data_transform` | 数据转换操作 | data_ops |
| `approve_tool_request` | 批准工具申请（MasterAgent 专用） | coordinator_ops |
| `deny_tool_request` | 拒绝工具申请（MasterAgent 专用） | coordinator_ops |
| `search_new_tools` | 在 ToolVault 中语义搜索新工具（兜底） | agent.py |

---

## 6. 记忆系统（youmi/memory/）

### 6.1 三层架构

```
MemoryManager（统一接口）
  ├─ MemoryStrategy（会话内上下文管理）
  │    ├─ FullMemoryStrategy    — 完整保留
  │    ├─ SummaryMemoryStrategy — 超窗口时 LLM 压缩为摘要
  │    └─ LSTMMemoryStrategy    — 双通道（长期+短期）
  ├─ PersistenceBackend（跨任务持久化）
  │    ├─ SQLiteBackend
  │    └─ FileBackend
  └─ ContextCompactor（超 token 时自动压缩，独立于策略）
```

### 6.2 核心接口

```python
mem = MemoryManager(agent_id, memory_config)
await mem.initialize()

await mem.on_message(role, content)        # 写入消息
ctx  = await mem.get_context()             # 获取 LLM 上下文列表
hits = await mem.search(query, top_k=5,    # 语义检索（有 embedding → 向量；无 → 关键词）
                         embedding_client=ec)
await mem.on_session_end()                 # 会话结束，自动持久化
```

### 6.3 ContextCompactor

```python
compactor = ContextCompactor(max_tokens=4000, llm_call=async_fn)
messages = await compactor.maybe_compact(messages)
# 超限时将早期消息替换为 LLM 生成的摘要消息
```

---

## 7. 全局知识层（youmi/knowledge/）

专供 `ToolGuardianAgent` 诊断修复使用；子 Agent **不消费**全局记忆。

### 7.1 数据模型

- `KnowledgeCategory`：`TOOL_EXPERIENCE` / `TASK_PATTERN` / `BUG_FIX`
- `KnowledgeEntry`：12 字段（含 `resolved` / `resolution` 修复闭环字段）
- `ToolKnowledge`：工具经验聚合视图（`best_practices` / `known_issues` / `fix_history`）

### 7.2 GlobalMemory 核心接口

```python
gm = GlobalMemory(db_path=".youmi_knowledge.db", embedding_client=ec)
await gm.add_experience(tool_name, content, category, success_rate=0.6)
results  = await gm.search(query, tool_name="file_read", top_k=5)
knowledge = await gm.get_tool_knowledge("file_read")  # → ToolKnowledge
await gm.mark_resolved(entry_id, resolution)
```

### 7.3 ToolExperienceExtractor

双模式失败分析：

1. **LLM 增强模式**：将失败对话发给 LLM，获取结构化根因分析
2. **规则降级模式**：`_ERROR_RULES` 6 类关键词模板（missing_target / permission / timeout / invalid_params / encoding / network）

---

## 8. 消息总线（youmi/bus/）

### 8.1 核心模型

```python
@dataclass
class WorkflowMessage:
    msg_id:      str               # UUID
    workflow_id: str               # 工作流隔离键
    msg_type:    WorkflowMessageType
    from_agent:  str
    to_agent:    str               # "" = 广播
    payload:     dict
    timestamp:   float
```

6 种消息类型：`TASK` / `RESULT` / `STATUS` / `TOOL_REQUEST` / `TOOL_RESPONSE` / `FEEDBACK`。

### 8.2 InProcessBroker

进程内实现，至少一次投递语义，`asyncio.Queue` 驱动，`subscribe(agent_id, handler)` 注册消费者。

### 8.3 BusServer / BusClient

`BusServer` 提供 WebSocket 总线服务端，支持跨进程 Agent 通信。`BusClient` 提供断线重连的 WebSocket 客户端。

---

## 9. LLM 客户端（youmi/llm/）

### 9.1 LLMClient

```python
client = LLMClient(llm_config)
response = await client.chat(messages, tools=openai_tools_schema)
# 支持流式响应：async for chunk in client.stream_chat(messages)
```

支持：OpenAI API / Ollama / 任意兼容 API（通过 `base_url` 配置）。

### 9.2 EmbeddingClient

```python
ec = EmbeddingClient(config)
vectors = await ec.embed(texts)     # 批量向量化
vec     = await ec.embed_one(text)  # 单条向量化
# 失败时抛出异常，上层捕获后降级到关键词搜索
```

---

## 10. PromptAssembler（youmi/core/prompt.py）

分层 Prompt 组装，支持 token 预算控制：

```python
assembler = PromptAssembler(token_budget=6000)
assembler.add_layer(PromptLayer.SYSTEM, system_prompt)
assembler.add_layer(PromptLayer.TOOL_CONTEXT, tool_descriptions)
assembler.add_layer(PromptLayer.MEMORY, context_messages)
assembler.add_layer(PromptLayer.TASK, user_message)

messages = assembler.build()  # 超预算时丢弃低优先级 layer
```

---

## 11. Plugin 系统（youmi/core/plugin.py）

```python
class Plugin(ABC):
    @abstractmethod
    async def on_agent_start(self, agent): ...
    async def on_agent_stop(self, agent): ...
    async def on_tool_call(self, agent, tool_name, args): ...

agent.plugin_manager.register(MyPlugin())
```

---

## 12. HeartbeatScheduler（youmi/scheduler/__init__.py）

```python
scheduler = HeartbeatScheduler(interval=5.0)
scheduler.add_task(ScheduledTask(name="heartbeat", fn=check_fn, interval=5.0))
await scheduler.start()
```

---

## 13. GUI 层（gui/）

> 详见 [GUI_Introduction.md](details/GUI_Introduction.md)

基于 aiohttp 的 Web 应用，通过 `EngineBridge` 无侵入桥接引擎：

```
浏览器 (HTML/CSS/JS 三栏布局)
  │ WebSocket + REST
gui/server.py (aiohttp)
  ├─ EngineBridge   — 引擎适配器（MCP + Bus + Hook 初始化）
  ├─ GUIHooks       — 监听 Hook 推送 WS 事件
  ├─ MCPService     — MCP + ToolStore + ToolVault 一体化
  ├─ WebSocketHub   — 连接管理（广播/定向推送）
  └─ Store          — JSON 持久化（gui/data/）
```

启动：`python -m gui`（默认端口 8000）

---

## 14. 目录结构（真实）

```
youmi/
├── core/         — Agent 基类、Hook、Plugin、Prompt、Tool 执行器、类型定义
├── coordinator/  — MasterAgent、ToolGuardian、WorkflowPlan、Handoff、PostTaskPipeline
├── mcp/          — Server/Client/Bridge、ToolVault、ToolStore、Context、Approval
├── memory/       — MemoryManager、Strategy（Full/Summary/LSTM）、Backend（SQLite/File）、Compactor
├── knowledge/    — GlobalMemory、KnowledgeEntry、ToolExperienceExtractor
├── tools/        — BuiltinToolProvider 及 9 个工具实现
├── bus/          — WorkflowMessage、InProcessBroker、BusServer、BusClient
├── llm/          — LLMClient、EmbeddingClient
├── scheduler/    — HeartbeatScheduler
└── agents/       — YAML 角色配置（master/、tool_guardian/）
gui/
├── engine/       — EngineBridge、GUIHooks、MCPService、Models、WorkflowTracker
├── hub/          — WebSocketHub、events
├── persistence/  — Store（JSON 持久化）
└── static/       — 前端（index.html / app.js / chat-renderer.js / ...）
```

---

## 15. 术语表

| 术语 | 定义 |
|------|------|
| **Agent** | 由 LLM 驱动的自主实体，具备角色、工具与记忆，通过 ReAct 循环执行任务 |
| **MasterAgent** | 编排层顶层 Agent，负责任务分析、子 Agent 管理、工作流调度 |
| **ToolGuardianAgent** | 专职工具守护 Agent，接收工具问题上报并执行修复 |
| **ToolVault** | 工具热温冷三级内存池，支持向量语义搜索 |
| **ToolStore** | SQLite + sqlite-vec 持久化工具库，含版本链与变更日志 |
| **AgentToolContext** | Agent 侧独立的工具三级状态视图，与共享 Vault 解耦 |
| **GlobalMemory** | 跨任务工具经验知识库（SQLite + 向量），仅供 ToolGuardian 消费 |
| **PostTaskPipeline** | 任务结束后后台 4 阶段流水线（统计/摘要/汇报/沉淀） |
| **WorkflowPlan** | Agent 步骤 DAG 执行计划，支持串行/并行/依赖 |
| **InProcessBroker** | 进程内异步消息总线，隔离不同工作流消息 |
| **HookRegistry** | Agent Hook 注册表，7 个挂载点 + 3 个 Decision 类型 |
| **ContextCompactor** | 超 token 预算时自动压缩历史消息为摘要 |
| **EngineBridge** | GUI 层引擎适配器，无侵入桥接核心引擎 |
