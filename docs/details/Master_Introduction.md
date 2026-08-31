# MasterAgent 编排层详解

> 对应代码：`youmi/coordinator/master.py`、`youmi/coordinator/plan.py`、`youmi/coordinator/handoff.py`、`youmi/coordinator/post_task.py`、`youmi/coordinator/subprocess_agent.py`、`youmi/coordinator/tool_approval.py`、`youmi/tools/coordinator_ops.py`
> （本文档同时覆盖原《AgentFactory_Introduction》主题 — 子 Agent 工厂与生命周期，项目未设独立 AgentFactory 组件，工厂能力由 MasterAgent 承担）

MasterAgent 是系统的「主 Agent」：用户消息的唯一入口，负责分析任务、动态实例化子 Agent、审批工具申请、驱动工作流执行，并在任务结束后运行后台经验沉淀流水线。

---

## 1. 类结构

```python
class MasterAgent(Agent, ToolApprovalMixin):
    """继承 Agent 基类获得 ReAct/记忆/总线能力；
    混入 ToolApprovalMixin 获得三级审批能力"""
```

### 构造与配置

```python
master = MasterAgent(config, global_memory=None)          # 直接构造
master = MasterAgent.from_config_dir(dir_path, **kwargs)  # 从 youmi/agents/master/config.yaml 加载
```

`config.yaml` 关键配置段：

| 配置段 | 说明 |
|--------|------|
| `system_prompt` | 含动态角色查询引导（无硬编码角色列表，改为调用 `list_available_roles` 工具） |
| `llm_config` | 模型与参数 |
| `mcp_config` | `vault_enabled` / `embedding` / `db_path` / `auto_approve` / `sensitive` |
| `memory_config` | 记忆策略 |

`global_memory` 参数注入 GlobalMemory 实例（P6 全局记忆，详见 GlobalMemory_Introduction.md），任务结束时传递给 PostTaskPipeline。

### 核心属性

| 属性 | 说明 |
|------|------|
| `sub_agents: dict[str, SubAgentRecord]` | 子 Agent 注册表（agent_id → 记录） |
| `global_memory` | 全局记忆实例（可选） |
| `hook_registry` / `plugin_manager` | 继承自 Agent |

---

## 2. 子 Agent 工厂（动态实例化）

### 实例化流程

MasterAgent 自身通过 LLM 输出结构化的 `WorkflowPlan`（JSON），由 `WorkflowExecutor` 校验后实例化；运行期也可直接调用 `create_sub_agent()`：

```python
sub = await master.create_sub_agent(
    role="coder",                      # 角色标识（对应 youmi/agents/<role>/config.yaml）
    task="实现用户认证模块",             # 任务描述
    system_prompt="...",               # 可选覆盖
    allowed_tools=["file_read", ...],  # 工具白名单
)
```

安全约束：

1. **声明式配置** — Plan 中只允许角色名、prompt、工具白名单等声明字段，禁止嵌入可执行代码
2. **工具名校验** — WorkflowExecutor 校验 Plan 中引用的工具确实存在
3. **权限隔离** — 子 Agent 的 ToolBridge 仅能看到 `allowed_tools` 白名单内的工具
4. **任务简报注入** — `_TASK_BRIEF_TEMPLATE` 将任务上下文、汇报要求注入子 Agent system prompt
5. **自动接线** — 子 Agent 自动连接 Master 的 MCPServer 与消息总线（同 workflow_id）

### 子 Agent 生命周期

| 阶段 | 机制 |
|------|------|
| 创建 | `create_sub_agent()` → Agent 实例化 + `initialize()` |
| 执行 | `run_sub_agent(agent_id)` 单个执行；`run_all_sub_agents(parallel=True/False)` 批量执行 |
| 监控 | SubAgentRecord 记录状态（PENDING → RUNNING → COMPLETED/FAILED） |
| 回收 | 工作流结束后按逆创建顺序 `destroy()`；异常终止走 finally 兜底 |
| 权限重置 | `reset_for_new_task()` 调用子 Agent 的 `reset_tool_permissions()`，动态申请的工具权限恢复为初始白名单 |

### 工具申请监听

`_start_tool_request_listener()` 在后台监听总线上的 `TOOL_REQUEST` 消息；子 Agent 调用 `request_tool()` 后由 Master 走审批决策（见 §5）。

---

## 3. WorkflowPlan 与 WorkflowExecutor（youmi/coordinator/plan.py）

```python
class WorkflowPlan(BaseModel):
    steps: list[WorkflowStep]     # 步骤序列
    # WorkflowStep: step_id / agent_role / task / depends_on / allowed_tools

class WorkflowExecutor:
    async def execute(self) -> dict[str, StepResult]
```

能力：

- **DAG 校验**：`validate()` 做环检测（`_detect_cycle`），依赖缺失、agent_id 重复等错误返回错误清单
- **分层调度**：`get_execution_order()` 拓扑排序为执行层（同层无依赖可并行）
- **双执行模式**：`_execute_layer_serial()` 逐个执行 / `_execute_layer_parallel()` asyncio.gather 并行
- **结果保留**：`StepResult`（status/output/error/duration）始终保留已完成步骤产出
- **Agent 获取**：`get_agent(step_id)` 返回该步骤对应的子 Agent 实例

---

## 4. HandoffProtocol 任务委派（youmi/coordinator/handoff.py）

Agent 间任务委派协议，独立于 Master 也可使用：

| 方法 | 说明 |
|------|------|
| `register_agent(agent)` / `unregister_agent()` | 注册可被委派的 Agent |
| `await handoff(from_id, to_id, task, ...)` | 定向委派：发送任务消息并等待结果 |
| `await auto_handoff(task_description)` | 按能力描述自动匹配目标 Agent |
| `get_chain_depth(from_id, task_id)` | 委派链深度检测（防无限传递） |
| `snapshot()` | 协议状态快照 |

---

## 5. 三级工具审批（tool_approval.py + youmi/mcp/approval.py）

MasterAgent 混入 `ToolApprovalMixin`，审批决策委托独立的 `ApprovalManager`：

| 审批级别 | 触发条件 | 决策者 |
|---------|---------|--------|
| AUTO | 工具在 `auto_approve_list` 白名单内 | 自动通过 |
| MANUAL | 工具在 `sensitive_tools` 敏感清单内 | 进入 `manual_review_queue` 等待人工/GUI 确认 |
| MASTER | 子 Agent 申请超出授权范围的工具 | MasterAgent LLM 决策 |

- `ApprovalManager.evaluate(agent_id, tool_name)` 返回 `ApprovalLevel`
- `submit_request()` / `approve()` / `deny()` 生成 `ApprovalRecord` 审计记录
- `master.get_approval_audit_log()` 查询审计日志

MasterAgent 内置 `approve_tool_request` / `deny_tool_request` 两个工具（coordinator_ops.py），LLM 可在对话中直接执行审批。

---

## 6. 进程隔离子 Agent（subprocess_agent.py）

`SubProcessAgentRunner` 基于 `asyncio.create_subprocess_exec` 在独立 Python 进程中运行子 Agent：

- 入口 `youmi/coordinator/_subprocess_entry.py`
- `SubProcessHandle` 管理子进程句柄、stdin/stdout 通信、退出码
- 隔离崩溃传播：子 Agent 异常不影响主进程
- 适用于运行不可信/资源密集型任务

---

## 7. 多轮任务循环

```python
async def conversation_loop(self): ...
```

- 支持同一 Master 会话内连续接收多个用户任务
- 每个新任务开始时 `reset_for_new_task()`：清除上一工作流状态、重置子 Agent 工具权限
- 任务结束时 `on_stop()` 触发 PostTaskPipeline（见 §8）

---

## 8. PostTaskPipeline 后台流水线（post_task.py）

任务结束后 MasterAgent 的 `on_stop()` 自动构建并运行四阶段流水线（ToolStore 从 `self._tool_bridge.vault.store` 自动发现）：

```
阶段1  collect_tool_experiences()   从各 SubAgent 对话记录提取工具调用模式
阶段2  summarize_task_outcomes()    汇总任务结果生成结构化摘要
阶段3  update_tool_notes()          向 ToolGuardianAgent 汇报高失败率工具
阶段4  update_global_memory()       经验沉淀到 GlobalMemory（P6）：
                                    ├─ 工具使用统计经验写入（ToolExperienceExtractor）
                                    ├─ 高失败率工具（成功率<0.7）失败根因分析
                                    └─ 累计失败≥3 且成功率<0.5 → trigger_tool_version_update()
                                       触发成功后记录 BUG_FIX 经验并重置失败计数
```

流水线任何阶段失败仅记录 warning，不影响任务结果返回。

---

## 9. MasterAgent 内置协调工具（coordinator_ops.py）

`register_coordinator_tools(master)` 注册 6 个工具供 Master LLM 调用：

| 工具 | 说明 |
|------|------|
| `create_sub_agent(role, task, system_prompt?, allowed_tools?)` | 创建子 Agent |
| `run_sub_agent(agent_id)` | 运行指定子 Agent |
| `list_sub_agents()` | 列出子 Agent 及状态 |
| `list_available_roles()` | 列出可用角色模板（从 youmi/agents/ 动态发现） |
| `approve_tool_request(agent_id, tool_names)` | 批准工具申请 |
| `deny_tool_request(agent_id, reason)` | 拒绝工具申请 |

---

## 10. 典型时序

```
用户消息 → MasterAgent.chat_turn_stream()
  │
  ├─ LLM 分析 → 决定需要哪些子 Agent
  ├─ tool: create_sub_agent(role="coder", task=...)     × N
  ├─ tool: run_sub_agent(agent_id) / 并行 run_all
  │     └─ 子 Agent ReAct 循环（工具调用走 MCP，失败可 request_tool）
  ├─ （监听器收到 TOOL_REQUEST → 审批 → TOOL_RESPONSE）
  ├─ 子 Agent 完成 → 结果反馈 Master
  └─ Master 汇总回复用户 → on_stop() → PostTaskPipeline 经验沉淀
```

---

## 11. 相关文档

- [Agent_Introduction.md](Agent_Introduction.md) — Agent 基类与 ReAct
- [MCP_Introduction.md](MCP_Introduction.md) — ToolBridge 与权限链路
- [GlobalMemory_Introduction.md](GlobalMemory_Introduction.md) — 经验沉淀与修复闭环
- [Message_Introduction.md](Message_Introduction.md) — TOOL_REQUEST/RESPONSE 消息协议
