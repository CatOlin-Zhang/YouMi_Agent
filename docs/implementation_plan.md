# YouMi Agent 实施计划

> 基于 `docs/structure.md` 协作架构设计，结合当前代码库状态，制定的分阶段实施计划。
>
> 最后更新：2026-08-05

---

## 当前已完成

| 模块 | 文件 | 状态 |
|------|------|------|
| Agent 基类（状态机、ReAct 循环） | `youmi/core/agent.py` | 已完成 |
| Agent env 属性（逻辑工作目录） | `youmi/core/agent.py` | 已完成 |
| 工具定义与注册表 | `youmi/core/tool.py` | 已完成 |
| 基础类型（LLMConfig、MemoryConfig 等） | `youmi/core/types.py` | 已完成 |
| MCP 协议层（JSON-RPC 2.0） | `youmi/mcp/protocol.py` | 已完成 |
| ToolProvider ABC + LocalFunctionProvider | `youmi/mcp/provider.py` | 已完成 |
| MCPServer 统一路由 | `youmi/mcp/server.py` | 已完成 |
| MCPClient 进程内客户端 | `youmi/mcp/client.py` | 已完成 |
| ToolBridge 权限 + 路由 | `youmi/mcp/bridge.py` | 已完成 |
| LLM HTTP 客户端 | `youmi/llm/client.py` | 已完成 |
| 记忆系统（full/summary/lstm） | `youmi/memory/` | 已完成 |
| Agent connect_mcp() 双模式 | `youmi/core/agent.py` | 已完成 |
| Agent 配置目录模块 | `youmi/agents/` | 已完成 |
| MasterAgent 基础实现 | `youmi/coordinator/master.py` | 已完成（Phase 3 部分） |
| 消息总线 WebSocket 实现 | `youmi/bus/` | 已完成（Phase 1） |
| Agent connect_bus() 消息总线集成 | `youmi/core/agent.py` | 已完成（Phase 1） |

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

## Phase 2：内置基础工具（BuiltinToolProvider）

> 对应 structure.md §3 — Agent 初始工具

### 目标

提供文件搜索、读取、写入三个内置工具，Agent 实例化时默认赋予。

### 新增文件

| 文件 | 职责 |
|------|------|
| `youmi/tools/__init__.py` | 模块导出 |
| `youmi/tools/builtin.py` | `BuiltinToolProvider` — 继承 `ToolProvider`，预注册基础工具 |
| `youmi/tools/file_ops.py` | `file_search` / `file_read` / `file_write` 函数实现 |

### 改造文件

| 文件 | 变更 |
|------|------|
| `youmi/core/agent.py` | `connect_mcp()` 时自动注册 `BuiltinToolProvider` 到 MCPServer |

### 关键设计

- `BuiltinToolProvider` 继承现有 `ToolProvider` ABC，与 `LocalFunctionProvider` 同级
- 文件操作限定在工作目录（可配置沙箱路径）内，防止越权访问
- `file_write` 支持创建/覆盖/追加三种模式
- 所有文件操作记录审计日志

### 测试

- `tests/test_builtin_tools.py` — 文件 CRUD、路径越权拒绝、沙箱隔离

---

## Phase 3：Master Agent 与动态编排

> 对应 structure.md §1 — 主 Agent 实例化子 Agent

### 目标

实现 Master Agent 的任务分析、子 Agent 实例化、工作流编排能力。

### 新增文件

| 文件 | 职责 |
|------|------|
| `youmi/coordinator/__init__.py` | 模块导出 |
| `youmi/coordinator/master.py` | `MasterAgent` — 继承 Agent，覆写 `_think()` 实现任务分解 |
| `youmi/coordinator/plan.py` | `WorkflowPlan` — 工作流计划模型（子 Agent 列表、依赖关系、工具权限） |
| `youmi/coordinator/executor.py` | `WorkflowExecutor` — 按计划实例化 Agent、分配任务、收集结果 |

### 改造文件

| 文件 | 变更 |
|------|------|
| `youmi/core/agent.py` | 新增 `create_sub_agent(config, bridge, broker)` 工厂方法 |

### 关键设计

- `MasterAgent` 接收用户任务后，调用 LLM 生成 `WorkflowPlan`（JSON 格式）
- Plan 包含：子 Agent 列表、每个 Agent 的角色/prompt/allowed_tools、执行顺序（串行/并行）
- `WorkflowExecutor` 按 Plan 创建 Agent 实例 → `initialize()` → `run()` → 收集 `TaskResult`
- 支持串行链（A → B → C）和并行扇出（A → [B, C, D] → 聚合）
- Master Agent 保持轻量状态，仅持有 Plan + 各子 Agent 的 TaskResult

### 测试

- `tests/test_master_agent.py` — 计划生成、串行执行、并行执行、错误处理

---

## Phase 4：渐进式工具暴露与向量搜索

> 对应 structure.md §2, §5.2 — 工具发现与冷启动防护

### 目标

实现基于向量搜索的工具发现机制，以及兜底工具防死锁。

### 新增文件

| 文件 | 职责 |
|------|------|
| `youmi/mcp/discovery.py` | `ToolDiscovery` — 工具向量索引 + 自然语言搜索 |
| `youmi/mcp/approval.py` | `ToolApproval` — 工具申请审批流（自动/人工） |

### 改造文件

| 文件 | 变更 |
|------|------|
| `youmi/mcp/server.py` | MCPServer 新增 `search_tools(query)` 方法 |
| `youmi/mcp/bridge.py` | ToolBridge 新增 `request_expansion(query)` 方法 |
| `youmi/mcp/provider.py` | 新增 `ToolDocProvider` — 管理工具说明文档的存储和检索 |

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

## Phase 5：外部 Skill 导入

> 对应 structure.md §4 — 工作流类和工具调用类 Skill

### 目标

实现外部 Skill 包的标准化导入，支持工作流类和工具调用类两种模式。

### 新增文件

| 文件 | 职责 |
|------|------|
| `youmi/skills/__init__.py` | 模块导出 |
| `youmi/skills/loader.py` | `SkillLoader` — Skill 发现、加载、分类 |
| `youmi/skills/manifest.py` | `SkillManifest` — Skill 清单模型（名称、类型、工具列表、入口文件） |
| `youmi/skills/docgen.py` | `ToolDocGenerator` — 调用 LLM 为工具函数自动生成说明文档 |

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

## Phase 6：全局知识库

> 对应 structure.md §5.4 — 跨 Agent 知识沉淀

### 目标

实现工作流级别的经验沉淀和跨 Agent 知识注入。

### 新增文件

| 文件 | 职责 |
|------|------|
| `youmi/knowledge/__init__.py` | 模块导出 |
| `youmi/knowledge/base.py` | `GlobalKnowledgeBase` — 全局知识存储与检索 |
| `youmi/knowledge/extractor.py` | `KnowledgeExtractor` — 从 Agent 执行结果中提取关键经验 |

### 改造文件

| 文件 | 变更 |
|------|------|
| `youmi/coordinator/master.py` | 工作流结束后调用 `KnowledgeExtractor` 提取经验写入知识库 |
| `youmi/coordinator/executor.py` | 创建子 Agent 时从知识库注入相关经验到 system_prompt |

### 关键设计

- 知识条目结构：`{topic, content, source_agent_id, workflow_id, timestamp, tags}`
- `KnowledgeExtractor` 调用 LLM 从 TaskResult + conversation 中提取关键信息
- 检索方式：按 tags 过滤 + 关键词匹配（后续可升级为向量搜索）
- 注入时机：`WorkflowExecutor` 创建子 Agent 时，将相关知识拼接到 `system_prompt`

### 测试

- `tests/test_knowledge_base.py` — 知识写入/检索、自动提取、注入验证

---

## Phase 7：层级架构

> 对应 structure.md §5.5 — 主 Agent 单点故障与过载

### 目标

支持 Master → Sub-Master → Worker 三层架构，分担编排压力。

### 改造文件

| 文件 | 变更 |
|------|------|
| `youmi/coordinator/master.py` | 支持 `MasterAgent` 创建 `SubMasterAgent` 分担编排 |
| `youmi/coordinator/plan.py` | `WorkflowPlan` 支持嵌套子计划（树形结构） |

### 关键设计

- `WorkflowPlan` 从扁平列表升级为树形结构，每个节点可以是 Worker Agent 或 Sub-Plan
- Sub-Master Agent 本质上是拥有编排能力的特殊 Agent，持有局部 Plan
- 层级深度可配置（默认最多 2 层），防止无限嵌套

### 测试

- `tests/test_hierarchy.py` — 嵌套计划、Sub-Master 编排、深度限制

---

## 实施优先级总览

```
Phase 1: 消息总线        ← 多 Agent 协作基石
   ↓
Phase 2: 内置工具        ← 独立可用，无依赖
   ↓
Phase 3: Master Agent    ← 依赖 Phase 1 (MessageBroker)
   ↓
Phase 4: 工具发现        ← 依赖 Phase 2 (MCP 扩展)
   ↓
Phase 5: Skill 导入      ← 依赖 Phase 4 (ToolDocProvider)
   ↓
Phase 6: 全局知识库      ← 依赖 Phase 3 (WorkflowExecutor)
   ↓
Phase 7: 层级架构        ← 依赖 Phase 3 + 6 (最终优化)
```

> Phase 1 和 Phase 2 无互相依赖，可以并行开发。
