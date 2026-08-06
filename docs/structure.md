# YouMi Agent 协作架构设计

> 本文档描述框架的高层协作模式与关键设计决策，面向多 Agent 动态编排场景。

---

## 1. 主 Agent 与子 Agent 实例化

系统初始仅有一个 **主 Agent（Master Agent）**，负责根据用户任务需求动态实例化多个子 Agent。

### 实例化流程

1. **工作流分析** — 主 Agent 调用 LLM 分析任务，确定：
   - 所需子 Agent 数量
   - 每个 Agent 的核心功能定位
   - 每个 Agent 需要的工具权限范围
2. **实例创建** — 系统根据分析结果创建 Agent 实例，通过 MCP 层授予对应的工具暴露权限
3. **权限隔离** — 每个子 Agent 仅能看到其被授权使用的工具（通过 `ToolBridge.allowed_tools` 白名单实现）

### 通信机制

- **共享消息空间** — Agent 间的工作交接和工作流通信通过共享空间传递
- **定向发送** — 消息必须指定发送对象（如 AgentA → AgentB）
- **独立记忆** — 各 Agent 的记忆处于自己的独立空间，允许同一工作流上的 Agent 使用不同的记忆策略

#### 消息投递模型

- **投递方式**：采用 **push 模型** — MessageBroker 主动将消息推入目标 Agent 的接收队列（`asyncio.Queue`），接收方被动接收
- **可靠性保证**：采用**至少一次投递**语义 — Broker 在接收方 ACK（即 `receive_message()` 成功返回）后才标记消息已消费；若 Agent 异常，消息保留在队列中待恢复后重新投递
- **广播范围**：`to_agent_id=None` 的广播消息**仅发给同一 `workflow_id` 下的所有 Agent**，不跨工作流传播

> **未来扩展**：支持中途切换记忆方式，提供不同预置方案的记忆传递工具。

### 子 Agent 生命周期管理

#### 工厂创建

子 Agent 通过 Master Agent 调用**安全的工厂方法**创建：

1. Master Agent 的 LLM 输出结构化的 `WorkflowPlan`（JSON），包含子 Agent 的配置
2. `WorkflowExecutor` 校验 Plan 合法性（工具名是否存在、权限是否合理）后调用 `Agent(config)` 实例化
3. 禁止任意代码注入 — Plan 中只允许声明式配置（角色名、prompt、工具白名单），不允许嵌入可执行代码

#### 自动销毁与回收

- 工作流正常结束后，`WorkflowExecutor` 按**逆创建顺序**逐个调用子 Agent 的 `destroy()` 方法
- 工作流异常终止时，Executor 在 `finally` 块中确保所有子 Agent 被销毁
- 可选：引入 `AgentPool` 复用机制 — 对于高频角色（如 Coder、Reviewer），销毁前保存配置快照，下次优先复用

#### 失败重试

- 子 Agent 执行失败后，Master Agent 根据 `RetryPolicy` 决定是否重试
- 重试时可选择：原样重跑 / 更换工具集 / 降级到更简单的子任务
- 最大重试次数由 `WorkflowPlan` 中的 `max_retries` 字段控制（默认 1 次）

---

## 2. 工具调用与权限

### MCP 服务器

项目内置一个 MCP 服务器，Agent 通过 MCP 协议调用工具，无需编写脚本。

### 超级工具上下文控制

与传统 Skill 式的工具说明不同，YouMi Agent 使用**渐进式工具暴露**：

- 每个工具拥有独立的说明文档
- 当 Agent 申请调用时，工具说明才复制到 Agent 的工作内存中
- Agent 在运行过程中可以以 order 的形式向 MCP 服务层申请扩展工具调用范围

### 自然语言工具发现

MCP 层可接收来自 Agent 的自然语言描述，通过**向量搜索**匹配工具库中的工具，返回结果后询问 Agent 需要注册哪几个工具。

### 工具权限动态扩展与回收

#### 热更新时序

Agent 在 ReAct 循环运行时申请新工具，流程如下：

1. Agent 调用兜底工具 `search_new_tools(query)` → ToolBridge 发起申请
2. MCP 层搜索匹配 → 返回候选工具列表
3. 审批通过后，`ToolBridge.add_allowed_tool(name)` 立即生效
4. **下一轮 `_think()`** 调用时，`to_openai_tools()` 自动包含新工具 — LLM 即可感知

> 当前 `_think()` 每轮都重新获取 tools schema，因此热更新天然兼容，无需中断 ReAct 循环。

#### 审批决策模型

| 审批模式 | 触发条件 | 决策者 |
|---------|---------|--------|
| 自动审批 | 工具在预定义的「可扩展清单」内 | MCP 层自动通过 |
| 人工审批 | 工具不在可扩展清单内，或涉及敏感操作 | 暂停 Agent，等待用户确认 |
| Master 审批 | 子 Agent 申请的工具超出其授权范围 | Master Agent 决策 |

#### 权限回收策略

- **工作流级回收**：工作流结束后，所有子 Agent 动态申请的工具权限**自动重置**（恢复为初始 `allowed_tools`）
- **Agent 级回收**：Agent 销毁时，其 ToolBridge 一并释放
- **持久化工具权限**（可选）：Master Agent 可以将验证过的工具权限写入全局知识库，下次创建同类 Agent 时自动赋予

---

## 3. Agent 初始工具

基础工具在 Agent 实例化完成时默认赋予，包含：

| 工具 | 说明 |
|------|------|
| `file_search` | 文件搜索 |
| `file_read` | 文件读取 |
| `file_write` | 文件写入 |

---

## 4. 外部 Skill 导入

外部 Skill 包通过以下流程兼容加入YouMi_Agent 的 MCP 层：

### 工作流类 Skill

无 tool call，仅定义某种工作方式（如 CoT、ReAct 模板等）。

- 保存整个 Skill 目录到本地
- 当需要时，以一个 Agent 的形式加载该 Skill，实例化为一个 Agent

### 工具调用类 Skill

有自定义的工具定义函数。

- 导入时调用 LLM 为每个 tool 生成说明文档，存入 MCP 层数据库，标记原始 Skill 来源
- 两种调用模式：
  - **动态加载** — 某个功能需求直接命中外部工具时，动态加载该 tool 到对应 Agent 的工作内存
  - **完整加载** — 用户要完整启动某个外部导入 Skill 时，以 Skill 为索引拉取完整 tool 列表到对应 Agent

### Skill 导入安全性

外部 Skill 包含可执行的 Python 函数，直接导入存在安全风险，需从以下方面防护：

| 风险 | 防护措施 |
|------|----------|
| **恶意代码执行** | Skill 工具函数在**沙箱环境**中执行 — 限制文件/网络/系统调用权限 |
| **依赖冲突** | 每个 Skill 的依赖声明在 `manifest.yaml` 中，导入时校验版本兼容性 |
| **命名空间污染** | 工具命名规范：`{skill_name}.{tool_name}`（如 `web_search.google_search`），MCPServer 按 Provider 隔离路由 |
| **资源滥用** | Skill 工具执行设置超时上限（默认 30s），超出则强制终止并记录异常 |

---

## 5. 已识别问题与解决方案

### 5.1 共享消息空间的并发与状态一致性

**挑战**：多 Agent 同时向共享空间写入消息，或 AgentB 还在处理任务时 AgentA 又发来新消息，如何保证消息时序和状态不混乱？

**解决方案**：
- 引入**消息总线（Message Broker）**，为每个工作流分配唯一 `workflow_id`
- 消息体包含严格元数据：`{sender_id, receiver_id, timestamp, msg_type, payload}`
- 增加**状态机**管理子 Agent 生命周期（`PENDING → RUNNING → WAITING_FOR_INPUT → COMPLETED / FAILED`），主 Agent 根据状态机决定下一步调度

#### 消息过滤与记忆隔离

当前 `receive_message()` 会将所有收到的消息写入 MemoryManager，大量无关消息会**污染 Agent 的独立记忆空间**。

**解决方案**：
- **消息分类标记**：消息元数据增加 `msg_type` 枚举（`task` / `feedback` / `status` / `query`）
- **过滤策略**：仅 `task` 和 `feedback` 类型消息写入记忆；`status` 和 `query` 类型消息仅放入队列供主动查询，不进入记忆上下文
- **可选覆盖**：Agent 可通过 `AgentConfig.extra["memory_filter"]` 自定义过滤规则（如「所有消息都记录」或「仅记录来自 Master 的消息」）

### 5.2 渐进式暴露的冷启动与死锁

**挑战**：Agent 当前内存中没有合适工具，且向量搜索也未命中，Agent 可能陷入"不知道该怎么办"的死循环。

**解决方案**：
- 为每个 Agent 提供**兜底工具**（如 `ask_master_agent` / `search_new_tools`），当工具无法完成任务时主动向上层求助或触发工具检索
- 设计**工具申请审批流**：Agent 提交申请 → MCP 层自动/人工审核 → 动态挂载（详见 §2 审批决策模型）
- **死锁检测**：Agent 连续 N 次（默认 3）`_reflect()` 判定"无进展"时，强制触发 `ask_master_agent` 兜底，由上层决定是否终止或调整任务

### 5.3 LLM 生成工具说明文档的准确性与幻觉

**挑战**：用 LLM 为外部 Skill 写说明文档，如果生成的文档有误导性，会导致后续 Agent 调用时参数传错。

**解决方案**：
- 导入 Skill 时，除 LLM 生成文档外，自动运行**单元测试验证**
- 将原始代码/函数签名作为 **Ground Truth（基准事实）** 与 LLM 文档一起存入向量库，Agent 调用不确定时可回溯查看原始签名

### 5.4 记忆空间的跨 Agent 知识沉淀

**挑战**：子 Agent 记忆独立，工作流结束后宝贵经验（如某个 API 的正确调用方式）就丢失了。

**解决方案**：
- 设计**全局知识库（Global Knowledge Base）**
- 子 Agent 完成任务或遇到错误时，主 Agent 提取关键信息（用户偏好、新发现的 API 用法等）写入全局知识库
- 下次实例化类似 Agent 时，自动注入这些全局经验

#### 知识冲突解决

多个 Agent 可能对同一主题产生矛盾知识（如 Agent A 认为 API X 用 POST，Agent B 认为用 GET）：

- **版本优先**：知识条目携带 `timestamp`，默认以**最新验证通过的条目**为准
- **置信度标记**：每条知识附带 `confidence` 评分（来源：执行成功 +1，执行失败 -1），低置信度条目自动降级
- **人工兜底**：高冲突条目（同一主题存在 2+ 条矛盾记录）标记为「待验证」，下次工作流中提醒用户确认

#### 知识过期与清理

- **TTL 机制**：每条知识设置过期时间（默认 30 天），过期后自动降级为「参考」状态，不再自动注入
- **版本追踪**：知识条目记录 `source_workflow_id`，当关联的外部资源（API 版本、工具版本）更新时，批量失效相关条目
- **粒度定义**：知识以**单条经验**为最小单位（如「API X 的正确调用方式」），而非整个工作流日志

### 5.5 主 Agent 的单点故障与过载

**挑战**：所有调度、分析、实例化都由主 Agent 完成，任务极其复杂时主 Agent 自身的上下文也会爆掉。

**解决方案**：
- 主 Agent 保持**无状态或轻量状态**，只负责宏观路由
- 引入**层级架构（Hierarchical Architecture）**：
  - Master Agent → Sub-Master Agent（负责某一类任务的编排）→ Worker Agent（负责具体执行）

### 5.6 工作流超时与熔断

**挑战**：子 Agent 可能陷入死循环或长时间无响应，导致整个工作流挂起。

**解决方案**：
- **工作流级超时**：`WorkflowPlan` 中设置 `timeout_seconds`（默认 300s），超时后 Executor 强制终止所有子 Agent
- **Agent 级超时**：单个 Agent 的 `run()` 方法已有 `max_iterations` 限制，额外增加 `max_wall_time`（墙钟时间）作为兜底
- **熔断机制**：连续 3 个子 Agent 执行失败或超时，触发熔断 — 暂停工作流，向用户报告并请求干预
- **心跳检测**：Executor 定期（每 10s）检查各子 Agent 的 `status` 和 `iteration_count`，若检测到停滞（连续 2 次心跳无进展），发出警告

### 5.7 级联失败处理

**挑战**：串行执行链 A → B → C 中，B 失败后 A 的结果是否有用？C 是否还要执行？

**解决方案**：
- `WorkflowPlan` 中每个步骤声明**失败策略**：

| 策略 | 行为 |
|------|------|
| `abort` | B 失败 → 整个工作流终止，返回已有结果（默认） |
| `skip` | B 失败 → 跳过 B，将 A 的输出直接传给 C |
| `retry` | B 失败 → 按 RetryPolicy 重试 B（最多 N 次） |
| `fallback` | B 失败 → 执行预定义的降级步骤 B' |

- **结果保留**：无论后续步骤是否失败，已完成步骤的 `TaskResult` 始终保留在 `WorkflowExecutor` 中，最终一并返回给用户
- **并行扇出失败**：并行执行 [B, C, D] 中部分失败时，采用**多数决**策略 — 超过半数成功则继续聚合，否则按串行失败处理

### 5.8 Token 预算与资源控制

**挑战**：多个子 Agent 并行执行时，每个都在调用 LLM，Token 消耗可能失控。

**解决方案**：
- **全局 Token 预算**：`WorkflowPlan` 中设置 `max_total_tokens`，Executor 在每次 LLM 调用前检查剩余额度
- **Agent 级预算**：`AgentConfig.extra["max_tokens"]` 限制单个 Agent 的最大 Token 消耗
- **预算分配**：Master Agent 根据子任务复杂度动态分配 Token 配额，预留 20% 作为应急储备
- **超预算处理**：Agent Token 耗尽时暂停执行，向 Master Agent 申请追加或降级为更轻量的模型
