# YouMi Agent 实施计划

> 最后更新：2026-08-31
>
> 本文档已合并 `supplement_roadmap.md`（生产就绪缺口分析），形成统一的开发路线图。

---

## 总体状态

核心框架（Agent 基类、MCP 协议层、记忆系统、消息总线、内置工具、MasterAgent、ToolGuardian、Hook/插件、Prompt 动态组装、ToolVault 向量搜索、GUI、全局记忆）已具备可用雏形。

GUI 已与核心 MCP/Bus 层完成集成：所有 Agent 通过 MCPService 接入共享 MCPServer + ToolVault（sqlite-vec 方案），消息总线通过 InProcessBroker 互联。MasterAgent 提示词已清理硬编码角色列表，改为动态查询。

Phase 6 全局记忆已落地并完成闭环：任务结束后 PostTaskPipeline 自动沉淀工具使用经验到 GlobalMemory（SQLite + 向量检索），累计失败超阈值自动触发工具版本更新；ToolGuardian 接入全局记忆，修复前自动查询历史经验作为上下文，修复成功后写入 BUG_FIX 经验并标记历史问题 resolved。

但对照完整生命周期流程图，**编排层高级能力（层级架构）、Skill 导入**等高级能力尚未落地。

**总体完成度：约 82%**

**生产就绪度：较低** — 当前缺乏重试容错、安全加固、可观测性、部署形态等生产必需能力，需优先补齐。

---

## Phase 1：消息总线与多 Agent 通信

### 已实现

| 模块 | 文件 |
|------|------|
| WorkflowMessage / BusEnvelope / WorkflowMessageType | `youmi/bus/message.py` |
| MessageBroker ABC + InProcessBroker | `youmi/bus/broker.py` |
| BusServer WebSocket 服务端 | `youmi/bus/server.py` |
| BusClient WebSocket 客户端（含断线重连） | `youmi/bus/ws_client.py` |
| Agent connect_bus() / wait_for_message() / send_message | `youmi/core/agent.py` |

### 未实现

| 缺失项 | 说明 |
|--------|------|
| 外部通信渠道（Telegram/Discord/Slack 等） | 当前只有进程内 + WebSocket + Streamlit GUI |

---

## Phase 2：内置基础工具

### 已实现

| 模块                                                 | 文件 |
|----------------------------------------------------|------|
| BuiltinToolProvider（9 个内置工具 + search_new_tools 兜底） | `youmi/tools/builtin.py` |
| 文件操作工具                                             | `youmi/tools/file_ops.py` |
| Shell 执行工具                                         | `youmi/tools/shell_ops.py` |
| 网页抓取工具                                             | `youmi/tools/web_ops.py` |
| 数据工具                                               | `youmi/tools/data_ops.py` |
| 协调器操作工具                                            | `youmi/tools/coordinator_ops.py` |
| Agent connect_mcp() 自动注册 BuiltinToolProvider       | `youmi/core/agent.py` |

### 未实现

| 缺失项 | 说明 |
|--------|------|
| 统一的工具调用超时控制与重试 | 仅 `shell_exec` 有超时，其他工具未统一 |

---

## Phase 3：Master Agent 与动态编排

### 已实现

| 模块 | 文件 | 说明 |
|------|------|------|
| MasterAgent 用户对话入口 | `youmi/coordinator/master.py` | 负责“拦截”并分析用户消息 |
| MasterAgent 多 Agent 决策与实例化 | `youmi/coordinator/master.py` | 确定是否需要多个 SubAgent 以流水线或并行方式处理 |
| create_sub_agent() / run_sub_agent() | `youmi/coordinator/master.py` | 子 Agent 工厂方法 |
| SubAgentRecord | `youmi/coordinator/master.py` | 子 Agent 状态记录 |
| ToolGuardianAgent | `youmi/coordinator/tool_guardian.py` | 工具问题接收与描述修正 |
| WorkflowPlan + WorkflowExecutor | `youmi/coordinator/plan.py` | 工作流计划与执行（串行/并行/DAG） |
| HandoffProtocol | `youmi/coordinator/handoff.py` | Agent 间任务委派 |
| Agent 状态机 + ReAct 循环 | `youmi/core/agent.py` | Agent 生命周期与推理循环 |
| HeartbeatScheduler | `youmi/scheduler/__init__.py` | 定时调度 |
| **SubAgent prompt 级工作流自检** | `youmi/core/agent.py` | `_TaskSelfCheck` + `_self_check_task()` 在 run() 前评估工具充足性 |
| **任务简报模板注入** | `youmi/coordinator/master.py` | `_TASK_BRIEF_TEMPLATE` 注入到 SubAgent system prompt |
| **SubAgent 工具权限申请流程** | `youmi/core/agent.py`, `youmi/coordinator/master.py` | `_ToolRequest` + `request_tool()` + `_handle_tool_request()` |
| **TOOL_REQUEST/TOOL_RESPONSE 消息类型** | `youmi/bus/message.py` | 消息总线支持工具申请和响应 |
| **approve/deny_tool_request 工具** | `youmi/tools/coordinator_ops.py` | MasterAgent 可批准或拒绝工具申请 |
| **SubAgent 进程隔离** | `youmi/coordinator/subprocess_agent.py`, `youmi/coordinator/_subprocess_entry.py` | `SubProcessAgentRunner` + `SubProcessHandle`，基于 asyncio.create_subprocess_exec |
| **新任务循环** | `youmi/coordinator/master.py` | `conversation_loop()` + `reset_for_new_task()` 多轮任务支持 |
| **后台流水线** | `youmi/coordinator/post_task.py` | `PostTaskPipeline` 工具经验收集、任务摘要、ToolGuardian 汇报 |
| **search_new_tools 兆底工具** | `youmi/core/agent.py` | Agent 在 ReAct 循环中工具不足时主动搜索新工具（ToolVault 向量搜索 / ToolRegistry 回退） |
| **三级审批模型** | `youmi/coordinator/master.py` | 自动审批（auto_approve_list）+ 人工审批（sensitive_tools + manual_review_queue）+ Master 审批 |
| **ToolBridge 热更新集成** | `youmi/coordinator/master.py`, `youmi/mcp/bridge.py` | 审批通过后调用 `ToolBridge.add_allowed_tool()` 立即生效，下一轮 `_think()` 自动包含新工具 |
| **工作流级权限回收** | `youmi/core/agent.py`, `youmi/coordinator/master.py` | `reset_tool_permissions()` + `reset_for_new_task()` 恢复初始 allowed_tools |

### 未实现

| 缺失项 | 说明 |
|--------|------|
| 层级架构（Sub-Master） | 无 Master → Sub-Master → Worker 三层编排（计划在后续 Phase 实现） |

---

## Phase 4：渐进式工具暴露、向量搜索与 MCP 工具生命周期

### 已实现（核心模块 + sqlite-vec 持久化 + GUI 集成）

| 模块 | 文件 | 说明 |
|------|------|------|
| EmbeddingClient 向量客户端 | `youmi/llm/embeddings.py` | 生成工具需求描述的向量表示 |
| ToolVault（内存版 + 向量搜索） | `youmi/mcp/vault.py` | 内存存储工具条目、向量、HOT/WARM/COLD 状态；语义搜索返回 top-k |
| ToolStore（sqlite-vec 持久化存储层） | `youmi/mcp/tool_store.py` | SQLite + 向量索引：`tools`、`vec_tools`、`tool_changelogs`、`tool_aliases`、`tool_tags`、`tool_dependencies` 6 张表 |
| ToolVault ↔ ToolStore 集成 | `youmi/mcp/vault.py` | Vault 作为内存一级缓存，Store 作为持久化层；`add_tool()` 同步写入 Store，`search()` 委托 Store 搜索 |
| 向量数据库增量更新 | `youmi/mcp/tool_store.py` | 新工具/新版本插入即更新 `vec_tools`；无需全量 `build_embeddings()` |
| 工具版本号与版本链 | `youmi/mcp/tool_store.py` | `tools` 表 `version` + `parent_version_id`；`create_version()` + `get_version_chain()` |
| 工具内部变更日志 | `youmi/mcp/tool_store.py` | `tool_changelogs` 表记录同版本内的 bug 修复说明 |
| ToolBridge discover/load/recycle | `youmi/mcp/bridge.py` | 工具发现、加载到上下文、LRU 回收 |
| ToolBridge 集成 Vault | `youmi/mcp/bridge.py` | `ToolBridge(vault=...)` + `to_openai_tools()` 优先从 Vault 取 schema |
| LocalFunctionProvider | `youmi/mcp/provider.py` | 本地函数注册与执行 |
| MCPServer JSON-RPC | `youmi/mcp/server.py` | 统一路由 |
| MCPClient | `youmi/mcp/client.py` | 进程内客户端 |
| ToolGuardianAgent | `youmi/coordinator/tool_guardian.py` | 修复工具摘要/代码建议 |
| **AgentToolContext 三级状态管理（Agent 侧）** | `youmi/mcp/context.py` | HOT/WARM/COLD 状态从 Vault 迁移到 Agent 侧；每个 Agent 独立上下文视图；含完整单测 |
| **AgentToolContext 集成（attach_vault）** | `youmi/mcp/bridge.py`, `gui/engine/mcp_service.py` | `ToolBridge.attach_vault()` 接入共享 Vault 时自动创建 Agent 侧上下文；白名单/协调器工具自动标记必备；GUI 中 Master 与子 Agent 均已切换 |
| **ApprovalManager 审批独立模块 + 集成** | `youmi/mcp/approval.py`, `youmi/coordinator/tool_approval.py` | 三级审批决策与审计日志委托 ApprovalManager；`ToolApprovalMixin` 全部审批路径接入，新增 `get_approval_audit_log()` |
| **MCPService（GUI 集成层）** | `gui/engine/mcp_service.py` | GUI 级 MCP 服务层：MCPServer + BuiltinToolProvider + ToolStore + ToolVault + EmbeddingClient 一体化 |
| **sqlite-vec 默认启用** | `gui/engine/mcp_service.py` | MCPService.setup() 默认创建 ToolStore + ToolVault + EmbeddingClient，工具描述向量化后存入 SQLite |
| **优雅降级策略** | `gui/engine/mcp_service.py` | Embedding 失败 → 关键词搜索；Store 失败 → 纯内存；任何异常不阻塞 MCP 主流程 |
| **GUI Agent 自动接入 Vault** | `gui/engine/bridge.py` | Master 和子 Agent 的 ToolBridge 自动注入共享 Vault 实例 |

### 未实现

| 缺失项 | 说明 |
|--------|------|
| MCP_Agent 功能封装 | `ToolBridge.inject_tool_context()` 接口已具备，但尚无独立的 MCP_Agent 角色自动为 SubAgent 补充工具上下文 |
| 召回确认闭环 | 原发起 Agent 确认：合适则加载到上下文；不合适则扩大搜索并排除已否决项；多次不合适回复“没有该功能的工具”（`search_and_confirm` 基础接口已有，自动闭环未接入对话流） |
| 多语言代码执行接口 | `tools.language` / `tools.runtime` 字段；当前仅实现 Python 执行器，其他语言留扩展接口 |
| AgentToolContext 轮次推进接入对话循环 | `advance_turn()` / `recycle()` 尚未在 Agent 每轮对话后自动调用，自动回收暂未生效 |

---

## Phase 5：外部 Skill 导入

### 已实现

无

### 未实现

| 缺失项 | 说明 |
|--------|------|
| `youmi/skills/` 整个模块 | Skill 加载器、清单模型、文档自动生成 |
| 代码型 Skill 目录规范 | `manifest.yaml` + `tools/` + `workflow.py` |
| Markdown 轻量 Skill 解析器 | `.md` 指令注入 system prompt |

---

## Phase 6：全局记忆 / 工具使用经验沉淀

> 经验专供工具管理 Agent（如 ToolGuardian）诊断和修复工具问题使用，修复完成后标记 resolved，不注入子 Agent prompt（避免记忆容量膨胀）。

### 已实现

| 模块 | 文件 | 说明 |
|------|------|------|
| Session 持久化后端（SQLite + File） | `youmi/memory/backends/` | 会话级数据持久化 |
| MemoryManager 持久化集成 | `youmi/memory/memory.py` | 记忆管理器集成后端 |
| 全局记忆数据模型 | `youmi/knowledge/models.py` | `KnowledgeEntry`（含 resolved/resolution 修复闭环字段）/ `KnowledgeCategory` / `ToolKnowledge` 聚合视图 |
| `GlobalMemory` 全局记忆核心 | `youmi/knowledge/global_memory.py` | SQLite 持久化 + 向量语义检索（接入 EmbeddingClient，未接入时降级关键词匹配）；`add_experience` / `batch_add` / `search` / `get_tool_knowledge` / `mark_resolved` / `stats` |
| `ToolExperienceExtractor` 经验提取器 | `youmi/knowledge/experience_extractor.py` | 从对话记录提取工具使用经验；失败分析支持 LLM 增强与规则降级（关键词匹配 + 模板生成） |
| 任务结束后自动提取工具经验 | `youmi/coordinator/post_task.py` | PostTaskPipeline 新增第4阶段 `update_global_memory()`：经验沉淀 + 高失败率工具语义分析 + 累计失败阈值触发版本更新 |
| 工具修复后自动创建新版本 | `youmi/coordinator/post_task.py` | 累计失败 ≥3 次且成功率 <50% 时调用 `trigger_tool_version_update()`，并记录 BUG_FIX 经验 |
| MasterAgent 集成全局记忆 | `youmi/coordinator/master.py` | `__init__` 接受 `global_memory` 参数；`on_stop()` 传递给 PostTaskPipeline（含 ToolStore 自动发现） |
| 记忆向量检索 | `youmi/memory/memory.py`, `youmi/memory/strategies/` | `MemoryManager.search()` 支持向量语义检索（传入 EmbeddingClient），降级为策略关键词检索；full/summary/lstm 策略均实现 `search()` |
| 顶层 API 导出 | `youmi/__init__.py` | `GlobalMemory` / `KnowledgeCategory` / `KnowledgeEntry` / `ToolKnowledge` / `ToolExperienceExtractor` |
| **ToolGuardian 全局记忆闭环** | `youmi/coordinator/tool_guardian.py` | 接入 `global_memory` 参数：修复前 `get_tool_knowledge()` 查询历史经验注入修复上下文；修复成功后 `_persist_fix_to_memory()` 写入 BUG_FIX 经验（自动标记 resolved）并将历史未解决问题 `mark_resolved()`；全局记忆不可用/失败时优雅降级 |
| **修复策略注入历史经验** | `youmi/coordinator/fix_strategies.py` | `_generate_fix()` 接受 `tool_knowledge` 参数：LLM prompt 注入「历史经验」段（known_issues/fix_history/resolved_issues + 根治提示）；规则路径附加已知问题（与本次报错去重） |
| **search_tool_experience 内置工具** | `youmi/coordinator/tool_guardian.py` | ToolGuardian 可主动检索全局记忆中的工具经验（语义检索，未接入/异常时返回明确状态） |

### 未实现

| 缺失项 | 说明 |
|--------|------|
| 人工反馈回写全局经验 | 用户反馈采集后写入 GlobalMemory（与非功能需求「反馈闭环」联动） |
| ToolGuardian GUI 集成 | GUI 中自动创建 ToolGuardianAgent 并传入全局记忆实例（当前需在代码中手动构造） |

---

## Phase 7：层级架构

### 已实现

无

### 未实现

| 缺失项 | 说明 |
|--------|------|
| Sub-Master Agent 支持 | 无 Master → Sub-Master → Worker 三层编排 |
| WorkflowPlan 树形嵌套结构 | 当前为扁平计划，不支持子计划嵌套 |
| 层级深度限制 | 无配置与防循环机制 |

---

## GUI-Core 集成（GUI 与核心架构同步）

> 将 GUI 后端与核心 MCP/Bus 层完全同步，确保 GUI 中所有 Agent 通过统一工具调用层和消息总线运行。

### 已实现

| 模块 | 文件 | 说明 |
|------|------|------|
| MCPService（GUI MCP 服务层） | `gui/engine/mcp_service.py` | 全局 MCPServer + ToolStore + ToolVault + EmbeddingClient 一体化；默认启用 sqlite-vec 方案 |
| EngineBridge.init() MCP+Bus 接入 | `gui/engine/bridge.py` | init() 创建 MCPService + InProcessBroker；优雅降级（失败则退化为 ToolRegistry 模式） |
| 子 Agent 自动接入 MCP+Bus | `gui/engine/bridge.py` | `_patch_create_sub_agent()` 自动注入 Vault + 连接总线 |
| GUI 配置层 | `gui/config.py` | mcp_enabled / bus_enabled / vault_enabled + 环境变量覆盖 |
| MasterAgent 提示词清理 | `youmi/agents/master/config.yaml` | 删除硬编码角色列表，改为动态调用 `list_available_roles` |
| coordinator_ops 角色提示清理 | `youmi/tools/coordinator_ops.py` | 错误提示、工具描述、参数描述均改为引导查询 `list_available_roles` |
| MCP 配置段 | `youmi/agents/master/config.yaml` | `mcp_config` 含 vault_enabled / embedding / db_path / auto_approve / sensitive |
| /api/tools REST 端点 | `gui/server.py` | `GET /api/tools` 返回工具列表 + 统计信息 |
| tool_list WebSocket 事件 | `gui/hub/events.py`, `gui/server.py` | WebSocket 连接时推送工具列表到前端 |
| MCP 工具面板（前端） | `gui/static/` | HTML + CSS + JS 渲染工具名称、描述、参数信息 |
| Mock 模式 MCP 接口 | `gui/mock_engine.py` | `list_tools()` / `get_tool_stats()` / `shutdown()` 保持接口一致 |
| 消息总线 GUI 集成 | `gui/engine/bridge.py` | InProcessBroker 创建 + Master/子 Agent 自动接入 |

### 未实现

| 缺失项 | 说明 |
|--------|------|
| 总线事件转发到前端 | 监听 Broker 消息回调，将 Agent 间通信转发为 GUI `agent_message` 事件 |
| ToolGuardian GUI 集成 | 创建 ToolGuardianAgent 实例接入总线（传入 MCPService 的全局记忆实例），自动接收子 Agent 工具调用失败汇报 |
| 工具搜索 UI | 前端工具面板增加「搜索工具」功能（自然语言 → 向量匹配 → 候选工具） |
| Mock 模式 Vault 数据 | mock_engine.py 返回更丰富的 mock 工具数据（参数 + 描述） |

---

## 非功能需求差距（生产就绪缺口）

### 1. 可靠性与容错 — **最高优先级**

> 当前系统无任何容错机制，瞬时故障即导致任务失败，是生产部署的首要障碍。

| 缺口 | 现状 | 目标 |
|------|------|------|
| LLM 调用重试与退避 | `llm/client.py` 无重试、无超时 | 指数退避重试 + 超时控制（装饰器层，覆盖所有 client 调用） |
| 熔断器 | 无 | 对高失败率 MCP/工具自动熔断，防止雪崩 |
| 持久化任务队列 | 消息总线为内存态，崩溃即丢失在途任务 | Redis Streams / RabbitMQ / 本地 SQLite WAL，支持断点续跑与 checkpoint |
| 多步计划补偿 | 无 | 每步超时/预算 + Saga/补偿机制，支持中途失败回滚 |
| 死信队列 | 无 | 失败任务可重放，不丢失 |
| 过载保护 | 无 | 限流与背压机制 |

### 2. 安全 — **高优先级**

> 当前无任何安全控制，总线无认证、工具无沙箱、凭证管理不明，无法在受控环境外部署。

| 缺口 | 现状 | 目标 |
|------|------|------|
| 认证与授权 | BusServer 无认证，任意客户端可连 | 总线与 API 增加 token/mTLS，按 agent/用户做 RBAC |
| 凭证管理 | MCP server 凭据存储方式不明 | 接入 Secret Manager（环境变量 / Vault），禁止明文落盘 |
| 执行沙箱 | `shell_ops` / `web_ops` 直接执行，无隔离 | 在容器/隔离环境运行，限制网络与文件系统访问 |
| 注入防护 | MCP 返回内容未做结构化解析 | 对不可信 server 返回做指令边界检测，防止提示注入 |
| 审计与脱敏 | 日志/记忆无脱敏 | 日志/记忆落库前 PII 脱敏，保留可审计链路 |

### 3. 可观测性 — **高优先级**

> 当前仅有 MCP 请求 `trace_id` 关联字段，无法回答「谁、何时、调了什么工具、卡在哪一步」。

| 缺口 | 现状 | 目标 |
|------|------|------|
| 分布式追踪 | 仅有日志 | 引入 OpenTelemetry，对 LLM 调用、工具调用、总线消息统一埋点（trace + span） |
| 审计日志 | 无 | agent_id / task_id / tool / 入参脱敏 / 耗时 / token / 费用 / 结果状态 |
| 指标监控 | 无 | 接入 Prometheus + Grafana，暴露任务吞吐、队列深度、LLM 错误率等指标 |
| 健康检查 | 无 | 提供 /health、/ready 存活探针端点 |

### 4. 部署与规模化 — **中高优先级**

> 当前单机单 Coordinator + 子进程 Agent，无法被外部系统调用，并发受单机限制。

| 缺口 | 现状 | 目标 |
|------|------|------|
| API 网关 | 无 HTTP 入口，唯一入口为 Streamlit GUI | FastAPI 网关：`submit_task` / `query_status` / `stream_events` 等接口 |
| 任务调度解耦 | 调度与执行耦合 | 任务队列 + 多 Worker（Celery / Ray / asyncio 池） |
| 多租户隔离 | 无 | 会话/记忆按 tenant 隔离，避免共享记忆串上下文 |
| 容器化部署 | 无 | K8s Deployment / HPA / 无状态 Worker 清单 |

### 5. 成本治理 — **中优先级**

> 当前仅有上下文窗口 token 预算（用于截断），无财务维度计量，多 Agent 并发成本不可见、不可控。

| 缺口 | 现状 | 目标 |
|------|------|------|
| 费用计量 | 无 | 按 agent / 任务维度计量 token 与费用，对接审计与看板 |
| 预算上限 | 无 | 按 agent 费用上限 / 日预算，超阈值阻断或降级 |
| 模型路由 | 无 | 简单任务走小模型（Ollama），复杂任务走大模型，平衡成本与质量 |
| 语义缓存 | 无 | semantic cache 复用相似请求结果，节省 token |

### 6. 质量保障与评估 — **中优先级**

> 测试集中在非 LLM 路径（EchoAgent 单测），LLM 行为无回归基准，无输出质量评估。

| 缺口 | 现状 | 目标 |
|------|------|------|
| Mock LLM | 无 | mock LLM server，对 LLM 依赖路径做确定性集成测试 |
| Eval 基准 | 无 | eval 数据集 + 评分脚本（任务完成率、工具选择准确率、成本） |
| 反馈闭环 | 无 | 人工反馈采集，回写全局经验（与 Phase 6 联动） |

### 7. 工程化与可演进性 — **中低优先级**

| 缺口 | 现状 | 目标 |
|------|------|------|
| 版本注册表 | 无 | agent / tool / prompt 版本化，支持回滚与灰度 |
| 实验追踪 | 无 | 记录不同 prompt / 模型的效果对比（MLflow / 轻量 JSONL） |
| 文档 | ✅ 已补全（docs/details/ 7 个模块详细介绍 + 全部顶层文档对齐） | API Reference、快速上手 Demo |
| CI | 无 | lint + 单测 + 打包校验 `pip install` |

### 8. 协议互操作 — **低优先级**

> 当前为私有 WebSocket 总线 + MCP，与外部 agent 生态不互通。

| 缺口 | 现状 | 目标 |
|------|------|------|
| 开放协议 | 无 | 评估接入 A2A（Agent2Agent）/ AG-UI 等开放协议 |
| 兼容接口 | 无 | API 网关输出兼容 OpenAI Assistants 风格接口，降低集成门槛 |

---

## 实施优先级总览（合并后重新排序）

> **排序原则**：基于「不补齐则无法生产部署」的实际影响排序。可靠性与安全是首要障碍，可观测性是运维基础，功能闭环次之，长期扩展最后。

```
P0 — 稳定性与安全（生产部署前置条件）
  ├── 可靠性：LLM 重试退避 + 熔断器                          ❌ 未实现（首要障碍）
  ├── 可靠性：持久化任务队列 + 断点续跑                       ❌ 未实现
  ├── 可靠性：Saga/补偿 + 死信队列 + 过载保护                 ❌ 未实现
  ├── 安全：认证/RBAC（总线 + API）                          ❌ 未实现
  ├── 安全：凭证管理 + PII 脱敏                              ❌ 未实现
  ├── 安全：执行沙箱 + 注入防护                              ❌ 未实现
  └── 可观测性：OTel 埋点 + 审计日志 + 监控 + 健康检查       ❌ 未实现

P1 — 功能闭环与生产可用
  ├── Phase 4  AgentToolContext 三级状态管理 + 集成           ✅ 已完成（轮次自动推进待接入）
  ├── Phase 4  召回确认闭环                                  ❌ 未实现
  ├── Phase 6  全局记忆 / 工具经验沉淀                       ✅ 已完成（含 ToolGuardian 经验消费闭环）
  ├── Phase 6  记忆向量检索                                  ✅ 已完成（MemoryManager.search）
  ├── 部署：FastAPI 网关 + 多 Worker                         ❌ 未实现
  ├── 部署：多租户隔离                                       ❌ 未实现
  └── 质量保障：mock LLM + eval 基准                         ❌ 未实现

P2 — 降本增效与工程化
  ├── 成本治理：费用计量 + 预算上限                           ❌ 未实现
  ├── 成本治理：模型路由 + 语义缓存                          ❌ 未实现
  ├── 工程化：CI/CD + 文档补全（docs/ 已补全）                ✅ 文档 / ❌ CI
  ├── 工程化：版本注册表 + 实验追踪                          ❌ 未实现
  ├── Phase 5  Skill 导入                                    ❌ 未实现
  └── 外部通信渠道（Telegram/Discord）                       ❌ 未实现

P3 — 长期扩展与生态
  ├── Phase 7  层级架构（Sub-Master）                        ❌ 未实现
  ├── 协议互操作：A2A / AG-UI / OpenAI 兼容接口              ❌ 未实现
  ├── Agent 运行时迁移与持久化                               ❌ 未实现
  ├── 多模型 Provider 管理与 fallback                         ❌ 未实现
  └── MCP Server 高可用                                      ❌ 未实现
```

### 已完成项（参考）

```
✅ Phase 1  消息总线
✅ Phase 2  内置基础工具
✅ Agent 基类 / ReAct / MCP 基础层
✅ Phase 3  MasterAgent + WorkflowPlan + Handoff（基础版）
✅ SubAgent prompt 级工作流自检（_TaskSelfCheck）
✅ SubAgent 工具权限申请流程（TOOL_REQUEST/TOOL_RESPONSE）
✅ SubAgent 进程隔离（SubProcessAgentRunner）
✅ 新任务循环（conversation_loop + reset_for_new_task）
✅ 任务完成后后台流水线（PostTaskPipeline）
✅ search_new_tools 兆底工具
✅ 三级审批模型（auto/manual/master）
✅ ToolBridge 热更新 + 工作流级权限回收
✅ Phase 4  ToolVault 内存版 + 向量搜索（基础版）
✅ Phase 4  ToolStore sqlite-vec 持久化存储层
✅ Phase 4  ToolVault ↔ ToolStore 集成 + 向量增量更新
✅ Phase 4  工具版本号 + 版本链 + 变更日志
✅ Phase 4  AgentToolContext Agent 侧三级状态 + attach_vault 集成
✅ Phase 4  ApprovalManager 独立审批模块 + MasterAgent 接入（含审计日志）
✅ Phase 6  全局记忆模块（GlobalMemory + KnowledgeEntry + ToolExperienceExtractor）
✅ Phase 6  PostTaskPipeline 经验沉淀 + 自动版本更新触发
✅ Phase 6  记忆向量检索（MemoryManager.search + 策略 search）
✅ Phase 6  ToolGuardian 经验消费闭环（修复前查询经验 + 修复后 BUG_FIX 写回 + mark_resolved）
✅ GUI-Core MCP 集成（MCPService + sqlite-vec 默认启用）
✅ GUI-Core Bus 集成（InProcessBroker + 子 Agent 自动接入）
✅ GUI 工具面板（前端 + /api/tools + tool_list 事件）
✅ MasterAgent 提示词动态角色查询（删除硬编码角色列表）
```

---

## 建议里程碑

- **M1 — 基础稳固**：P0 可靠性（重试退避 + 熔断）+ P0 安全（认证 + 沙箱）+ P0 可观测性（OTel + 审计日志）→ 达到「可控、可查、可观测」的最小生产门槛。
- **M2 — 可运维**：P0 可靠性（持久队列 + 断点续跑 + Saga）+ P1 部署（FastAPI 网关 + Worker + 多租户）→ 支持真实并发与故障恢复。
- **M3 — 功能完整**：P1 工具生命周期闭环（AgentToolContext + 召回闭环）+ P1 全局记忆（经验沉淀 + 向量检索 + ToolGuardian 闭环）→ 工具与知识可积累。
- **M4 — 降本增效**：P2 成本治理 + 质量保障 + 工程化 → 成本可控、行为可回归、配置可复现。
- **M5 — 生态扩展**：P3 互操作 + 层级架构 + 外部通信 → 融入外部 agent 生态，支持复杂编排。
