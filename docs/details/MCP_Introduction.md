# MCP 工具层详解

> 对应代码：`youmi/mcp/`（provider.py / server.py / client.py / bridge.py / vault.py / tool_store.py / context.py / approval.py / protocol.py / models.py）、`youmi/tools/`（builtin.py 等）

MCP 层是 YouMi Agent 的统一工具调用网关，采用 **Provider → Server → Client → Bridge** 四层架构，在此之上叠加 ToolVault（语义缓存）、ToolStore（持久化版本管理）与 AgentToolContext（Agent 侧三级状态）。

---

## 1. 四层核心架构

```
工具函数/Python函数
       ↓
 LocalFunctionProvider  ← 将 Python 函数包装为 MCP 工具
 (ToolProvider 子类)
       ↓
   MCPServer             ← 统一工具网关，_tool_route 字典路由
       ↓
   MCPClient             ← 进程内客户端，直接引用 MCPServer
       ↓
   ToolBridge            ← 权限白名单 + 调用委托（每个 Agent 独立实例）
```

### MCPServer（youmi/mcp/server.py）

`register_provider(provider)` 注册 ToolProvider；接收 JSON-RPC 2.0 格式调用（`tools/list` / `tools/call`），按 `_tool_route` 字典分发到对应 Provider。

### ToolProvider / LocalFunctionProvider（youmi/mcp/provider.py）

`ToolProvider` 是插件化扩展点：实现 `provider_id()`、`get_tools()` 返回工具定义列表、`execute()` 执行工具调用。`LocalFunctionProvider` 将带标注的 Python 函数自动包装为 MCP 工具，是最常用的方式（`Agent.connect_mcp()` 内部使用）。

### ToolBridge（youmi/mcp/bridge.py，约 760 行）

每个 Agent 有独立的 ToolBridge 实例，主要职责：

| 方法 | 说明 |
|------|------|
| `call_tool(name, arguments)` | 权限校验（白名单）→ MCPClient 调用 |
| `to_openai_tools()` | 生成 OpenAI function calling schema（含 search_new_tools 兜底工具） |
| `add_allowed_tool(name)` | 运行期热添加工具权限 |
| `attach_vault(vault, essential_names)` | 接入共享 ToolVault；必需工具自动标记 HOT |
| `discover_tools(query)` | 向 ToolVault 语义搜索工具（自然语言描述） |
| `load_tool(name)` | 将 WARM/COLD 工具提升为活跃状态 |
| `recycle_tools()` | 回收长期未用工具 |
| `advance_turn()` | 推进工具使用轮次 |
| `search_and_confirm(query)` / `confirm/reject_search_result()` | 召回确认流程接口 |
| `inject_tool_context(agent)` | 为 SubAgent 注入当前工具上下文 |

---

## 2. 内置工具清单（youmi/tools/builtin.py 等）

`BuiltinToolProvider` 注册 9 个标准工具：

| 工具名 | 说明 |
|--------|------|
| `file_search` | glob 模式递归文件搜索 |
| `file_read` | 读取文件内容（支持 start_line/max_lines 分段读取） |
| `file_write` | 文件写入（overwrite/append/create 模式） |
| `list_directory` | 列出目录内容（可选 detail/show_hidden） |
| `text_search` | 正则或文本全局搜索（grep 风格） |
| `shell_exec` | Shell 命令执行（含超时控制，默认 30s） |
| `web_fetch` | HTTP 网页抓取 |
| `get_datetime` | 获取当前日期时间（支持时区偏移与自定义格式） |
| `json_tool` | JSON 格式化/校验/提取（format/validate/extract） |

额外：`search_new_tools`（兜底工具，Agent 在 ReAct 中工具不足时自动触发向量或关键词搜索）。

`coordinator_ops.py` 注册 MasterAgent 专用工具（create_sub_agent / run_sub_agent 等，见 Master 文档）。

---

## 3. ToolVault 工具库（youmi/mcp/vault.py）

内存级工具缓存 + 语义向量搜索层。每次 `ToolBridge.call_tool()` 记录使用情况，自动维护工具热度：

```
HOT   — 当前正在使用，包含在 LLM 的工具 schema 中
WARM  — 近期使用但不在本轮 schema，以摘要形式提示
COLD  — 长期未用，从活跃列表淘汰
```

| 方法 | 说明 |
|------|------|
| `add_tool(entry)` | 注册工具（同步写入 ToolStore 持久层） |
| `add_tools_from_provider(provider)` | 批量从 Provider 导入 |
| `search(query, top_k)` | 向量语义搜索（有 EmbeddingClient）或关键词降级 |
| `load_tool(name)` | WARM/COLD → HOT 提升（从 ToolStore 加载） |
| `recycle(idle_threshold)` | 将超过 idle_threshold 轮未用工具 → COLD |
| `advance_turn()` | 推进全局使用轮次 |
| `record_usage(name, turn)` | 记录工具使用，更新最近使用轮次 |

工具库还支持标签查询和别名解析（由 ToolStore 持久化）。

---

## 4. ToolStore 版本管理（youmi/mcp/tool_store.py）

基于 SQLite + sqlite-vec 扩展的持久化存储层，维护 6 张表：

```
tools            — 工具主记录（name/description/parameters/version/parent_version_id）
vec_tools        — 向量索引（description 的 embedding，用于语义搜索）
tool_changelogs  — 同版本内变更日志（bug 修复说明）
tool_aliases     — 工具别名（alias_name → tool_name + version）
tool_tags        — 工具标签（多对多）
tool_dependencies— 工具依赖关系
```

关键能力：

| 方法 | 说明 |
|------|------|
| `upsert_tool(entry)` | 插入或更新工具（新版本插入即更新 vec_tools） |
| `create_version(name, new_version, change)` | 创建新版本，记录 parent_version_id |
| `get_version_chain(name)` | 获取完整版本历史链 |
| `add_changelog(name, change)` | 追加同版本变更日志 |
| `search(query, top_k)` | 向量语义或关键词搜索 |
| `add_alias / resolve_alias` | 别名管理 |
| `add_tag / search_by_tags` | 标签管理 |

`PostTaskPipeline` 累计失败超阈值时调用 `trigger_tool_version_update()` → 自动调用 `ToolStore.create_version()` 写入修复版本。

---

## 5. AgentToolContext Agent 侧上下文（youmi/mcp/context.py）

每个 Agent 维护独立的工具上下文视图（三级状态），与共享 ToolVault 分离：

```python
class AgentToolContext:
    def init_tools(self, hot, warm, cold, ...)  # 按白名单初始化
    def register_tool(name, tier)              # 手动注册某工具
    def get_tier(name) -> ToolContextTier      # 查询当前层级
    async def promote(name) -> bool            # WARM/COLD → HOT（从 Vault 加载 schema）
    def demote(name) -> bool                   # 降级
    def record_usage(name, turn)               # 记录使用
    def recycle(idle_threshold) -> list[str]   # 回收冷工具
    def advance_turn() -> int                  # 推进轮次
    def to_openai_tools() -> list[dict]        # 输出 HOT 工具的 LLM schema
    def to_warm_summaries() -> list[dict]      # 输出 WARM 工具摘要（提示 LLM 可用）
```

`ToolBridge.attach_vault(vault, essential_names)` 自动创建 AgentToolContext；必需工具（如内置工具）自动标记 HOT 不被回收。

---

## 6. 工具审批（youmi/mcp/approval.py）

`ApprovalManager` 独立管理三级审批决策与审计日志（被 `ToolApprovalMixin` 调用，详见 Master 文档第 5 节）。

`ToolIssueReport` 与 `ToolIssueType`（`youmi/mcp/protocol.py`）是 SubAgent 向 ToolGuardian 汇报工具问题的标准格式：

```python
class ToolIssueType(str, Enum):
    UNCLEAR_DESCRIPTION  # 描述不清
    PARAMETER_BOUNDARY   # 参数边界
    MISSING_FEATURE      # 功能缺失
    UNEXPECTED_BEHAVIOR  # 意外行为
    ERROR_HANDLING       # 错误处理不足
    OTHER
```

---

## 7. 工具发现与注册全流程

```
1. 用户/管理员                    向 MCPServer.register_provider() 注册 ToolProvider
                                   ↓
2. MCPService.setup()              自动导入到 ToolVault（add_tools_from_provider）
   (GUI 启动时)                    ↓ 异步向量化 + 存入 ToolStore
                                   ↓
3. Agent 运行中                    search_new_tools(query) 触发 ToolVault.search()
   工具不足                        → 返回候选 → Agent 选择 → load_tool() → HOT
                                   ↓
4. ToolBridge.call_tool()          白名单校验 → MCPClient.call → Provider.execute
                                   ↓
5. 失败累计                        PostTaskPipeline 沉淀经验 → 触发 trigger_tool_version_update
                                   ↓ ToolStore 写新版本 + GlobalMemory BUG_FIX 记录
```

---

## 8. MCPService GUI 集成层（gui/engine/mcp_service.py）

GUI 启动时 `EngineBridge.init()` 创建 `MCPService`，一站式初始化：

| 组件 | 细节 |
|------|------|
| MCPServer | 注册 BuiltinToolProvider + 各 Agent 的 LocalFunctionProvider |
| ToolStore | SQLite + sqlite-vec，默认路径 `.youmi_tools.db` |
| ToolVault | 接入 ToolStore 作为持久层 |
| EmbeddingClient | 连接本地/远程 embedding API，失败自动降级关键词搜索 |

`connect_agent(agent, mcp_server)` 为每个 Agent 建立 ToolBridge 并注入共享 Vault。

---

## 9. 相关文档

- [Agent_Introduction.md](Agent_Introduction.md) — 工具调用在 Agent ReAct 中的位置
- [Master_Introduction.md](Master_Introduction.md) — 工具审批决策与工作流权限回收
- [GlobalMemory_Introduction.md](GlobalMemory_Introduction.md) — 工具经验沉淀与修复闭环
