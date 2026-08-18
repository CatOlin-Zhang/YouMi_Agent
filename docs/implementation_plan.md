# YouMi Agent 实施计划

> 基于 `docs/structure.md` 协作架构设计，结合当前代码库状态，制定的分阶段实施计划。
> 新增：OpenClaw 对标差距分析（OC-1 ~ OC-10），识别 YouMi 与成熟 Agent 框架的功能差距。
>
> 最后更新：2026-08-18

---

## 当前已完成

> 总体完成度约 **60%**。核心框架（Agent 基类 + MCP + 记忆 + 消息总线 + 内置工具 + MasterAgent + ToolGuardian + GUI）已完整可用。

### 基础框架层

| 模块 | 文件 | 状态 |
|------|------|------|
| Agent 基类（状态机、ReAct 循环） | `youmi/core/agent.py` | ✅ 已完成 |
| Agent env 属性（逻辑工作目录） | `youmi/core/agent.py` | ✅ 已完成 |
| 工具定义与注册表 | `youmi/core/tool.py` | ✅ 已完成 |
| 基础类型（LLMConfig、MemoryConfig 等） | `youmi/core/types.py` | ✅ 已完成 |
| LLM HTTP 客户端（Ollama/OpenAI 兼容） | `youmi/llm/client.py` | ✅ 已完成 |
| Agent connect_mcp() 双模式 | `youmi/core/agent.py` | ✅ 已完成 |
| Agent 配置目录模块 | `youmi/agents/` | ✅ 已完成 |

### MCP 协议层

| 模块 | 文件 | 状态 |
|------|------|------|
| MCP 协议层（JSON-RPC 2.0） | `youmi/mcp/protocol.py` | ✅ 已完成 |
| ToolProvider ABC + LocalFunctionProvider | `youmi/mcp/provider.py` | ✅ 已完成 |
| MCPServer 统一路由 | `youmi/mcp/server.py` | ✅ 已完成 |
| MCPClient 进程内客户端 | `youmi/mcp/client.py` | ✅ 已完成 |
| ToolBridge 权限 + 路由 | `youmi/mcp/bridge.py` | ✅ 已完成 |

### 记忆系统

| 模块 | 文件 | 状态 |
|------|------|------|
| 记忆基类与管理器 | `youmi/memory/base.py` / `memory.py` | ✅ 已完成 |
| Full 记忆策略 | `youmi/memory/strategies/full.py` | ✅ 已完成 |
| Summary 记忆策略 | `youmi/memory/strategies/summary.py` | ✅ 已完成 |
| LSTM 记忆策略 | `youmi/memory/strategies/lstm.py` | ✅ 已完成 |

### 消息总线（Phase 1）

| 模块 | 文件 | 状态 |
|------|------|------|
| 消息总线 WebSocket 实现 | `youmi/bus/` | ✅ 已完成 |
| Agent connect_bus() 消息总线集成 | `youmi/core/agent.py` | ✅ 已完成 |

### 内置工具（Phase 2）

| 模块 | 文件 | 状态 |
|------|------|------|
| BuiltinToolProvider（9 个内置工具） | `youmi/tools/builtin.py` | ✅ 已完成 |
| 文件操作工具 | `youmi/tools/file_ops.py` | ✅ 已完成 |
| Shell 执行工具 | `youmi/tools/shell_ops.py` | ✅ 已完成 |
| 网页抓取工具 | `youmi/tools/web_ops.py` | ✅ 已完成 |
| 数据工具（datetime/json） | `youmi/tools/data_ops.py` | ✅ 已完成 |
| 协调器操作工具 | `youmi/tools/coordinator_ops.py` | ✅ 已完成 |

### 协调层（Phase 3 部分）

| 模块 | 文件 | 状态 |
|------|------|------|
| MasterAgent（任务分析、子 Agent 实例化、编排） | `youmi/coordinator/master.py` | ✅ 已完成 |
| ToolGuardianAgent（工具记忆守护） | `youmi/coordinator/tool_guardian.py` | ✅ 已完成 |
| WorkflowPlan 工作流计划模型 | `youmi/coordinator/plan.py` | ❌ 未实现 |
| WorkflowExecutor 工作流执行器 | `youmi/coordinator/executor.py` | ❌ 未实现 |

### GUI

| 模块 | 文件 | 状态 |
|------|------|------|
| 群聊式 Streamlit GUI（656 行） | `gui/streamlit_app.py` | ✅ 已完成 |

---

## Phase 1：消息总线与多 Agent 通信 ✅ 已完成

> 对应 structure.md §5.1 — 共享消息空间的并发与状态一致性

### 目标

建立 Agent 间可靠的消息通信基础设施，支持多 Agent 协作工作流。

### 已实现文件

| 文件 | 职责 | 状态 |
|------|------|------|
| `youmi/bus/message.py` | `WorkflowMessage` + `BusEnvelope` + `WorkflowMessageType` | 已完成 |
| `youmi/bus/broker.py` | `MessageBroker` ABC + `InProcessBroker` (asyncio.Queue) | 已完成 |
| `youmi/bus/server.py` | `BusServer` — WebSocket 服务端 | 已完成 |
| `youmi/bus/ws_client.py` | `BusClient` — WebSocket 客户端（含断线重连） | 已完成 |
| `youmi/bus/__init__.py` | 模块导出 | 已完成 |

### 改造文件

| 文件 | 变更 | 状态 |
|------|------|------|
| `youmi/core/agent.py` | 新增 `connect_bus()` / `wait_for_message()` / `bus` 属性，`send_message` 委托 Broker | 已完成 |
| `youmi/__init__.py` | 新增 bus 模块导出 | 已完成 |

### 实现特性

- `MessageBroker` ABC + `InProcessBroker`（asyncio.Queue）+ `BusClient`（WebSocket）三种实现
- 消息类型：`task` / `feedback` / `status` / `query`，task/feedback 写入记忆
- 按 `workflow_id` 隔离消息通道
- 点对点投递和广播
- at-least-once 投递语义（ACK 机制）
- 心跳保活、断线重连
- WebSocket 客户端与进程内 Agent 混合通信

### 测试

- `tests/test_message_bus.py` — 36 个测试全部通过

---

## Phase 2：内置基础工具（BuiltinToolProvider） ✅ 已完成

> 对应 structure.md §3 — Agent 初始工具

### 目标

提供内置工具集，Agent 实例化时默认赋予。实际实现已超出原计划（3 个 → 9 个工具）。

### 已实现文件

| 文件 | 职责 | 状态 |
|------|------|------|
| `youmi/tools/__init__.py` | 模块导出 | ✅ 已完成 |
| `youmi/tools/builtin.py` | `BuiltinToolProvider` — 继承 `LocalFunctionProvider`，预注册 9 个工具 | ✅ 已完成 |
| `youmi/tools/file_ops.py` | `file_search` / `file_read` / `file_write` / `list_directory` / `text_search` | ✅ 已完成 |
| `youmi/tools/shell_ops.py` | `shell_exec` — 沙箱化命令执行 | ✅ 已完成 |
| `youmi/tools/web_ops.py` | `web_fetch` — 网页内容抓取 | ✅ 已完成 |
| `youmi/tools/data_ops.py` | `get_datetime` / `json_tool` | ✅ 已完成 |
| `youmi/tools/coordinator_ops.py` | 协调器操作工具 | ✅ 已完成 |

### 改造文件

| 文件 | 变更 | 状态 |
|------|------|------|
| `youmi/core/agent.py` | `connect_mcp()` 时自动注册 `BuiltinToolProvider` 到 MCPServer | ✅ 已完成 |

### 内置工具清单

| 工具名 | 功能 |
|--------|------|
| `file_search` | 按 glob 模式搜索文件 |
| `file_read` | 读取文件内容（支持行号范围） |
| `file_write` | 写入/创建文件（覆盖/追加/仅创建） |
| `list_directory` | 列出目录内容 |
| `text_search` | 文件内容搜索（类 grep） |
| `shell_exec` | 沙箱化命令执行 |
| `web_fetch` | 网页内容抓取 |
| `get_datetime` | 获取当前日期时间 |
| `json_tool` | JSON 解析/格式化/校验 |

### 关键设计

- `BuiltinToolProvider` 继承现有 `LocalFunctionProvider`，与 MCP 层无缝集成
- 文件操作限定在工作目录（可配置沙箱路径）内，防止越权访问
- `file_write` 支持创建/覆盖/追加三种模式
- 所有文件操作记录审计日志
- 支持 `exclude` 参数按需排除特定工具

### 测试

- `tests/test_builtin_tools.py` — 文件 CRUD、路径越权拒绝、沙箱隔离

---

## Phase 3：Master Agent 与动态编排 ⚠️ 部分完成

> 对应 structure.md §1 — 主 Agent 实例化子 Agent

### 目标

实现 Master Agent 的任务分析、子 Agent 实例化、工作流编排能力。

### 已实现文件

| 文件 | 职责 | 状态 |
|------|------|------|
| `youmi/coordinator/__init__.py` | 模块导出 | ✅ 已完成 |
| `youmi/coordinator/master.py` | `MasterAgent` — 继承 Agent，任务分析 + 子 Agent 管理（382 行） | ✅ 已完成 |
| `youmi/coordinator/tool_guardian.py` | `ToolGuardianAgent` — 工具记忆守护 Agent（775 行） | ✅ 已完成 |
| `youmi/agents/master/config.yaml` | MasterAgent 配置 | ✅ 已完成 |
| `youmi/agents/tool_guardian/config.yaml` | ToolGuardianAgent 配置 | ✅ 已完成 |

### 未实现文件

| 文件 | 职责 | 状态 |
|------|------|------|
| `youmi/coordinator/plan.py` | `WorkflowPlan` — 独立的工作流计划模型（子 Agent 列表、依赖关系、工具权限） | ❌ 未实现 |
| `youmi/coordinator/executor.py` | `WorkflowExecutor` — 按计划实例化 Agent、分配任务、收集结果 | ❌ 未实现 |

### 改造文件

| 文件 | 变更 | 状态 |
|------|------|------|
| `youmi/core/agent.py` | 新增 `create_sub_agent()` 工厂方法 | ✅ 已完成 |

### 关键设计

- `MasterAgent` 接收用户任务后，调用 LLM 生成任务拆解方案
- 支持从 `youmi/agents/<role>/config.yaml` 加载子 Agent 配置，或参数构造默认配置
- `SubAgentRecord` 记录子 Agent 运行状态
- `ToolGuardianAgent` 收集工具调用问题汇报，自动修正工具描述

### 待补充

- `WorkflowPlan`：当前编排逻辑内联在 MasterAgent 中，需拆分为独立的计划模型
- `WorkflowExecutor`：需实现独立的执行器，支持串行链和并行扇出

### 测试

- `tests/test_master_agent.py` — 计划生成、串行执行、并行执行、错误处理
- `tests/test_tool_guardian.py` — 工具问题汇报、描述修正、修改历史追溯

---

## Phase 4：渐进式工具暴露与向量搜索 ❌ 未实现

> 对应 structure.md §2, §5.2 — 工具发现与冷启动防护

### 目标

实现基于向量搜索的工具发现机制，以及兜底工具防死锁。

### 待新增文件

| 文件 | 职责 | 状态 |
|------|------|------|
| `youmi/mcp/discovery.py` | `ToolDiscovery` — 工具向量索引 + 自然语言搜索 | ❌ 未实现 |
| `youmi/mcp/approval.py` | `ToolApproval` — 工具申请审批流（自动/人工） | ❌ 未实现 |

### 待改造文件

| 文件 | 变更 | 状态 |
|------|------|------|
| `youmi/mcp/server.py` | MCPServer 新增 `search_tools(query)` 方法 | ❌ 未实现 |
| `youmi/mcp/bridge.py` | ToolBridge 新增 `request_expansion(query)` 方法 | ❌ 未实现 |
| `youmi/mcp/provider.py` | 新增 `ToolDocProvider` — 管理工具说明文档的存储和检索 | ❌ 未实现 |

### 关键设计

- 工具说明文档使用 embedding 向量化后存入内存索引（初期用简单的 TF-IDF，后续可替换为 FAISS/chromadb）
- `ToolBridge.request_expansion("我需要一个能发邮件的工具")` → 向量搜索 → 返回候选工具列表 → Agent 选择 → 动态挂载到 `allowed_tools`
- 兜底工具 `ask_master_agent`：当 Agent 判定当前工具无法完成任务时，向 Master Agent 发送 query 类型消息请求帮助
- 兜底工具 `search_new_tools`：Agent 主动向 MCP 层发起工具搜索

### 依赖

- 新增依赖：`scikit-learn`（TF-IDF）或 `chromadb`（向量数据库）

### 测试

- `tests/test_tool_discovery.py` — 向量搜索命中率、审批流程、兜底工具触发

---

## Phase 5：外部 Skill 导入 ❌ 未实现

> 对应 structure.md §4 — 工作流类和工具调用类 Skill

### 目标

实现外部 Skill 包的标准化导入，支持工作流类和工具调用类两种模式。

### 待新增文件

| 文件 | 职责 | 状态 |
|------|------|------|
| `youmi/skills/__init__.py` | 模块导出 | ❌ 未实现 |
| `youmi/skills/loader.py` | `SkillLoader` — Skill 发现、加载、分类 | ❌ 未实现 |
| `youmi/skills/manifest.py` | `SkillManifest` — Skill 清单模型（名称、类型、工具列表、入口文件） | ❌ 未实现 |
| `youmi/skills/docgen.py` | `ToolDocGenerator` — 调用 LLM 为工具函数自动生成说明文档 | ❌ 未实现 |

### 关键设计

- Skill 目录结构约定：
  ```
  skills/
  └── my_skill/
      ├── manifest.yaml    # Skill 清单
      ├── tools/           # 工具函数定义（工具调用类）
      └── workflow.py      # 工作流定义（工作流类）
  ```
- `manifest.yaml` 定义 Skill 类型、工具列表、依赖关系
- 工具调用类导入时：解析函数签名 → LLM 生成说明文档 → 存入 MCP 工具库
- Ground Truth 保留：原始函数签名 + docstring 与 LLM 文档一起存储
- 导入后可选运行单元测试验证工具正确性

### 测试

- `tests/test_skill_import.py` — 清单解析、文档生成、工具注册、工作流加载

---

## Phase 6：全局知识库 ❌ 未实现

> 对应 structure.md §5.4 — 跨 Agent 知识沉淀

### 目标

实现工作流级别的经验沉淀和跨 Agent 知识注入。

### 待新增文件

| 文件 | 职责 | 状态 |
|------|------|------|
| `youmi/knowledge/__init__.py` | 模块导出 | ❌ 未实现 |
| `youmi/knowledge/base.py` | `GlobalKnowledgeBase` — 全局知识存储与检索 | ❌ 未实现 |
| `youmi/knowledge/extractor.py` | `KnowledgeExtractor` — 从 Agent 执行结果中提取关键经验 | ❌ 未实现 |

### 待改造文件

| 文件 | 变更 | 状态 |
|------|------|------|
| `youmi/coordinator/master.py` | 工作流结束后调用 `KnowledgeExtractor` 提取经验写入知识库 | ❌ 未实现 |
| `youmi/coordinator/executor.py` | 创建子 Agent 时从知识库注入相关经验到 system_prompt | ❌ 未实现（依赖 executor.py 先实现） |

### 关键设计

- 知识条目结构：`{topic, content, source_agent_id, workflow_id, timestamp, tags}`
- `KnowledgeExtractor` 调用 LLM 从 TaskResult + conversation 中提取关键信息
- 检索方式：按 tags 过滤 + 关键词匹配（后续可升级为向量搜索）
- 注入时机：`WorkflowExecutor` 创建子 Agent 时，将相关知识拼接到 `system_prompt`

### 测试

- `tests/test_knowledge_base.py` — 知识写入/检索、自动提取、注入验证

---

## Phase 7：层级架构 ❌ 未实现

> 对应 structure.md §5.5 — 主 Agent 单点故障与过载

### 目标

支持 Master → Sub-Master → Worker 三层架构，分担编排压力。

### 待改造文件

| 文件 | 变更 | 状态 |
|------|------|------|
| `youmi/coordinator/master.py` | 支持 `MasterAgent` 创建 `SubMasterAgent` 分担编排 | ❌ 未实现 |
| `youmi/coordinator/plan.py` | `WorkflowPlan` 支持嵌套子计划（树形结构） | ❌ 未实现（依赖 plan.py 先实现） |

### 关键设计

- `WorkflowPlan` 从扁平列表升级为树形结构，每个节点可以是 Worker Agent 或 Sub-Plan
- Sub-Master Agent 本质上是拥有编排能力的特殊 Agent，持有局部 Plan
- 层级深度可配置（默认最多 2 层），防止无限嵌套

### 测试

- `tests/test_hierarchy.py` — 嵌套计划、Sub-Master 编排、深度限制

---

## 实施优先级总览

```
Phase 1: 消息总线        ✅ 已完成
   ↓
Phase 2: 内置工具        ✅ 已完成（超出原计划，9 个工具）
   ↓
Phase 3: Master Agent    ⚠️ 部分完成（缺 plan.py + executor.py）
   ↓
Phase 4: 工具发现        ❌ 未实现  ← 依赖 Phase 2 (MCP 扩展)
   ↓
Phase 5: Skill 导入      ❌ 未实现  ← 依赖 Phase 4 (ToolDocProvider)
   ↓
Phase 6: 全局知识库      ❌ 未实现  ← 依赖 Phase 3 (WorkflowExecutor)
   ↓
Phase 7: 层级架构        ❌ 未实现  ← 依赖 Phase 3 + 6 (最终优化)
```

---

## 未实现功能总览

### Phase 3 遗留项（优先级最高）

| 缺失项 | 说明 | 阻塞关系 |
|--------|------|----------|
| `youmi/coordinator/plan.py` | `WorkflowPlan` 独立工作流计划模型，支持串行/并行/条件分支 | 阻塞 Phase 6、Phase 7 |
| `youmi/coordinator/executor.py` | `WorkflowExecutor` 独立执行器，按计划编排子 Agent | 阻塞 Phase 6 |

### Phase 4 — 工具发现与向量搜索

| 缺失项 | 说明 |
|--------|------|
| `youmi/mcp/discovery.py` | 工具向量索引 + 自然语言搜索（TF-IDF / FAISS / chromadb） |
| `youmi/mcp/approval.py` | 工具申请审批流（自动/人工） |
| 兜底工具 `ask_master_agent` | Agent 工具不足时向 Master 求助 |
| 兜底工具 `search_new_tools` | Agent 主动搜索新工具 |
| MCPServer `search_tools()` | 工具搜索接口 |
| ToolBridge `request_expansion()` | 工具扩展请求 |
| `ToolDocProvider` | 工具说明文档管理 |

### Phase 5 — 外部 Skill 导入

| 缺失项 | 说明 |
|--------|------|
| `youmi/skills/` 整个模块 | Skill 加载器、清单模型、文档自动生成 |
| Skill 目录结构规范 | `manifest.yaml` + `tools/` + `workflow.py` |

### Phase 6 — 全局知识库

| 缺失项 | 说明 |
|--------|------|
| `youmi/knowledge/` 整个模块 | 全局知识存储、检索、经验提取 |
| MasterAgent 知识注入 | 工作流结束后提取经验，创建子 Agent 时注入 |

### Phase 7 — 层级架构

| 缺失项 | 说明 |
|--------|------|
| Sub-Master 支持 | Master → Sub-Master → Worker 三层编排 |
| WorkflowPlan 树形结构 | 支持嵌套子计划 |

### 非功能需求差距

| 需求（来自 requirements.md） | 状态 | 说明 |
|------------------------------|------|------|
| 工具调用超时控制与重试 (F3.7) | ⚠️ 部分 | `shell_exec` 有超时，其他工具未统一 |
| 记忆向量检索 (F4.3) | ❌ 未实现 | 当前为 full/summary/lstm 策略，无语义搜索 |
| 记忆可插拔后端 (F4.6) | ❌ 未实现 | 仅内存实现，无 SQLite/Redis 持久化 |
| Skill/Tool 版本管理与兼容性 (F2.7) | ❌ 未实现 | |
| Agent 运行时迁移与持久化 | ❌ 待定 | 非 MVP，暂停/恢复能力 |
| OpenTelemetry 追踪 | ❌ 未实现 | 日志有，但无分布式追踪 |
| 安全加固（认证、加密） | ❌ 未实现 | 无身份认证、传输加密、加密存储 |
| MCP Server 高可用 (NF2.2) | ❌ 未实现 | 单实例，无故障恢复 |

---

## OpenClaw 对标差距分析

> 基于 OpenClaw（160K+ GitHub Stars，Node.js 独立 Agent 应用）的架构设计，
> 对比 YouMi Agent（Python Agent 框架库）识别出的功能差距。
>
> OpenClaw 设计哲学："配置优于编码"（SOUL.md 声明式 Agent 定义）
> YouMi 设计哲学："代码驱动 + Pydantic 强类型"
>
> 两者定位不同，以下仅列出 YouMi 可借鉴并值得迁移的能力。

### OC-1: 上下文压缩（Compaction）— P0

**OpenClaw 实现：**
- 自动检测上下文窗口接近 token 上限时触发 compaction
- 将旧消息摘要化，保留关键信息，释放 token 空间
- 支持 `before_compaction` / `after_compaction` 钩子
- compaction 失败可自动 retry

**YouMi 现状：**
- `SummaryMemoryStrategy` 有 LLM 摘要能力，但没有**自动触发的上下文压缩机制**
- `_observe()` 直接将完整 `_conversation` 传给 LLM，无 token 预算管控
- 长对话必然超出 context window 导致失败

**待实现：**

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `youmi/core/agent.py` | `_observe()` 新增 token 计数 + compaction 触发逻辑 | P0 |
| `youmi/memory/compaction.py` | `ContextCompactor` — 上下文压缩引擎（摘要化旧消息、token 预算管理） | P0 |
| `youmi/memory/memory.py` | `MemoryManager` 新增 `compact()` 方法，供 Agent 自动调用 | P0 |
| `youmi/core/types.py` | `LLMConfig` 新增 `max_context_tokens` / `compaction_reserve_ratio` 字段 | P0 |

**关键设计：**
- token 计数使用 `tiktoken` 或简单的字符估算（1 token ≈ 2-4 字符）
- 压缩策略：保留最近 N 条消息不变，将更早的消息通过 LLM 摘要压缩
- 压缩前触发 `before_compaction` 钩子，允许插件干预
- 压缩失败时 fallback 到截断最早消息

---

### OC-2: Session 持久化 — P0

**OpenClaw 实现：**
- SQLite 存储 session transcript（完整对话记录）
- session 跨重启持久化，Agent 重启后可恢复上下文
- writer claim 机制防止并发写入冲突
- 支持多 session 管理（sessionKey/sessionId）

**YouMi 现状：**
- 记忆策略（Full/Summary/LSTM）均为**内存存储**
- Agent 重启后所有对话历史和记忆丢失
- 没有 session 持久化层

**待实现：**

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `youmi/memory/backends/` | 新模块 — 持久化后端 ABC + SQLite/JSON 文件实现 | P0 |
| `youmi/memory/backends/base.py` | `PersistenceBackend` ABC — save/load/delete session | P0 |
| `youmi/memory/backends/sqlite_backend.py` | `SQLiteBackend` — SQLite 持久化 | P0 |
| `youmi/memory/backends/file_backend.py` | `FileBackend` — JSON 文件持久化（轻量替代） | P0 |
| `youmi/memory/memory.py` | `MemoryManager` 接受 `backend` 参数，自动持久化 | P0 |
| `youmi/core/agent.py` | `initialize()` 时自动恢复上次 session | P0 |

**关键设计：**
- `PersistenceBackend` ABC：`save(session_id, messages)` / `load(session_id)` / `list_sessions()` / `delete(session_id)`
- SQLite 使用 `aiosqlite` 异步驱动
- 每条消息带 `session_id` + `timestamp` + `role` + `content`
- Agent `initialize()` 时检查是否有可恢复的 session
- 与现有 `MemoryStrategy` 解耦：策略负责"怎么记"，后端负责"存哪里"

---

### OC-3: 定时/主动调度（Heartbeat Scheduler）— P1

**OpenClaw 实现：**
- `HEARTBEAT.md` 定义定时任务（类似 cron，但用自然语言描述）
- 每 30 分钟自动唤醒 Agent，检查待处理任务、执行定时技能
- Agent 可以**主动行动**，而非只被动等待用户输入

**YouMi 现状：**
- 完全没有调度机制，Agent 只在 `run()` / `chat_turn()` 被调用时执行
- 没有 cron/heartbeat/定时器相关代码

**待实现：**

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `youmi/scheduler/__init__.py` | 新模块 — 调度器 | P1 |
| `youmi/scheduler/heartbeat.py` | `HeartbeatScheduler` — 基于 asyncio 的定时唤醒 | P1 |
| `youmi/scheduler/task.py` | `ScheduledTask` — 定时任务定义（cron 表达式 / 间隔 / 自然语言） | P1 |
| `youmi/core/agent.py` | Agent 新增 `schedule_task()` / `on_heartbeat()` 接口 | P1 |

**关键设计：**
- `HeartbeatScheduler` 管理一个 asyncio 事件循环定时器
- 定时任务定义：`{name, interval_seconds, task_description, enabled}`
- 唤醒时调用 `Agent.run(task_description)` 或自定义 `on_heartbeat()` 钩子
- 支持 YAML 配置定时任务（`heartbeat.yaml` 或在 `config.yaml` 中声明）
- 任务执行结果写入记忆，下次唤醒时可参考历史

---

### OC-4: Agent 间任务委派（Handoff / Delegation）— P1

**OpenClaw 实现：**
- SOUL.md 中通过 `## Handoffs` 声明委派规则
- Agent 通过 `@mention` 将任务转交给其他 Agent
- Gateway 内部路由消息，无需外部通信

**YouMi 现状：**
- `MasterAgent` 可以创建和运行子 Agent，但是**单向的命令式调度**
- 子 Agent 之间不能直接通信（需要通过 MasterAgent 中转）
- `_Thought.action_type` 有 `"delegate"` 枚举值，但未实现
- 消息总线支持 `to_agent_id` 路由，但没有 Agent 间的 handoff 协议

**待实现：**

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `youmi/core/agent.py` | 实现 `delegate` action_type 的处理逻辑 | P1 |
| `youmi/core/types.py` | 新增 `HandoffRule` 模型（触发条件、目标 Agent、消息模板） | P1 |
| `youmi/coordinator/handoff.py` | 新文件 — `HandoffProtocol` — Agent 间任务委派协议 | P1 |
| `youmi/bus/broker.py` | Broker 新增 handoff 消息路由（`to_agent_id` 自动匹配） | P1 |

**关键设计：**
- `AgentConfig` 新增 `handoff_rules: list[HandoffRule]` 声明委派规则
- `_think()` 返回 `action_type="delegate"` 时，Agent 通过消息总线将任务转交
- 目标 Agent 收到 handoff 消息后自动开始处理
- 委派结果通过 `feedback` 消息回传给发起方
- 支持链式委派（A → B → C），但设最大深度限制防止循环

---

### OC-5: Hook / 插件系统 — P2

**OpenClaw 实现：**
- 丰富的 Plugin Hooks：`before_model_resolve`、`before_prompt_build`、
  `before_tool_call`、`after_tool_call`、`message_received`、`message_sending` 等
- 内部 Hooks：`agent:bootstrap`、命令钩子
- 插件通过 SDK 注册，可以拦截、修改、替换 Agent 行为的任何阶段

**YouMi 现状：**
- Agent 生命周期钩子（`on_initialize/on_start/on_stop/on_destroy`）仅覆盖生命周期边界
- 没有 `before_tool_call` / `after_tool_call` 工具拦截
- 没有 `before_prompt_build` prompt 注入点
- 没有插件注册机制

**待实现：**

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `youmi/core/hooks.py` | 新文件 — `HookRegistry` + 钩子类型定义 | P2 |
| `youmi/core/agent.py` | ReAct 循环各阶段插入钩子调用点 | P2 |
| `youmi/core/plugin.py` | 新文件 — `Plugin` ABC + `PluginManager` | P2 |

**关键设计：**
- 钩子类型：`before_prompt_build`、`before_tool_call`、`after_tool_call`、
  `before_model_call`、`after_model_call`、`message_received`、`message_sending`
- 每个钩子支持 `block` / `modify` / `pass` 三种决策
- `Plugin` ABC：`name` + `hooks: dict[HookType, handler]` + `setup()` / `teardown()`
- `PluginManager` 管理插件注册/卸载/优先级
- 钩子按优先级链式调用，`block` 终止后续链

---

### OC-6: System Prompt 动态组装 — P2

**OpenClaw 实现：**
- System prompt 由多层拼接：base prompt + skills prompt + bootstrap context + per-run overrides
- 模型特定的 token 限制和 compaction reserve 自动计算
- 支持 bootstrap/context 文件注入

**YouMi 现状：**
- `AgentConfig.system_prompt` 是静态字符串
- `_observe()` 直接拼接 system_prompt + conversation，无动态组装
- 没有上下文文件注入、Skill prompt 注入

**待实现：**

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `youmi/core/prompt.py` | 新文件 — `PromptAssembler` — 分层 prompt 组装引擎 | P2 |
| `youmi/core/types.py` | 新增 `PromptLayer` 模型（name, content, priority, token_budget） | P2 |
| `youmi/core/agent.py` | `_observe()` 改为调用 `PromptAssembler` 组装 system prompt | P2 |

**关键设计：**
- Prompt 分层：`base`（Agent 身份）→ `skills`（可用 Skill 说明）→ `context`（上下文文件/知识库摘要）→ `runtime`（运行时注入的额外指令）→ `overrides`（per-run 覆盖）
- 每层有 `priority`（高优先级层不被低优先级截断）和 `token_budget`
- `PromptAssembler.assemble(layers, max_tokens)` 按优先级截断/压缩
- 与 OC-1（Compaction）协同：compaction 只压缩 conversation，不动 system prompt

---

### OC-7: 外部通信渠道（Channels）— P3

**OpenClaw 实现：**
- 内置 Telegram / WhatsApp / Discord / Slack / Signal / iMessage 集成
- Agent 通过 Channel 接收和发送消息
- 每个 Channel 有独立的 bot token 配置

**YouMi 现状：**
- 消息总线仅支持进程内 + WebSocket
- 没有外部消息平台集成
- Streamlit GUI 是唯一的用户界面

**待实现：**

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `youmi/channels/__init__.py` | 新模块 — Channel 适配器 | P3 |
| `youmi/channels/base.py` | `Channel` ABC — `start()` / `stop()` / `send()` / `on_message()` | P3 |
| `youmi/channels/telegram.py` | `TelegramChannel` — Telegram Bot API 集成 | P3 |
| `youmi/channels/discord.py` | `DiscordChannel` — Discord Bot 集成 | P3 |

**关键设计：**
- `Channel` ABC 统一接口：`start()` / `stop()` / `send_message()` / `on_message(callback)`
- Channel 收到消息后转换为 `WorkflowMessage` 发布到消息总线
- Agent 回复通过消息总线路由回 Channel 发送
- 每个 Channel 独立配置（bot token、webhook URL 等）

---

### OC-8: 工具执行审批（Exec Approvals）— P3

**OpenClaw 实现：**
- Shell 命令执行前有审批门控（approve/deny）
- 支持人工审批和自动审批策略
- 工具调用前后有 `before_tool_call` / `after_tool_call` 拦截点

**YouMi 现状：**
- `ToolBridge` 有白名单管控（`allowed_tools`）
- `ToolGuardian` 做事后错误分析和修正
- 但没有工具执行前的审批/确认机制

**待实现：**

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `youmi/core/types.py` | 新增 `ApprovalPolicy` 枚举（auto/manual/deny） | P3 |
| `youmi/mcp/bridge.py` | `ToolBridge` 新增审批拦截逻辑 | P3 |
| `youmi/core/agent.py` | `_act()` 调用前检查审批策略 | P3 |

**关键设计：**
- 每个工具可配置审批策略：`auto`（自动通过）/ `manual`（需人工确认）/ `deny`（禁止）
- `shell_exec` 默认 `manual`，其他工具默认 `auto`
- manual 模式下：暂停执行 → 向 GUI/Channel 发送审批请求 → 等待人工 approve/deny
- 与 OC-5（Hook 系统）集成：通过 `before_tool_call` 钩子实现

---

### OC-9: 多模型 Provider 管理 — P4

**OpenClaw 实现：**
- 多 Provider Registry（Anthropic / OpenAI / Google / Ollama）
- 每个 Agent 可配置不同的模型
- 模型 fallback 链（主模型失败自动切换备选）
- provider 级别的超时和 idle watchdog

**YouMi 现状：**
- `LLMClient` 支持 OpenAI 兼容 API（覆盖大多数场景）
- 有 Ollama 集成测试
- 没有多 provider 注册表、fallback 链、provider 级别超时管理

**待实现：**

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `youmi/llm/providers.py` | 新文件 — `ProviderRegistry` + fallback 链管理 | P4 |
| `youmi/llm/client.py` | `LLMClient` 支持 fallback_providers 配置 | P4 |
| `youmi/core/types.py` | `LLMConfig` 新增 `fallback_models` / `provider_timeout` 字段 | P4 |

**关键设计：**
- `ProviderRegistry` 管理多个 provider 配置（每个 provider 是一个 `LLMConfig`）
- fallback 链：主模型调用失败 → 自动切换到下一个 fallback 模型
- provider 级别超时：`connect_timeout` / `read_timeout` / `idle_watchdog`
- 每个 Agent 可独立配置 provider，也可继承 MasterAgent 的 provider

---

### OC-10: Skill 系统增强（对标 SKILL.md）— P3

> 注：现有 Phase 5 已规划 Skill 导入，此处补充 OpenClaw 特有的设计要点。

**OpenClaw 特有设计：**
- `SKILL.md` 用 Markdown + frontmatter 定义 Skill，非开发者也能编辑
- Skill 在 prompt assembly 时自动注入 system prompt
- 社区 ClawHub 共享 13,700+ Skill

**与现有 Phase 5 的差异：**
- Phase 5 侧重 Python 代码级别的 Skill 导入（`manifest.yaml` + `tools/` + `workflow.py`）
- OpenClaw 的 SKILL.md 更轻量：纯 Markdown 指令，不需要 Python 代码
- 建议 YouMi 同时支持两种 Skill 格式：
  - **轻量 Skill**：纯 Markdown 指令文件（`.md`），注入到 system prompt
  - **代码 Skill**：Python 包（`manifest.yaml` + 工具函数），注册到 MCP

**待补充到 Phase 5：**

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `youmi/skills/markdown_skill.py` | 新文件 — Markdown Skill 解析器（frontmatter + 指令注入） | P3 |
| `youmi/core/prompt.py` | Prompt 组装时自动注入已加载的 Skill 指令 | P3（依赖 OC-6） |

---

## 合并优先级总览（原有 Phase + OpenClaw 对标）

```
P0 — 必须优先实现（Agent 可用性基础）:
  ├── OC-1  上下文压缩 (Compaction)        — 防止长对话崩溃
  └── OC-2  Session 持久化                 — 重启不丢失数据

P1 — 高价值能力（Agent 智能化升级）:
  ├── Phase 3 遗留  WorkflowPlan + Executor — 完善编排引擎
  ├── OC-3  定时/主动调度 (Heartbeat)       — Agent 主动行动
  └── OC-4  Agent 间 Handoff               — 多 Agent 真正协作

P2 — 可扩展性基础:
  ├── OC-5  Hook / 插件系统                — 第三方扩展点
  └── OC-6  System Prompt 动态组装          — prompt 质量提升

P3 — 按需实现:
  ├── Phase 4  工具发现与向量搜索           — 工具智能发现
  ├── Phase 5  Skill 导入 + Markdown Skill  — 工作流复用
  ├── OC-7   外部通信渠道                   — Telegram/Discord
  └── OC-8   工具执行审批                   — 安全加固

P4 — 长期优化:
  ├── Phase 6  全局知识库                   — 跨 Agent 经验沉淀
  ├── Phase 7  层级架构                     — 三层编排
  └── OC-9   多 Provider 管理               — 模型 fallback
```
