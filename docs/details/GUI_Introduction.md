# GUI 层详解

> 对应代码：`gui/`（server.py / engine/ / hub/ / persistence/ / static/）
> 启动命令：`python -m gui`（默认端口 8766）

GUI 采用「群聊式 Web 应用」设计（QQ/微信隐喻），基于 **aiohttp** 提供 REST + WebSocket 双模式接口，通过 `EngineBridge` 桥接 YouMi 引擎，全程不修改核心 `youmi/` 代码。支持配置启用式认证中间件（M1）。

---

## 1. 整体架构

```
浏览器（gui/static/）
  HTML/CSS/JS  三栏布局 · 气泡聊天 · 工具卡片
       │ WebSocket + REST（可选 Bearer token 认证）
gui/server.py  (aiohttp)
  ├─ 认证中间件    (YOUMI_AUTH_* 配置启用式，保护 /api/* 与 /ws)
  ├─ 静态资源服务 (GET /static/*, GET /)
  ├─ REST 端点   (/api/agents / /api/sessions / /api/tools / /api/audit / /healthz)
  ├─ WebSocket   (WS /ws  事件流)
  └─ 引擎持有器  (单例 EngineBridge)
       │ 进程内调用
gui/engine/
  ├─ bridge.py      EngineBridge  引擎适配器
  ├─ hook_bridge.py GUIHookBridge 挂载 HookRegistry
  ├─ mcp_service.py MCPService    MCP/Vault/ToolStore 一体化
  ├─ models.py      Session/AgentCard/MessageRecord 数据模型
  └─ tracker.py     WorkflowTracker 工作流状态追踪
gui/hub/
  ├─ events.py      WebSocket 事件定义
  └─ ws_hub.py      WebSocketHub  连接管理（广播/定向推送）
gui/persistence/
  └─ store.py       Store  会话与消息 JSON 持久化
```

---

## 2. 核心数据模型（gui/engine/models.py）

| 模型 | 关键字段 |
|------|---------|
| `AgentCard` | agent_id / name / role / color / status / bio / task |
| `MessageRecord` | msg_id / session_id / agent_id / agent_name / role / kind / text / ts / meta |
| `Session` | session_id / type(single\|group) / name / owner_agent_id / member_ids / created_at |

`role` 字段取值：`user` / `assistant` / `system` / `tool`。`kind` 字段取值：`text` / `tool` / `system`。

---

## 3. EngineBridge（gui/engine/bridge.py，约 510 行）

GUI 引擎适配器，单例（`gui/server.py` 持有）：

### 初始化

```python
await bridge.init()
# 执行顺序：
# 1. MCPService.setup(master)  — MCP + ToolStore + ToolVault + EmbeddingClient
# 2. InProcessBroker 创建
# 3. Master.connect_bus(broker, wf_id)
# 4. _patch_create_sub_agent()  — 拦截子 Agent 创建，自动注入 MCP + Bus
# 5. _patch_run_sub_agent()     — 拦截子 Agent 运行，更新状态追踪
# 6. GUIHooks.install(master)   — 挂载全局 Hook
```

### 子 Agent 自动接线（_patch_create_sub_agent）

```python
# 原始 create_sub_agent() → 拦截 → 自动执行：
# 1. MCPService.connect_agent(sub, mcp_server)  — ToolBridge + Vault
# 2. sub.connect_bus(broker, workflow_id)        — 消息总线
# 3. GUIHooks.install(sub)                       — 注入 GUI Hook
# 4. on_sub_agent_created(sub, role, task)        — 注册到 AgentCard 表
```

### 消息生命周期

| 方法 | 说明 |
|------|------|
| `open_message(rec)` | 开启一条新消息气泡（推送 `agent_message_start` 事件） |
| `append_chunk(msg_id, text)` | 追加流式文本增量（推送 `agent_chunk`） |
| `replace_message(msg_id, text)` | 替换消息内容（工具结果卡片替换占位符） |
| `close_message(msg_id, meta)` | 关闭气泡（推送 `agent_message_end`，持久化消息） |
| `split_agent_stream(agent_id)` | 为同一 Agent 开启新气泡（多轮流式分割） |

### 其他方法

| 方法 | 说明 |
|------|------|
| `update_agent_status(agent_id, status)` | 更新 AgentCard 状态（推送 `status` 事件） |
| `await send_user_message(session_id, text)` | 接收用户消息并路由到对应 Agent |
| `await create_session(...)` | 创建单聊/群聊会话 |
| `await delete_session(session_id)` | 删除会话及其消息 |
| `await push_history(ws, session_id)` | WebSocket 连接时推送历史消息 |
| `await list_tools()` | 列出工具库中所有工具 |
| `get_tool_stats()` | 工具统计信息（总数/向量化数等） |

---

## 4. GUIHookBridge（gui/engine/hook_bridge.py）

通过 Agent 的 `HookRegistry` 注入四类监听处理器，无需改动引擎代码：

| Hook 类型 | 触发时机 | GUI 动作 |
|-----------|---------|---------|
| `BEFORE_TOOL_CALL` | 工具调用前 | 推送 `message_start`（kind=tool，工具卡片占位）|
| `AFTER_TOOL_CALL` | 工具调用后 | 填充工具卡片（结果摘要）|
| `AFTER_MODEL_CALL` | LLM 调用后 | 渲染文本气泡（流式增量 / 完整回复）|
| `MESSAGE_SENDING` | Agent 发送总线消息前 | 渲染 Agent 间协作提示气泡 |

`GUIHookBridge.inject(agent)` 对每个 Agent（包括运行期动态创建的子 Agent）安装上述处理器，并负责工作流完成检测（`workflow_complete` 事件）。所有处理器返回 `HookDecision.PASS`（不干预引擎执行）。

---

## 5. MCPService（gui/engine/mcp_service.py）

GUI 级 MCP 服务层，统一管理 MCP + ToolStore + ToolVault + EmbeddingClient：

```python
class MCPService:
    async def setup(self, master):
        # 1. 创建 MCPServer + 注册 BuiltinToolProvider
        # 2. _init_vault(work_dir)  — 创建 ToolStore(.youmi_tools.db) + ToolVault
        # 3. EmbeddingClient 初始化（失败降级为关键词搜索）
        # 4. _import_agent_tools_to_vault(master)  — 工具向量化 + 存入 ToolStore
        # 5. connect_agent(master, mcp_server)  — MasterAgent 接入 ToolBridge + Vault

    def connect_agent(self, agent, mcp_server):
        # ToolBridge(vault=self.vault) → agent._tool_bridge
        # attach_vault() 自动初始化 AgentToolContext
```

优雅降级策略：

- Embedding 失败 → 关键词搜索
- ToolStore 初始化失败 → 纯内存 ToolVault
- 任何异常不阻塞 MCP 主流程（`try/except + logger.warning`）

---

## 6. WebSocket 事件协议（gui/hub/events.py）

后端 → 前端推送的事件类型（统一 JSON 对象，带 `type` 与 `ts` 字段）：

| 事件 | 说明 |
|------|------|
| `hello` | 连接建立握手（master_id / auth_enabled）|
| `pong` | 心跳应答 |
| `session_created` / `session_deleted` | 会话创建 / 删除 |
| `agent_join` | 新子 Agent 加入群聊（成员更新）|
| `agent_update` | Agent 状态变化（idle/running/…）|
| `message_start` | Agent 开始新气泡（msg_id/agent_id/kind=text\|tool）|
| `message_chunk` | 流式文本增量 |
| `message_replace` | 替换消息内容（工具结果替换占位符）|
| `message_end` | 气泡结束（meta：耗时/token 等）|
| `typing` | 输入中指示 |
| `history` | 历史消息列表（连接/切换会话时）|
| `tool_list` | 工具列表与统计（工具面板数据源）|
| `workflow_step` | 工作流步骤状态变更 |
| `workflow_complete` | 所有工作流步骤完成 |
| `error` | 错误通知 |

`WebSocketHub` 管理所有活跃 WebSocket 连接，提供 `broadcast(event)` 广播与定向推送。

---

## 7. REST 端点与认证（gui/server.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agents` | 获取所有 Agent 卡片列表 |
| GET | `/api/sessions` | 获取会话列表 |
| POST | `/api/sessions` | 创建新会话 |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| POST | `/api/sessions/{id}/messages` | 发送消息 |
| GET | `/api/tools` | 工具列表 + 统计（工具面板数据源） |
| GET | `/api/audit` | 查询最近审计事件（需 admin 角色，limit / event_type 过滤）|
| GET | `/healthz` | 健康探活（免认证：引擎就绪 / 认证开关 / 审计开关 / 连接数）|
| GET | `/` | 前端 index.html |
| GET | `/static/*` | 静态资源 |

### 认证中间件（M1）

由 `YOUMI_AUTH_TOKEN` / `YOUMI_AUTH_TOKENS` 配置启用（复用 `youmi/security/auth.py`，未配置时零摩擦放行）：

- 保护范围：`/api/*` 与 `/ws`；豁免 `/`、`/static/*`、`/healthz`；
- Token 形态：`Authorization: Bearer <token>` 或 `?token=<token>`；
- 认证失败返回 401 并写审计日志；`/api/audit` 要求 admin 角色；
- 前端 `auth.js` 将 `?token=` 自动存入 localStorage 并清理地址栏，后续 REST/WS 请求自动附带。

---

## 8. 持久化（gui/persistence/store.py）

`Store` 以 JSON 文件持久化会话与消息（运行时目录 `gui/data/`）：

- `save_session(session)` / `load_session(id)` / `list_sessions()`
- `save_message(rec)` / `load_messages(session_id)`
- 多会话并发写入安全（asyncio.Lock 保护）

---

## 9. 配置（gui/config.py）

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `YOUMI_GUI_HOST` | `127.0.0.1` | 监听地址 |
| `YOUMI_GUI_PORT` | `8766` | HTTP 端口 |
| `YOUMI_GUI_MASTER` | `master` | 主 Agent 名（对应 `youmi/agents/<name>/config.yaml`）|
| `YOUMI_GUI_MCP` | `1` | 启用 MCP 工具调用层 |
| `YOUMI_GUI_BUS` | `1` | 启用进程内消息总线 |
| `YOUMI_GUI_VAULT` | `1` | 启用 ToolVault + ToolStore（sqlite-vec）|
| `YOUMI_GUI_VAULT_DB` | （空）| ToolStore 数据库路径 |
| `YOUMI_GUI_EMBEDDING_URL` | `http://localhost:11434/v1` | Embedding 服务地址 |
| `YOUMI_GUI_EMBEDDING_MODEL` | `nomic-embed-text` | Embedding 模型名 |
| `YOUMI_AUTH_TOKEN` / `YOUMI_AUTH_TOKENS` | （空）| 认证 token（未配置零摩擦；见 youmi/security/auth.py）|

---

## 10. 工作流追踪（gui/engine/tracker.py）

`WorkflowTracker` 记录工作流执行状态（子 Agent 创建顺序、各步骤 status、完成时间），供 GUI 渲染进度时间轴使用。

---

## 11. 前端结构（gui/static/）

| 文件 | 职责 |
|------|------|
| `index.html` | 三栏骨架（会话列表 + 聊天窗口 + 群成员） |
| `app.js` | WebSocket 客户端、消息路由、渲染调度 |
| `chat-renderer.js` | 气泡渲染（流式文本 + 工具卡片折叠；发言者变化时新建独立气泡）|
| `session-panel.js` | 会话列表面板（未读角标、成员数实时更新）|
| `panels.js` | 工具面板（工具库浏览） |
| `modal.js` | 弹窗交互（新会话、确认等） |
| `state.js` | 前端状态管理 |
| `ui.js` | 通用 UI 工具函数 |
| `ws.js` | WebSocket 连接管理与重连（认证 token 附带）|
| `auth.js` | 认证逻辑（`?token=` 记忆 localStorage + 地址栏清理）|
| `style.css` | QQ/微信风格样式（运行中状态黄色闪烁动画）|

---

## 12. Mock 模式（gui/mock_engine.py）

无 LLM 时用于演示：`list_tools()` / `get_tool_stats()` / `shutdown()` 返回预设 mock 数据，接口与 `EngineBridge` 完全兼容。

---

## 13. 相关文档

- [Agent_Introduction.md](Agent_Introduction.md) — Hook 系统（GUIHooks 的基础）
- [MCP_Introduction.md](MCP_Introduction.md) — MCPService 详细能力
- [Message_Introduction.md](Message_Introduction.md) — 总线集成
