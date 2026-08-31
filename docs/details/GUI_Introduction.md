# GUI 层详解

> 对应代码：`gui/`（server.py / engine/ / hub/ / persistence/ / static/）
> 启动命令：`python -m gui`（默认端口 8000）

GUI 采用「群聊式 Web 应用」设计（QQ/微信隐喻），基于 **aiohttp** 提供 REST + WebSocket 双模式接口，通过 `EngineBridge` 桥接 YouMi 引擎，全程不修改核心 `youmi/` 代码。

---

## 1. 整体架构

```
浏览器（gui/static/）
  HTML/CSS/JS  三栏布局 · 气泡聊天 · 工具卡片
       │ WebSocket + REST
gui/server.py  (aiohttp)
  ├─ 静态资源服务 (GET /static/*, GET /)
  ├─ REST 端点   (/api/agents / /api/sessions / /api/tools 等)
  ├─ WebSocket   (WS /ws  事件流)
  └─ 引擎持有器  (单例 EngineBridge)
       │ 进程内调用
gui/engine/
  ├─ bridge.py      EngineBridge  引擎适配器
  ├─ hook_bridge.py GUIHooks      挂载 HookRegistry
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

## 4. GUIHooks（gui/engine/hook_bridge.py）

通过 Agent 的 `HookRegistry` 注入三类监听处理器，无需改动引擎代码：

| Hook 类型 | 触发时机 | GUI 动作 |
|-----------|---------|---------|
| `MESSAGE_SENDING` | Agent 发送总线消息前 | 推送 `agent_chunk` / `agent_message_end` 事件 |
| `BEFORE_TOOL_CALL` | 工具调用前 | 推送 `tool_call` 卡片事件（工具名 + 入参） |
| `AFTER_TOOL_CALL` | 工具调用后 | 推送 `tool_result` 卡片事件（结果摘要） |

`GUIHooks.install(agent)` 对每个 Agent（包括运行期动态创建的子 Agent）安装上述三个处理器。所有处理器返回 `HookDecision.PASS`（不干预引擎执行）。

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

后端 → 前端推送的事件类型：

| 事件 | 说明 |
|------|------|
| `init_data` | 连接建立，推送 agents/sessions/tools 全量数据 |
| `history_messages` | 历史消息列表（切换会话时） |
| `user_msg` | 用户消息回声 |
| `agent_message_start` | Agent 开始新气泡（agent_id/name） |
| `agent_chunk` | 流式文本增量（agent_id/delta） |
| `agent_message_end` | 气泡结束（agent_id/meta：耗时/token 等） |
| `tool_call` | 工具调用卡片（agent_id/tool/args） |
| `tool_result` | 工具结果卡片（agent_id/tool/result） |
| `agent_join` | 新子 Agent 加入群聊（系统消息 + 成员更新） |
| `status` | Agent 状态变化（idle/running/…） |
| `tool_list` | 工具列表更新 |
| `error` | 错误通知 |

`WebSocketHub` 管理所有活跃 WebSocket 连接，提供 `broadcast(event)` 广播与 `send_to(ws, event)` 定向推送。

---

## 7. REST 端点（gui/server.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agents` | 获取所有 Agent 卡片列表 |
| GET | `/api/sessions` | 获取会话列表 |
| POST | `/api/sessions` | 创建新会话 |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| POST | `/api/sessions/{id}/messages` | 发送消息 |
| GET | `/api/tools` | 工具列表 + 统计（工具面板数据源） |
| GET | `/` | 前端 index.html |
| GET | `/static/*` | 静态资源 |

---

## 8. 持久化（gui/persistence/store.py）

`Store` 以 JSON 文件持久化会话与消息（运行时目录 `gui/data/`）：

- `save_session(session)` / `load_session(id)` / `list_sessions()`
- `save_message(rec)` / `load_messages(session_id)`
- 多会话并发写入安全（asyncio.Lock 保护）

---

## 9. 配置（gui/config.py）

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|---------|-------|------|
| `port` | `YOUMI_GUI_PORT` | 8000 | HTTP 端口 |
| `host` | `YOUMI_GUI_HOST` | `0.0.0.0` | 监听地址 |
| `use_mock` | `YOUMI_USE_MOCK` | false | 启用 Mock 模式（无 LLM 演示） |
| `mcp_enabled` | - | true | 启用 MCP 工具层 |
| `bus_enabled` | - | true | 启用消息总线 |
| `vault_enabled` | - | true | 启用 ToolVault 向量搜索 |

---

## 10. 工作流追踪（gui/engine/tracker.py）

`WorkflowTracker` 记录工作流执行状态（子 Agent 创建顺序、各步骤 status、完成时间），供 GUI 渲染进度时间轴使用。

---

## 11. 前端结构（gui/static/）

| 文件 | 职责 |
|------|------|
| `index.html` | 三栏骨架（会话列表 + 聊天窗口 + 群成员） |
| `app.js` | WebSocket 客户端、消息路由、渲染调度 |
| `chat-renderer.js` | 气泡渲染（流式文本 + 工具卡片折叠） |
| `session-panel.js` | 会话列表面板（未读角标、最后消息预览） |
| `panels.js` | 工具面板（工具库浏览） |
| `modal.js` | 弹窗交互（新会话、确认等） |
| `state.js` | 前端状态管理 |
| `ui.js` | 通用 UI 工具函数 |
| `ws.js` | WebSocket 连接管理与重连 |
| `style.css` | QQ/微信风格样式（微信绿 #07C160 / QQ 蓝 #12B7F5） |

---

## 12. Mock 模式（gui/mock_engine.py）

无 LLM 时用于演示：`list_tools()` / `get_tool_stats()` / `shutdown()` 返回预设 mock 数据，接口与 `EngineBridge` 完全兼容。

---

## 13. 相关文档

- [Agent_Introduction.md](Agent_Introduction.md) — Hook 系统（GUIHooks 的基础）
- [MCP_Introduction.md](MCP_Introduction.md) — MCPService 详细能力
- [Message_Introduction.md](Message_Introduction.md) — 总线集成
