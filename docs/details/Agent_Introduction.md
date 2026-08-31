# Agent 基类与 ReAct 运行时详解

> 对应代码：`youmi/core/agent.py`（约 1700 行）及其协作模块
> `youmi/core/hooks.py`、`youmi/core/plugin.py`、`youmi/core/prompt.py`、`youmi/memory/compaction.py`、`youmi/scheduler/__init__.py`

Agent 是框架的核心执行实体。所有 Agent（包括 MasterAgent、ToolGuardianAgent、普通 SubAgent）都继承自该基类，通过组合而非继承获得记忆、工具、消息、Hook、插件、调度等能力。

---

## 1. 状态机与生命周期

```python
# youmi/core/models.py
class AgentStatus(str, Enum):
    IDLE = "idle"            # 空闲
    RUNNING = "running"      # ReAct 循环执行中
    WAITING = "waiting"      # 等待其他 Agent 或外部资源
    COMPLETED = "completed"  # 任务完成
    FAILED = "failed"        # 任务失败
    DESTROYED = "destroyed"  # 已销毁
```

生命周期方法：

| 方法 | 说明 |
|------|------|
| `await initialize()` | 初始化记忆、Hook、插件；幂等 |
| `await run(task, task_id="")` | 执行完整任务：工作流自检 → ReAct 循环 → 返回 `TaskResult` |
| `await chat_turn(message)` | 单轮对话（GUI 单聊模式） |
| `chat_turn_stream(message)` | 单轮流式对话（异步生成器，逐块产出文本增量） |
| `await destroy()` | 释放资源、断开连接；触发 `on_destroy` 生命周期钩子 |

子类可覆盖的生命周期钩子：`on_initialize()` / `on_start()` / `on_stop()` / `on_destroy()`。

---

## 2. ReAct 循环

`run()` 内部驱动四阶段循环，直到目标达成或达到 `max_iterations`（默认 20，可由 `AgentConfig.max_iterations` 覆盖）：

```
┌────────────────────────────────────────────────┐
│  _observe()  接收任务/消息，读取记忆上下文        │
│      ↓                                         │
│  _think()    LLM 推理（含 function calling）     │
│      ↓         ├─ 有 tool_calls → 继续          │
│      │         └─ 纯文本回复 → 循环结束          │
│  _act()      通过 ToolBridge/ToolRegistry 执行   │
│      ↓       工具调用，结果写回记忆               │
│  _reflect()  评估进展，决定是否继续循环            │
└───────────────┬────────────────────────────────┘
                ↓ 未达成目标则回到 _observe
```

关键行为：

- **工具热更新兼容**：`_think()` 每轮重新调用 `to_openai_tools()` 获取工具 schema，运行期动态授权的工具下一轮即对 LLM 可见，无需中断循环。
- **工具不足兜底**：Agent 自动携带 `search_new_tools` 兜底工具，ReAct 循环中工具不够用时主动向 MCP 层发起检索（ToolVault 向量搜索，无 Vault 时回退 ToolRegistry 关键词匹配）。
- **上下文压缩**：每轮对话后经 `ContextCompactor.maybe_compact()` 检查 token 预算，超限时将早期消息压缩为摘要（详见记忆系统文档）。

---

## 3. 双模式工具接入

Agent 支持**有/无 MCP** 两种模式，同一套 `_act()` 逻辑无缝切换：

| 模式 | 接入方式 | 工具来源 |
|------|---------|---------|
| MCP 模式 | `connect_mcp(server)` | ToolBridge → MCPServer（权限白名单） |
| 本地模式 | 不连接 MCP | 自带 `ToolRegistry`（全量注册工具） |

连接 MCP 时，本地已注册工具自动迁移为 `LocalFunctionProvider` 注册到 MCPServer。`register_builtin_tools()` 会注册 9 个内置工具（见 MCP 文档工具清单）。

### 工具权限申请（SubAgent）

```python
await agent.request_tool(tool_description="需要读取 PDF 的工具", reason="任务要求解析简历")
```

`request_tool()` 通过消息总线向 MasterAgent 发送 `TOOL_REQUEST` 消息；Master 决策后回 `TOOL_RESPONSE`，批准则调用 `ToolBridge.add_allowed_tool()` 立即生效。`reset_tool_permissions()` 将白名单恢复为初始授权（工作流级权限回收）。

---

## 4. 消息能力（消息总线集成）

| 方法 | 说明 |
|------|------|
| `connect_bus(broker, workflow_id)` | 连接 MessageBroker（InProcessBroker 或 BusClient），加入指定工作流 |
| `await send_message(to_agent_id, content, msg_type)` | 定向发送（`to_agent_id=None` 为工作流内广播） |
| `await receive_message(msg)` | 接收消息；`task`/`feedback` 写入记忆，`status`/`query` 仅入队 |
| `await pending_messages(agent_id)` | 拉取待处理消息 |
| `await wait_for_message(timeout)` | 阻塞等待下一条消息 |

属性 `agent.bus` 与 `agent.workflow_id` 由 `connect_bus()` 设置。

---

## 5. 工作流自检（TaskSelfCheck）

`run()` 启动前执行 prompt 级自检：`_self_check_task()` 让 LLM 评估「当前授权工具是否足以完成任务」，不足时提前触发 `search_new_tools` 或向 Master 求助，避免进入死循环后才发现工具缺失。

---

## 6. Hook 系统（youmi/core/hooks.py）

7 个挂载点贯穿 Agent 运行全程：

```python
class HookType(str, Enum):
    BEFORE_PROMPT_BUILD = "before_prompt_build"   # system prompt 组装前
    BEFORE_MODEL_CALL  = "before_model_call"     # LLM 调用前
    AFTER_MODEL_CALL   = "after_model_call"      # LLM 调用后
    BEFORE_TOOL_CALL   = "before_tool_call"      # 工具调用前
    AFTER_TOOL_CALL    = "after_tool_call"       # 工具调用后
    MESSAGE_RECEIVED   = "message_received"      # 收到总线消息
    MESSAGE_SENDING    = "message_sending"       # 发送总线消息前
```

Hook 处理器返回 `HookDecision`：

- `PASS` — 不干预，继续执行
- `MODIFY` — 修改数据（如改写 prompt、替换工具入参）后继续
- `BLOCK` — 终止当前操作

`HookRegistry.register(hook_type, handler, priority)` 支持优先级排序与批量移除。GUI 的实时气泡推送（`gui/engine/hook_bridge.py`）即通过在所有 Agent 上注册 `MESSAGE_SENDING` / `BEFORE_TOOL_CALL` / `AFTER_TOOL_CALL` 处理器实现。

---

## 7. 插件系统（youmi/core/plugin.py）

`Plugin` 抽象基类只需实现 `name()` 与 `setup(hook_registry)`；`PluginManager` 负责注册与生命周期管理，插件在 `setup()` 中向 `HookRegistry` 挂载处理器，实现无侵入扩展（如审计日志、内容过滤）。

---

## 8. Prompt 动态组装（youmi/core/prompt.py）

`PromptAssembler` 以命名层（`PromptLayer`）管理 system prompt 片段：

- `add_layer(layer)` / `remove_layer(name)` — 增删层（如任务简报、工具使用须知、角色补充）
- `assemble(max_tokens=0)` — 按 token 预算拼接全部层，超限时从低优先级层开始裁剪
- `from_system_prompt(text)` — 从静态 prompt 构建初始层
- 每层带 `estimated_tokens` 估算，`estimated_total_tokens()` 汇总

---

## 9. 上下文压缩（youmi/memory/compaction.py）

`ContextCompactor` 独立于记忆策略工作：

| 方法 | 说明 |
|------|------|
| `needs_compaction(messages)` | 估算消息总 token 是否超过 `max_tokens` |
| `await maybe_compact(messages)` | 超限时调用 LLM 生成早期消息摘要，替换原消息序列 |
| `current_summary` | 当前压缩摘要 |
| `compaction_count` | 累计压缩次数 |

---

## 10. 心跳调度（youmi/scheduler/__init__.py）

`HeartbeatScheduler` 为 Agent 提供周期性后台任务：

- `ScheduledTask` 声明任务名、间隔、handler、启用状态
- `add_task()` / `remove_task()` / `enable_task()` 管理任务
- `await start()` 启动全部任务循环；`await stop()` 优雅停止
- `bind_agent(agent)` 绑定宿主 Agent（handler 可访问 Agent 上下文）

---

## 11. AgentConfig 关键字段

```python
@dataclass
class AgentConfig:
    name: str                      # Agent 名称
    system_prompt: str             # 系统提示词
    llm_config: LLMConfig          # 模型/温度/max_tokens/base_url
    memory_config: MemoryConfig    # 记忆策略与持久化配置
    allowed_tools: list[str]       # 初始工具白名单
    max_iterations: int = 20       # ReAct 最大迭代
    metadata: AgentMetadata        # display_name/role/description
```

角色模板以 YAML 声明（`youmi/agents/<role>/config.yaml`），由 `MasterAgent.from_config_dir()` / `load_agent_config()` 加载。

---

## 12. 相关文档

- [Master_Introduction.md](Master_Introduction.md) — MasterAgent 编排与子 Agent 工厂
- [MCP_Introduction.md](MCP_Introduction.md) — 工具调用链路与权限
- [Message_Introduction.md](Message_Introduction.md) — 消息总线协议
- [Memory_Introduction.md](Memory_Introduction.md) — 记忆策略与持久化
