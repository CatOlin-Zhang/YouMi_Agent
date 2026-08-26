# YouMi Agent — 群聊式 Web GUI 重设计方案

> **目标**：在**不改动核心引擎**的前提下，将 GUI 从现有 Streamlit「群聊 v3」重做为 **QQ/微信风格的 Web 应用**。
> **隐喻**：单聊 = 与单个 Agent 对话；群聊 = 多个 Agent 协作，每个 Agent 是群里的「成员」，发言以独立气泡呈现。
> **状态**：**已确认**，进入实现阶段。

---

## 1. 设计原则

1. **核心逻辑复用**：`MasterAgent` 编排、`Agent.chat_turn_stream()`、`core/hooks`、消息总线、`MCP` 工具网关全部保留，只重做 GUI 层。
2. **QQ/微信隐喻**：
   - 左栏 = **会话列表**（单聊 + 群聊，含头像、最后一条预览、时间、未读角标）。
   - 中栏 = **聊天窗口**（对方气泡居左带头像/昵称，自己气泡居右；时间戳；打字状态；工具调用折叠卡片；系统消息居中）。
   - 右栏/抽屉 = **通讯录 / 群成员**（Agent 列表，可发起单聊或拉群）。
3. **单聊与群聊统一抽象为「会话」**：会话有 `type=single|group`，群聊成员即参与协作的多个 Agent。

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────┐
│  浏览器 (gui/static: HTML/CSS/JS)                          │
│   三栏布局 · 气泡聊天 · 头像/未读/打字状态                    │
└───────────────┬──────────────────────────────────────────┘
                │  WebSocket (JSON 事件流)
┌───────────────▼──────────────────────────────────────────┐
│  gui/server.py  (新增后端)                                  │
│   ├─ 静态资源服务 (index.html / app.js / style.css)        │
│   ├─ WebSocket 网关: list_contacts / list_conversations /  │
│   │                    create_conversation / send_message  │
│   ├─ 引擎持有器: 单例 MasterAgent(群主) + 子Agent注册表      │
│   ├─ GUI Hook 桥接: 将引擎 hooks 事件 → WS 事件推送          │
│   └─ 持久化: gui/data/*.json (会话 + 消息)                  │
└───────────────┬──────────────────────────────────────────┘
                │  进程内调用 (保持不变)
┌───────────────▼──────────────────────────────────────────┐
│  YouMi 引擎 (youmi/*): MasterAgent · Agent · hooks · bus   │
│               · mcp · memory · tools                       │
└──────────────────────────────────────────────────────────┘
```

---

## 3. 数据模型

- **Contact（Agent）**：`id, name, role, avatar_color, status, is_master`
- **Conversation**：`id, type(single|group), name, member_ids[], last_message, updated_at, unread`
- **Message**：`id, conversation_id, sender_id(user|agent_id|system), sender_name, content, kind(text|tool|system|typing), timestamp, meta{tool_calls, iterations, elapsed}`

---

## 4. 事件流协议（后端 → 前端，WebSocket JSON）

| 事件 | 含义 |
|---|---|
| `user_msg` | 回声用户消息 |
| `agent_message_start` | 某 Agent 开始一条新气泡（`agent_id, name`） |
| `agent_chunk` | 流式文本增量（`agent_id, delta`） |
| `agent_message_end` | 气泡结束（`agent_id, meta`） |
| `tool_call` / `tool_result` | 工具调用/结果卡片（`agent_id, tool, args/result`） |
| `agent_join` | 新 Agent 加入群聊（系统消息 + 实时加入成员） |
| `status` | Agent 状态变化（idle/running/…） |
| `typing` | 某 Agent「正在输入…」 |
| `error` | 错误 |

---

## 5. 核心交互映射（与现有逻辑一致）

- **单聊（会话含 1 个 Agent）**：`send_message` → 直接调用该 Agent 的 `chat_turn_stream(message)`；其 hook 事件渲染为该 Agent 的气泡。
- **群聊（会话含 MasterAgent + 成员）**：`send_message` → 调用 `MasterAgent.chat_turn_stream(message)`。GUI Hook 捕获**所有** Agent 的 `MESSAGE_SENDING` / `BEFORE_TOOL_CALL` / `AFTER_TOOL_CALL`（含运行期动态创建的子 Agent），按 `conversation_id` 路由为独立气泡。
- **动态成员**：MasterAgent 运行期 `create_sub_agent` 时，自动安装 GUI Hook 并向群聊推送 `agent_join`（「X 加入群聊」+ 成员头像实时更新）。
- **@mention**：群聊中输入 `@Agent名` 可定向对话（复用现有 `parse_mention` 思路），未指定则默认发给 MasterAgent 编排。

---

## 6. 后端实现要点（gui/server.py，新增）

1. **引擎持有器**：单例 `MasterAgent`（群主）复用 `create_master_agent` 逻辑；子 Agent 注册表来自 `master.get_sub_agents()`。支持多会话共享同一引擎，会话状态按 `conversation_id` 隔离。
2. **GUI Hook**：实现 `HookRegistry`，监听 `MESSAGE_SENDING` / `BEFORE_TOOL_CALL` / `AFTER_TOOL_CALL`，将事件通过 WS 推送；对动态创建的子 Agent 同样安装（hook `create_sub_agent`）。
3. **持久化**：`gui/data/` 下 JSON 存储会话与消息；提供 `list_conversations` / `get_history`；后续可升级 sqlite。
4. **依赖选择**（已确认选 a）：
   - **(a) aiohttp** ✅：一个库同时提供静态服务 + WebSocket，代码最简洁（需在 `pyproject` 增加 `web = ["aiohttp"]`）。
   - ~~(b) websockets + stdlib `http.server`~~：零新增依赖但静态服务需手写，已排除。
5. **启动**：`python -m gui.server`（或 `python gui/server.py`），默认 `http://localhost:8000`。

---

## 7. 前端实现要点（gui/static/*，新增）

- **三栏布局**：会话列表（左）｜聊天窗口（中）｜通讯录/群成员（右）。
- **气泡聊天**：对方居左（头像 + 昵称 + 气泡），自己居右；圆角气泡；时间戳；群聊显示发言者昵称。
- **打字状态**：收到 `typing` 事件显示「对方正在输入…」。
- **工具调用**：`tool_call/tool_result` 渲染为可折叠卡片（🔧 工具名 + 入参/结果摘要）。
- **系统消息**：`agent_join`、群创建等居中灰色小字。
- **未读角标 / 最后消息预览**：会话列表展示，切回会话清零。
- **@ 面板**：群聊输入框上方「@」按钮弹出成员选择（复用现有浮动选择器交互）。
- **配色**：微信绿 `#07C160` / QQ 蓝 `#12B7F5`，浅色主题；头像用首字母色块区分 Agent。

---

## 8. 文件清单

**新增**
- `gui/server.py` — 后端（引擎持有 + WS 网关 + Hook 桥接 + 持久化 + 静态服务）
- `gui/static/index.html` — 三栏骨架
- `gui/static/app.js` — WS 客户端、渲染、状态管理
- `gui/static/style.css` — QQ/微信风格样式
- `gui/data/`（运行时生成，存会话/消息 JSON）

**修改**
- `pyproject.toml` — 增加 Web 依赖（aiohttp）与启动说明
- `README.md` / `docs/` — 补充新 GUI 使用说明

**保留**
- `gui/streamlit_app.py` — 作为旧版并存，不删除

---

## 9. 里程碑

| 阶段 | 交付 |
|---|---|
| **M1 后端骨架** | `server.py` 启动；引擎持有；静态服务；`list_contacts` / `list_conversations`；WS 联通 |
| **M2 单聊闭环** | 单聊：发送 → 流式气泡 → 持久化历史 |
| **M3 群聊 + Hooks** | 安装 GUI Hook；逐 Agent 独立气泡；`agent_join`；工具调用卡片；@mention |
| **M4 UI 打磨** | QQ/微信视觉、头像、未读角标、打字状态、会话切换、历史重载 |
| **M5 增强（可选）** | Agent 详情/工具抽屉、多模型切换、设置面板、sqlite 持久化 |

---

## 10. 风险确认与决策（已关闭）

| # | 风险点 | 决策 |
|---|---|---|
| 1 | 子 Agent Hook 挂载 | ✅ 可行 — `hook_registry` 是公开属性，封装 `_install_gui_hooks(agent)` 对每个新 Agent 统一注入 |
| 2 | 逐 Agent 气泡 | ✅ A+B 结合 — **(A)** 在 `Agent.send_message()` 中补 `MESSAGE_SENDING` 钩子触发桩；**(B)** 隔离模式下由 BusServer 监听 `WorkflowMessage` 并转发 GUI WS 事件 |
| 3 | 本地模型依赖 | ✅ 无变化 — `LLMConfig` 已支持 Ollama / OpenAI / Anthropic / 自定义 provider |
| 4 | 与 v3 并存 | ✅ 无冲突 — 独立文件、独立端口 |
| 5 | 依赖取舍 | ✅ 选 (a) aiohttp — 单端口服务 HTTP + WS，代码量最少 |

### 补桩细节（风险 #2 实施方案）

**方案 A — 同进程补桩**（改 `youmi/core/agent.py`）：
- 在 `Agent.send_message()` 方法开头增加 `await self._hook_registry.invoke(HookType.MESSAGE_SENDING, HookContext(...))`
- 一处改动，全局生效；所有同进程 Agent（含非隔离子 Agent）的 `MESSAGE_SENDING` 钩子均被触发
- GUI 侧通过 `_install_gui_hooks(agent)` 在每个 Agent 上注册 handler，将事件转推 GUI WS

**方案 B — 跨进程转发**（改 `gui/server.py`）：
- GUI 后端在启动时注册 `BusServer.broker.on_message()` 回调
- 当隔离子的 FEEDBACK 消息流经 BusServer 时，回调将其转为 GUI WS 事件（`agent_chunk` / `agent_message_end`）推送给前端
- 无需改动 `_subprocess_entry.py`，隔离子 Agent 代码零侵入

*所有决策已确认，按 M1→M4 实现可运行 MVP，M5 视需求迭代。*
