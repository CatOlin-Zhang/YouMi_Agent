# 生产基础设施详解（M1 / P1）

> 对应代码：`youmi/core/resilience.py`、`youmi/observability/`、`youmi/security/`、`youmi/gateway/`、`youmi/eval/`、`youmi/llm/mock_server.py`

M1 里程碑交付了生产加固三件套（可靠性 / 安全 / 可观测性），P1 补齐了部署形态（HTTP 任务网关 + 多租户）与质量保障（Mock LLM + Eval 基准）。本文档汇总这些横切基础设施的设计与接入点。

---

## 1. 可靠性：重试与熔断（youmi/core/resilience.py）

### retry_async — 异步重试执行器

```python
result = await retry_async(
    call_fn,                      # 待执行的异步可调用
    exceptions=(TimeoutError,),   # 可重试异常类型
    max_attempts=3,               # 最大尝试次数（含首次）
    backoff=BackoffConfig(strategy=BackoffStrategy.EXPONENTIAL, base_delay=1.0, max_delay=30.0),
    should_retry=callable,        # 自定义重试判定（可选）
    on_failure=callable,          # 每次失败回调（可选）
)
```

三种退避策略：`FIXED`（固定间隔）/ `LINEAR`（线性递增）/ `EXPONENTIAL`（指数退避，封顶 max_delay）。

### CircuitBreaker — 熔断器

三态状态机，防级联故障：

```
CLOSED（正常）──连续失败数 ≥ failure_threshold──▶ OPEN（熔断，直接拒绝）
OPEN ──冷却 recovery_timeout──▶ HALF_OPEN（放行试探请求）
HALF_OPEN ──试探成功──▶ CLOSED；──试探失败──▶ OPEN
```

`CircuitBreakerRegistry` 进程级注册表按名称管理多个熔断器实例。

### 接入点与关键语义

| 位置 | 行为 |
|------|------|
| `LLMClient.chat / chat_stream` | 重试退避 + 熔断；`chat_stream` 仅连接建立阶段（未 yield 首块前）可重试，重试时重置收集状态 |
| `ToolExecutor._do_execute_tool` | 熔断拒绝以**失败 ActionResult** 返回（不打断 ReAct 循环）；统一超时 `YOUMI_TOOL_TIMEOUT_S` |

关键语义：熔断检查在外层、整轮（含重试）计一次调用、被拒请求不计入统计；业务 4xx 类失败不触发熔断（`record_success` 语义——非基础设施故障）。

---

## 2. 可观测性（youmi/observability/）

### AuditLogger 审计日志（audit.py）

- **结构化事件**：`AuditEvent`（event_type / agent_id / tenant / task_id / tool_name / status / duration_ms / detail / error）；
- **存储**：内存环形缓冲（容量上限）+ 可选 JSONL 文件落盘（`YOUMI_AUDIT_LOG` 配置路径）；
- **脱敏**：`redact_data` 递归掩码敏感字段（password / token / api_key / secret 等）；
- **快捷方法**：`log_llm_call`（带 attempts）/ `log_tool_call` / `log_auth` 等；
- **进程级单例**：`get_audit_logger()` / `configure_audit_logger()` / `reset_audit_logger()`。

事件类型覆盖：LLM 调用、工具调用、认证、审批、沙箱拦截、召回审计（`tool.recall_audit`，来自 MCP `AuditGate`）。

### OTel 追踪（tracing.py）

- `setup_tracing()`：初始化 TracerProvider，支持 **OTLP 导出**（需 `[otlp]` 可选依赖）或降级 **JSONL** 导出（`JsonlSpanExporter`）；
- `span(name)`：异步上下文管理器，自动挂接当前 context；
- `set_span_attributes()` / `record_span_error()`：span 属性与错误记录辅助；
- 埋点覆盖：`llm.chat` / `llm.chat_stream` / `tool.call` 全链路 span，含 model / tool_name / duration / attempts 属性。

---

## 3. 安全（youmi/security/）

### AuthManager 认证与 RBAC（auth.py）

- **配置启用式**：`YOUMI_AUTH_TOKEN`（单 token）/ `YOUMI_AUTH_TOKENS`（多 token，支持 `token:role:tenant` 格式）；未配置时零摩擦放行（匿名 admin），本地开发无感；
- **三角色**：`admin` / `agent` / `viewer`；`Principal` 携带 role / tenant / 权限列表；
- **恒时比较**：`hmac.compare_digest` 防时序攻击；
- **复用面**：总线（BusServer/BusClient）、GUI（`/api/*` + `/ws`）、网关（全部端点）共用同一 AuthManager。

### Sandbox 策略式沙箱（sandbox.py）

```
SandboxPolicy:
  enabled             # 总开关
  denied_commands     # 命令黑名单（BUILTIN_DENIED_PATTERNS 内置黑名单始终生效：rm -rf / sudo 等）
  allowed_commands    # 白名单（配置后仅白名单可执行）
  allowed_roots       # 文件系统允许根目录
  network_blocked     # 网络命令拦截（curl / wget / nc 等）
  scrub_env           # 环境变量清理（敏感变量不透传子进程）
  max_timeout_s       # 超时钳制
  max_output_chars    # 输出大小钳制
```

通过 `policy_from_env()` 从环境变量注入，无需改代码。接入点：`shell_exec`（命令评估 + env 清理 + 超时/输出钳制）、文件工具（`check_path` 根目录约束）。违例抛 `SandboxViolation`。

---

## 4. 任务网关（youmi/gateway/，P1）

基于 **FastAPI + uvicorn** 的 HTTP 任务网关，`python -m youmi.gateway` 启动：

```bash
python -m youmi.gateway --host 0.0.0.0 --port 8000 --workers 4 --agent-name master
```

### 架构

```
FastAPI api.py（REST + SSE + 认证依赖）
   └─ GatewayService（service.py，门面）
        ├─ TaskQueue（queue.py：抽象 + InMemoryTaskQueue，可外接 Redis 等）
        ├─ TaskRegistry（registry.py：任务记录内存表，超限淘汰已完结）
        ├─ WorkerPool（worker.py：size 个 asyncio worker 循环消费队列）
        │    └─ TaskExecutor（executor.py：MasterTaskExecutor）
        │         ├─ 按租户懒创建 MasterAgent（factory 模式）
        │         ├─ asyncio.Lock 同租户串行执行
        │         └─ reset_between_tasks 任务间上下文隔离
        └─ TaskEventHub（events.py：发布订阅，驱动 SSE）
```

### REST 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康探活（免认证）|
| POST | `/tasks` | 提交任务（tenant / task 描述）→ task_id |
| GET | `/tasks` | 任务列表（按租户过滤，分页）|
| GET | `/tasks/{task_id}` | 查询任务状态与结果 |
| GET | `/tasks/{task_id}/events` | SSE 实时事件流（状态流转）|
| GET | `/stats` | 运行统计（admin 角色）|

认证复用 AuthManager（`Authorization: Bearer` / `?token=`），任务状态机：`QUEUED → RUNNING → COMPLETED / FAILED / CANCELLED`。

### 多租户传播链

认证主体的 `tenant` 沿调用链自动传播：网关请求 → MasterTaskExecutor（按租户建 Agent）→ 消息总线（`WorkflowMessage.tenant`，跨租户投递阻断）→ MemoryManager / GlobalMemory（会话与知识按租户隔离，`clone_for_tenant()` 共享连接获取租户视图）。

---

## 5. 质量保障（youmi/llm/mock_server.py + youmi/eval/）

### MockLLMServer — OpenAI 兼容 Mock 服务

真实 HTTP 形态（aiohttp），`LLMClient` 全链路可测：

- **脚本化**：FIFO 队列 `enqueue(MockResponse)` 或条件匹配 `when(...)` / `when_content_contains(...)`；
- **流式**：支持 SSE chunk 输出（`chunk_size` 控制）；
- **故障注入**：`status != 200` 错误注入、`delay_s` 延迟注入。

### Eval 基准框架（youmi/eval/）

`python -m youmi.eval` CLI 入口：

| 文件 | 职责 |
|------|------|
| `dataset.py` | `EvalStep` / `EvalTask` / `EvalDataset`（JSON/YAML 加载保存 + 内置 4 任务数据集）|
| `runner.py` | `EvalRunner`：自动启动 MockLLMServer 替代真实 LLM，保证可重复；集成审计日志成本汇总 |
| `scorer.py` | 评分：任务完成率 / 工具调用准确率 / token 成本（价格加权）/ 延迟估计 |
| `tools.py` | 确定性评测工具（get_weather / calculate / reverse_text / word_count）|

支持 `--min-completion` / `--min-tool-accuracy` 阈值（低于阈值 exit code 非 0，可接 CI）。

---

## 6. 实验框架（experiments/，独立于框架包）

两个创新点的可复现实验（详见 `experiments/README.md` / `REPORT.md` / `REPORT_REAL.md`）：

1. **Plan 记忆复用**（`plan_reuse_eval.py`）：合成基准 5 任务族 + 消融对比（有无 PlanMemory），指标含命中率 / 骨架保真率 / 价格加权成本节省 / 延迟节省；支持接入真实 Ollama 模型（`real_llm.py` + `run_real_eval.py`，「大模型规划 + 小模型适配」分层）；
2. **经验沉淀闭环**（`experience_eval.py`）：确定性模拟驱动真实组件（GlobalMemory / FeedbackCollector / ToolGuardian），量化「失败 → 沉淀 → 阈值 → 修复 → mark_resolved」学习曲线。

成本模型（`fake_llm.py`）：full（昂贵模型单价 1.0）vs adapt（廉价模型单价 0.05）价格加权记账；`paper_comparison.py` + `render_html.py` 生成与前沿工作（APC / AgentReuse / ExpeL 等）的对比 HTML 图表。

---

## 7. 配置项速查

| 环境变量 | 作用域 | 说明 |
|---------|--------|------|
| `YOUMI_AUTH_TOKEN` / `YOUMI_AUTH_TOKENS` | 认证 | 启用 token 认证（`token:role:tenant` 格式可指定角色与租户）|
| `YOUMI_TOOL_TIMEOUT_S` | 工具治理 | 工具调用统一超时（秒）|
| `YOUMI_AUDIT_LOG` | 审计 | JSONL 审计日志落盘路径 |
| `YOUMI_OTEL_SERVICE` / `YOUMI_OTEL_ENDPOINT` / `YOUMI_TRACE_FILE` | 追踪 | OTel 服务名 / OTLP endpoint（配置后 OTLP 导出）/ JSONL span 落盘路径 |
| `YOUMI_SANDBOX_*` | 沙箱 | `ENABLED` / `ROOTS` / `DENY` / `ALLOW` / `SCRUB_ENV` / `NETWORK_BLOCKED` / `MAX_TIMEOUT_S` / `MAX_OUTPUT_CHARS` |

---

## 8. 相关文档

- [Agent_Introduction.md](Agent_Introduction.md) — 治理接入点在 ReAct 循环中的位置
- [Message_Introduction.md](Message_Introduction.md) — 总线认证与租户隔离
- [MCP_Introduction.md](MCP_Introduction.md) — 召回审计闸门（AuditGate）
- [GUI_Introduction.md](GUI_Introduction.md) — GUI 认证中间件与健康检查
