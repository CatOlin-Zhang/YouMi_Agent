# 消息总线详解

> 对应代码：`youmi/bus/`（message.py / broker.py / server.py / ws_client.py）、`youmi/core/agent.py`（消息集成方法）

消息总线是多 Agent 通信的基础设施。所有 Agent 间的工作交接、工具申请、状态通知均通过消息总线传递，保证消息不丢失、工作流隔离、可靠投递。

---

## 1. 消息结构（youmi/bus/message.py）

### WorkflowMessage

```python
class WorkflowMessage(BaseModel):
    msg_id: str                     # UUID，全局唯一
    from_agent_id: str              # 发送方
    to_agent_id: str | None         # 接收方；None = 工作流广播
    workflow_id: str                # 工作流 ID，隔离不同工作流的消息
    msg_type: WorkflowMessageType   # 消息类型（见下表）
    content: str                    # 消息主体
    metadata: dict                  # 扩展元数据（任务 ID、trace_id 等）
    timestamp: datetime             # 创建时间
```

### WorkflowMessageType 枚举

| 类型 | 用途 | 是否写入记忆 |
|------|------|-------------|
| `task` | 任务指派（Master → SubAgent） | ✅ |
| `feedback` | 结果反馈（SubAgent → Master） | ✅ |
| `status` | 状态通知（如 RUNNING/COMPLETED） | ❌（仅入队） |
| `query` | 查询/问答 | ❌（仅入队） |
| `tool_request` | 工具权限申请（SubAgent → Master） | ❌ |
| `tool_response` | 工具权限回复（Master → SubAgent） | ❌ |

`task` 和 `feedback` 消息在 `Agent.receive_message()` 时会写入 MemoryManager，其他类型仅放入接收队列。

### BusEnvelope（传输信封）

WebSocket 远程通信时的外层封装，包含序列化后的 WorkflowMessage 与投递元数据（retry_count、delivered_at 等）。

---

## 2. MessageBroker 抽象接口

```python
class MessageBroker(ABC):
    async def subscribe(agent_id, workflow_id)    # 注册 Agent 到工作流
    async def unsubscribe(agent_id)              # 注销
    async def publish(message)                   # 投递消息
    async def wait_for_message(agent_id, timeout) # 阻塞等待下一条
    async def pending_messages(agent_id)          # 拉取待处理消息列表
    async def ack(agent_id, message_id)           # 确认消息已处理
    async def create_workflow() -> str            # 创建工作流，返回 workflow_id
```

---

## 3. InProcessBroker 进程内实现（broker.py）

基于 `asyncio.Queue` 的进程内实现，无需网络，是单机多 Agent 场景（包括 GUI）的默认选择：

**可靠性保证：至少一次投递**

```
publish(msg)
    → 放入目标 Agent 的 asyncio.Queue
    → Agent.receive_message(msg) 成功返回 → ack(msg_id) → 标记已消费
    → Agent 异常时消息保留队列，恢复后重新投递
```

**广播语义**：`to_agent_id=None` 的消息只推送给同一 `workflow_id` 下所有已订阅 Agent，不跨工作流传播。

**回调支持**（观察者模式）：注册消息到达回调（GUI 用此监听 Agent 间通信转发前端事件）。

---

## 4. BusServer / BusClient WebSocket 实现（server.py / ws_client.py）

分布式场景（跨进程/跨机器 Agent）时使用：

```
BusServer ─── 包装 InProcessBroker → WebSocket 服务端
                  │ ws://host:port/ws
BusClient ─── 实现 MessageBroker 接口 → WebSocket 客户端（支持断线重连）
```

BusClient 对 Agent 完全透明：Agent 代码无需区分进程内或远程 Broker，接口一致。

BusServer 启动：

```python
server = BusServer(broker=InProcessBroker())
await server.start(host="0.0.0.0", port=8765)
```

BusClient 连接（子 Agent 进程隔离时使用）：

```python
client = BusClient(ws_url="ws://localhost:8765/ws")
await agent.connect_bus(client, workflow_id="wf_001")
```

---

## 5. Agent 消息 API 集成

```python
# 连接总线（EngineBridge 在 GUI 中自动完成）
await agent.connect_bus(broker, workflow_id="wf_001")

# 发送消息
await agent.send_message(
    to_agent_id="sub_agent_001",
    content="请完成代码审查任务",
    msg_type=WorkflowMessageType.TASK,
)

# 广播（同工作流所有 Agent）
await agent.send_message(to_agent_id=None, content="任务已完成")

# 等待消息（超时 30s）
msg = await agent.wait_for_message(timeout=30.0)

# 查询积压消息（非阻塞）
messages = await agent.pending_messages(agent.agent_id)
```

属性 `agent.bus`（MessageBroker 实例）与 `agent.workflow_id` 在 `connect_bus()` 后可用。

---

## 6. 工具申请流程（消息层视角）

```
SubAgent.request_tool("需要 PDF 解析工具", reason)
    │
    ├─ 发送 TOOL_REQUEST 消息 → MasterAgent 的队列
    │    metadata: {tool_description, reason, agent_id}
    │
    └─ await wait_for_message(timeout)  等待审批结果

MasterAgent._tool_request_listener（后台循环）
    │
    ├─ 收到 TOOL_REQUEST → ApprovalManager.evaluate()
    │    ├─ AUTO     → add_allowed_tool() → 发送 TOOL_RESPONSE(approved=True)
    │    ├─ MANUAL   → 加入 manual_review_queue，等待人工/LLM 调用 approve_tool_request
    │    └─ MASTER   → LLM 决策
    │
    └─ 发送 TOOL_RESPONSE 消息 → SubAgent 的队列
         metadata: {approved, tool_names, reason}

SubAgent 收到 TOOL_RESPONSE
    ├─ approved=True  → request_tool() 返回 True → 工具可用
    └─ approved=False → request_tool() 返回 False → 继续执行或报告
```

---

## 7. 工作流隔离设计

每次 `create_workflow()` 生成唯一 `workflow_id`。所有消息携带 `workflow_id` 字段，Broker 路由时只投递给同 `workflow_id` 下订阅的 Agent。`reset_for_new_task()` 触发新工作流 ID，物理隔离不同会话的消息队列。

---

## 8. GUI 总线集成

`EngineBridge.init()` 创建 `InProcessBroker`，Master 与所有子 Agent 均通过 `_patch_create_sub_agent()` 自动 `connect_bus()`，无需手动调用。

GUI 监听 Broker 回调（观察者模式）以捕获 Agent 间通信，尚未实现全量转发到前端（`total_bus_events` 转发为 `agent_message` WebSocket 事件列为待实现项）。

---

## 9. 相关文档

- [Agent_Introduction.md](Agent_Introduction.md) — Agent 收发消息方法
- [Master_Introduction.md](Master_Introduction.md) — 工具申请审批的 Master 侧处理
- [GUI_Introduction.md](GUI_Introduction.md) — EngineBridge 与总线的 GUI 集成
