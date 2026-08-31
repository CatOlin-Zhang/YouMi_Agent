# YouMi Agent 协作架构设计

> 本文档描述框架的高层协作模式与关键设计决策，面向多 Agent 动态编排场景。
> 标注 ✅ 的能力已在当前代码库实现，标注 🔲 的为规划中能力。

---

## 0. 真实模块结构（当前代码库）

```
YouMi_Agent/
├── youmi/                          # 主包
│   ├── __init__.py                 # 顶层导出（Agent/MasterAgent/MCPServer/GlobalMemory 等）
│   ├── agents/                     # Agent 角色配置
│   │   ├── master/config.yaml      # MasterAgent 默认配置
│   │   └── tool_guardian/config.yaml
│   ├── core/                       # 核心 Agent 框架
│   │   ├── agent.py                # Agent 基类（1700 行，ReAct 循环、Hook、状态机）
│   │   ├── hooks.py                # HookRegistry / HookType（7 个挂载点）
│   │   ├── plugin.py               # Plugin / PluginManager
│   │   ├── prompt.py               # PromptAssembler / PromptLayer
│   │   ├── tool.py                 # ToolDefinition / ToolRegistry
│   │   ├── mcp_integration.py      # MCP 集成 Mixin
│   │   ├── tool_executor.py        # 工具执行 Mixin
│   │   ├── models.py               # AgentStatus / TaskResult
│   │   └── types.py                # AgentConfig / LLMConfig / MemoryConfig 等
│   ├── coordinator/                # 任务编排与协调
│   │   ├── master.py               # MasterAgent（子 Agent 工厂、工作流编排）
│   │   ├── tool_guardian.py        # ToolGuardianAgent（工具问题守护 + 全局记忆闭环）
│   │   ├── post_task.py            # PostTaskPipeline（4 阶段后台流水线）
│   │   ├── fix_strategies.py       # FixStrategiesMixin（LLM/规则修复策略）
│   │   ├── plan.py                 # WorkflowPlan / WorkflowExecutor（DAG 调度）
│   │   ├── handoff.py              # HandoffProtocol（Agent 间任务委派）
│   │   ├── tool_approval.py        # ToolApprovalMixin（三级审批）
│   │   ├── subprocess_agent.py     # SubProcessAgentRunner（进程隔离）
│   │   └── _subprocess_entry.py    # 子进程入口
│   ├── mcp/                        # MCP 工具协议层
│   │   ├── server.py               # MCPServer（JSON-RPC 2.0 网关）
│   │   ├── client.py               # MCPClient（进程内客户端）
│   │   ├── bridge.py               # ToolBridge（权限白名单 + 工具上下文）
│   │   ├── vault.py                # ToolVault（内存缓存 + 向量搜索）
│   │   ├── tool_store.py           # ToolStore（SQLite + sqlite-vec 持久化版本管理）
│   │   ├── context.py              # AgentToolContext（Agent 侧三级状态）
│   │   ├── approval.py             # ApprovalManager（审批决策 + 审计日志）
│   │   ├── provider.py             # ToolProvider / LocalFunctionProvider
│   │   ├── protocol.py             # 协议类型（ToolIssueReport 等）
│   │   └── models.py               # MCP 数据模型
│   ├── memory/                     # 记忆系统
│   │   ├── memory.py               # MemoryManager（统一接口 + 向量检索）
│   │   ├── compaction.py           # ContextCompactor（token 超限自动压缩）
│   │   ├── strategies/
│   │   │   ├── full.py             # 完整记忆策略
│   │   │   ├── summary.py          # 摘要策略
│   │   │   └── lstm.py             # LSTM 双通道策略
│   │   └── backends/
│   │       ├── sqlite_backend.py   # SQLite 持久化后端
│   │       └── file_backend.py     # 文件系统后端
│   ├── knowledge/                  # 全局记忆（P6）
│   │   ├── global_memory.py        # GlobalMemory（SQLite + 向量检索）
│   │   ├── models.py               # KnowledgeEntry / KnowledgeCategory / ToolKnowledge
│   │   └── experience_extractor.py # ToolExperienceExtractor
│   ├── bus/                        # 消息总线
│   │   ├── broker.py               # MessageBroker ABC + InProcessBroker
│   │   ├── message.py              # WorkflowMessage / WorkflowMessageType
│   │   ├── server.py               # BusServer（WebSocket 服务端）
│   │   └── ws_client.py            # BusClient（WebSocket 客户端，含断线重连）
│   ├── llm/
│   │   ├── client.py               # LLMClient（httpx 异步 HTTP，兼容 OpenAI API）
│   │   └── embeddings.py           # EmbeddingClient（向量嵌入）
│   ├── tools/                      # 内置工具提供者
│   │   ├── builtin.py              # BuiltinToolProvider（9 个标准工具）
│   │   ├── file_ops.py / shell_ops.py / web_ops.py / data_ops.py
│   │   └── coordinator_ops.py      # MasterAgent 专用工具（create_sub_agent 等）
│   └── scheduler/
│       └── __init__.py             # HeartbeatScheduler / ScheduledTask
│
├── gui/                            # Web GUI（aiohttp REST + WebSocket）
│   ├── server.py                   # HTTP/WS 服务入口
│   ├── engine/
│   │   ├── bridge.py               # EngineBridge（引擎适配器）
│   │   ├── hook_bridge.py          # GUIHooks（Hook 注入）
│   │   ├── mcp_service.py          # MCPService（MCP+Vault 一体化）
│   │   ├── models.py               # Session/AgentCard/MessageRecord
│   │   └── tracker.py              # WorkflowTracker
│   ├── hub/
│   │   ├── events.py               # WebSocket 事件定义
│   │   └── ws_hub.py               # WebSocketHub
│   ├── persistence/store.py        # JSON 持久化
│   └── static/                     # 前端（三栏布局 · QQ/微信风格）
│
└── tests/                          # 25 个测试文件，467 个测试用例
```

---

## 1. 主 Agent 与子 Agent 实例化 ✅

系统初始仅有一个 **主 Agent（MasterAgent）**，负责根据用户任务需求动态实例化多个子 Agent。

### 实例化流程 ✅

1. **工作流分析** — MasterAgent 调用 LLM 分析任务，确定：
   - 所需子 Agent 数量
   - 每个 Agent 的核心功能定位（角色从 `youmi/agents/` 动态发现，无硬编码列表）
   - 每个 Agent 需要的工具权限范围
2. **实例创建** — 调用 `create_sub_agent(role, task, allowed_tools)` 实例化
3. **权限隔离** — 子 Agent 的 `ToolBridge.allowed_tools` 白名单控制可用工具范围
4. **自动接线** — `_patch_create_sub_agent()` 自动注入 MCPServer + 消息总线 + GUIHooks

### 通信机制 ✅

- **定向消息** — `send_message(to_agent_id, content, msg_type)` 发往指定 Agent
- **广播** — `to_agent_id=None` 广播给同 `workflow_id` 下所有 Agent
- **独立记忆** — 每个 Agent 的 `MemoryManager` 独立，支持不同记忆策略
- **工具申请** — SubAgent 通过 `TOOL_REQUEST` 消息向 Master 申请扩展工具

#### 消息投递模型 ✅

- **push 模型**：InProcessBroker 主动推入目标 Agent 的 `asyncio.Queue`
- **至少一次投递**：`receive_message()` 成功返回后才 ACK，异常时消息保留队列
- **工作流隔离**：`workflow_id` 过滤，广播不跨工作流

### 子 Agent 生命周期管理 ✅

#### 创建

子 Agent 通过 MasterAgent 内置工具 `create_sub_agent` 创建（LLM 直接调用），安全约束：

1. 声明式配置（角色名、prompt、工具白名单），禁止注入可执行代码
2. 工具名白名单校验（`WorkflowExecutor.validate()`）
3. 任务简报模板（`_TASK_BRIEF_TEMPLATE`）自动注入 SubAgent system prompt
4. 进程隔离选项（`SubProcessAgentRunner`，基于 `asyncio.create_subprocess_exec`）

#### 执行与销毁 ✅

- `run_sub_agent(agent_id)` / `run_all_sub_agents(parallel=True/False)` 驱动执行
- 工作流结束后 `on_stop()` 触发 PostTaskPipeline，然后逐个 `destroy()`
- `reset_for_new_task()` 重置状态、恢复工具权限

#### 失败重试 ✅（基础版）

- Agent ReAct 循环内置 `max_iterations` 硬限制
- WorkflowPlan 支持步骤级失败策略（`abort` / `skip` / `retry` / `fallback`）
- 🔲 高级：Saga/补偿事务、死信队列待实现

---

## 2. 工具调用与权限 ✅

### MCP 服务器 ✅

内置 MCPServer 采用 JSON-RPC 2.0 协议，`ToolProvider` 插件化注册。`LocalFunctionProvider` 将 Python 函数包装为 MCP 工具。

### 渐进式工具暴露（超级工具上下文）✅

每个 Agent 独立的 `AgentToolContext`（三级状态 HOT/WARM/COLD）：

- **HOT** — 在 LLM schema 中完整呈现
- **WARM** — 以摘要形式提示 LLM（降低 token 消耗）
- **COLD** — 已淘汰，不出现在 LLM 视野

工具申请时 `promote()` 升级状态，`recycle()` 按 idle_threshold 降级回收。

### 自然语言工具发现 ✅

`search_new_tools(query)` 兜底工具：

1. ToolVault 向量语义搜索（有 EmbeddingClient）
2. 关键词搜索降级（无 EmbeddingClient 或向量化失败）
3. 返回候选工具列表，Agent 选择后 `load_tool()` → HOT

🔲 召回确认闭环（Agent 与用户/Master 确认后再加载）待实现。

### 工具权限动态扩展与回收 ✅

| 审批模式 | 触发条件 | 决策者 |
|---------|---------|--------|
| 自动审批 | 工具在 `auto_approve_list` 内 | `ApprovalManager` 自动通过 |
| 人工审批 | 工具在 `sensitive_tools` 清单 | 进入 `manual_review_queue` 等待用户/GUI 确认 |
| Master 审批 | 超出 sensitive 范围 | MasterAgent LLM 决策 |

- 审批通过 → `ToolBridge.add_allowed_tool()` 立即生效，下一轮 `_think()` 自动包含
- 工作流结束 → `reset_tool_permissions()` 恢复初始 `allowed_tools`

---

## 3. Agent 初始工具 ✅

`register_builtin_tools()` 在连接 MCP 时默认注册 9 个内置工具：

| 工具 | 说明 |
|------|------|
| `file_search` | glob 文件搜索 |
| `file_read` | 文件读取（支持分段） |
| `file_write` | 文件写入（overwrite/append/create） |
| `list_directory` | 目录列表 |
| `text_search` | 正则/文本全局搜索 |
| `shell_exec` | Shell 命令执行（含超时） |
| `web_fetch` | HTTP 网页抓取 |
| `get_datetime` | 获取当前时间 |
| `json_tool` | JSON 格式化/校验/提取 |

---

## 4. 外部 Skill 导入 🔲

> 当前未实现（Phase 5 待开发）

### 工作流类 Skill 🔲

### 工具调用类 Skill 🔲

---

## 5. 已识别问题与解决方案

### 5.1 共享消息空间的并发与状态一致性 ✅

**解决方案（已实现）**：

- `InProcessBroker`（asyncio.Queue 进程内实现）保证消息顺序与线程安全
- 消息 `workflow_id` 隔离，不同工作流消息不互相干扰
- `WorkflowMessageType` 枚举区分消息类型：`task`/`feedback` 写入记忆，`status`/`query` 仅入队

### 5.2 渐进式暴露的冷启动与死锁 ✅

**解决方案（已实现）**：

- `search_new_tools` 兜底工具：Agent 工具不足时主动向 ToolVault 发起检索
- `_TaskSelfCheck`：`run()` 启动前评估工具充足性，提前触发检索
- ToolVault 三级状态 + recycle 防止工具无限堆积

### 5.3 LLM 生成工具说明文档的准确性 ✅（部分）

**解决方案（已实现）**：

- ToolStore 记录工具定义原始参数 Schema 作为 Ground Truth
- ToolGuardian 收集工具调用失败汇报，修正描述

### 5.4 记忆空间的跨 Agent 知识沉淀 ✅

**解决方案（已实现，P6）**：

- `GlobalMemory`：跨任务工具经验知识库（SQLite + 向量检索）
- `PostTaskPipeline.update_global_memory()`：任务结束自动沉淀
- `ToolGuardian` 接入全局记忆：修复前查历史经验，修复后写回 BUG_FIX + mark_resolved
- SubAgent **不注入**全局记忆（避免容量膨胀）

### 5.5 主 Agent 的单点故障与过载 🔲

**规划**：

- 引入**层级架构（Phase 7）**：Master → Sub-Master → Worker 三层
- Master 当前保持轻量：核心工作委托子 Agent，仅做宏观路由

### 5.6 工作流超时与熔断 🔲

**当前有**：`max_iterations` 硬限制、WorkflowPlan 步骤失败策略

**规划（P0 生产就绪）**：

- LLM 调用重试退避 + 熔断器（高失败率自动断路）
- 工作流级超时 + 心跳检测
- 持久化任务队列 + 断点续跑

### 5.7 级联失败处理 ✅（基础版）

**已实现**：WorkflowPlan 步骤失败策略（abort/skip/retry/fallback），`StepResult` 保留已完成步骤产出。

**规划**：Saga/补偿事务、死信队列。

### 5.8 Token 预算与资源控制 ✅（上下文层）

**已实现**：`ContextCompactor` token 超限自动压缩；`max_iterations` 限制 ReAct 循环。

**规划（P2）**：费用计量（按 Agent/任务维度）+ 模型路由（简单任务走小模型）。
