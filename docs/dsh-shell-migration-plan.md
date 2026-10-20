# YouMi Agent × DeepSeek Harness 迁移计划（dsh-shell 分支）

- 状态：计划定稿，待执行
- 分支：`dsh-shell`（与远程 `origin/dsh-shell` 对应）
- 基线：`master` @ `01c82de`（0.0.5），保持原形态冻结
- 调研基线：`deepseek-ai/deepseek-harness` master（developer preview，2026-10 拉取）

## 1. 背景与结论

对比结论：DSH 在宿主能力上更完整，YouMi 在工作流与知识沉淀上更强。因此两种形态不重叠：

- **DSH 提供宿主**：Cordis 全插件架构（无特权核心），`core/agent-loop`、`core/session`（append-only 日志，model-visible 即可重建）、`core/tools`（`tools/pre-execute → tools/execute → tools/post-execute → tools/result` 治理管线）、`subagent`、`approval`、`sandbox`、Web UI（`dsh web`，默认 127.0.0.1:3080）。
- **YouMi 提供能力**：工具目录锥形检索（ToolStore / ConeRetriever / ToolVault）、Skill 索引（SkillStore）、计划复用（PlanMemory）、失败经验闭环（GlobalMemory + ExperienceExtractor）。
- **接入方式**：YouMi 收敛为 Python MCP 能力服务（stdio JSON-RPC），DSH 通过自带 `@deepseek-ai/dsh-mcp-client` 挂载；需要钩子的能力由 `youmi-bridge` TS 薄插件挂在 DSH 文档化扩展点上。

目标形态一句话：**DSH 是宿主，YouMi 是能力服务；YouMi 不再自建 agent loop、消息总线与 GUI。**

## 2. 分支与仓库策略

| 分支 | 形态 | 规则 |
|---|---|---|
| `master` | 原形态：Python 多 Agent 框架 + aiohttp GUI | 冻结：只接受 bug fix 与安全修复，不再新增宿主能力 |
| `dsh-shell` | DSH 形态：TS 插件 + Python 能力服务 | 承载本次迁移全部工作，对应远程同名分支 |

- 两分支共享的"纯 Python 库"层保持无宿主依赖，便于双向 cherry-pick：`youmi/mcp/tool_store.py`、`youmi/mcp/skill_store.py`、`youmi/mcp/cone.py`、`youmi/mcp/vault.py`、`youmi/knowledge/global_memory.py`、`youmi/coordinator/plan_memory.py`。
- 禁止在 `dsh-shell` 上继续演进 `MasterAgent`、ReAct loop、GUI 引擎层。
- 迁移不 fork DSH 仓库、不修改其内核；DSH 代码只作为 npm 依赖 / CLI 运行时使用。

## 3. 目标架构

```text
┌─────────────────────── DSH 宿主（Node 22+，TypeScript，Cordis）───────────────────────┐
│  dsh web（127.0.0.1:3080）   Web UI（host/client）                                     │
│  core/agent-loop   core/session   core/tools   subagent   approval   sandbox          │
│  mcp-client（serverName=youmi，stdio）                                                 │
└──────────────────────────────┬────────────────────────────────────────────────────────┘
                               │ newline-delimited JSON-RPC 2.0（stdio）
                               ▼
┌───────────────────── YouMi Capability Server（Python）────────────────────────────────┐
│  youmi.mcp.stdio_server → MCPServer.handle → ToolProvider 路由                        │
│  youmi_tool_search / youmi_tool_load        （工具目录检索）                             │
│  youmi_skill_search / youmi_skill_load      （Skill 索引）                              │
│  youmi_plan_search / youmi_plan_save        （计划复用）                                │
│  youmi_experience_search / youmi_experience_add（失败经验）                             │
│  数据层：ToolStore / SkillStore / PlanMemory / GlobalMemory（SQLite + sqlite-vec）       │
└──────────────────────────────▲────────────────────────────────────────────────────────┘
                               │ agent/pre-step、tools/result、turn/end
┌──────────────────────────────┴────────────────────────────────────────────────────────┐
│  youmi-bridge（TS 薄插件，独立 npm 包）                                                 │
│  youmi-tool-catalog：agent/pre-step → ctx.tools.restrict() 渐进式工具暴露               │
│  youmi-plan-recall：agent/pre-step 注入相似计划；turn/end 归档计划轨迹                  │
│  youmi-experience：tools/result 捕获失败；agent/pre-step 注入经验                       │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

## 4. 能力迁移映射

| 能力 | YouMi 现状 | DSH 落点 | 迁移形态 |
|---|---|---|---|
| 工具目录检索 | `youmi/mcp/tool_store.py`（`ToolStore.search_cone`）、`cone.py`（`ConeRetriever`）、`vault.py`（`ToolVault` HOT/WARM/COLD） | MCP 工具 `youmi_tool_search` / `youmi_tool_load` + `youmi-tool-catalog` 插件调用 `ctx.tools.restrict()` | 服务化 + TS 插件 |
| Skill 索引 | `youmi/mcp/skill_store.py`（`SkillStore.search_cone`、`resolve_bound_tool`）、`skill_ingest.py` | MCP 工具 `youmi_skill_search` / `youmi_skill_load`；内容经 section provider 或工具结果注入 | 服务化 + TS 薄插件（复用 DSH `skill` 包） |
| 计划复用 | `youmi/coordinator/plan_memory.py`（`PlanMemory.search_plan`，阈值 0.85）、`planner.py` | 插件 `agent/pre-step` 注入；`turn/end` 归档；MCP `youmi_plan_search` / `youmi_plan_save` | 服务化 + TS 插件 |
| 失败经验 | `youmi/knowledge/global_memory.py`（`search` / `batch_add`）、`experience_extractor.py`（`extract`） | 插件监听 `tools/result` 捕获失败、`turn/end` 入库、`agent/pre-step` 注入；MCP `youmi_experience_search` / `youmi_experience_add` | 服务化 + TS 插件 |
| 工具审批 | `youmi/mcp/approval.py`（`ApprovalManager` 三级审批） | `tools/pre-execute` 返回 `ask` + `ctx.approval` | 退役，改用 DSH 原生 |
| 沙箱 | `youmi/security/sandbox.py` | `ctx.sandbox` 后端（landlock / sandbox-exec） | 退役，改用 DSH 原生 |
| 多 Agent 协作 / 委派 | `youmi/bus/broker.py`、`youmi/coordinator/handoff.py` | `ctx.subagents` + `dsh-tool-subagent`（可选 Agent Teams） | 退役 |
| Agent loop | `youmi/core/agent.py` ReAct、`coordinator/master.py` | `core/agent-loop`（turn/step 流水线） | 退役 |
| Agent 私有记忆 / 压缩 | `youmi/memory/*` | `core/session` + `ctx.compaction` | 退役 |
| GUI | `gui/server.py`、`gui/engine/bridge.py` | `dsh web`（host/client + WebSocket/SSE 事件流） | 退役（仅保留在 `master`） |
| 可观测性 | `youmi/observability/*`（JSONL 审计 + OTel） | DSH telemetry + `session/event` 派生；需要业务审计时落 `youmi_experience_add` | 部分保留 |

## 5. MCP 服务契约（serverName = `youmi`）

- 传输：stdio，newline-delimited JSON-RPC 2.0；复用现有 `MCPServer.handle()`，只需新增 transport 壳与能力 Provider。
- 入口：`python -m youmi.mcp.stdio_server`；`pyproject.toml` 增加 `[project.scripts] youmi-mcp = "youmi.mcp.stdio_server:main"`。
- 环境变量：`YOUMI_DB_PATH`（默认 `~/.youmi/youmi.db`）、`YOUMI_TENANT_ID`、`YOUMI_LOG_LEVEL`。
- 工具名在 DSH 模型侧显示为 `mcp__youmi__<rawName>`；`serverName` 与工具名是稳定契约（会话历史与权限规则依赖），只增不改，改名走"新工具 + 弃用期"。

| MCP 工具（rawName） | 入参 | 出参 | 数据源 |
|---|---|---|---|
| `youmi_tool_search` | `query: str, top_k: int = 5, risk_ceiling: str = ""` | `[{name, score, summary, definition}]` | `ToolStore.search_cone` / `ConeRetriever` |
| `youmi_tool_load` | `name: str` | `{name, definition, schema}` | `ToolVault` / `ToolStore` |
| `youmi_skill_search` | `query: str, top_k: int = 5` | `[{skill_id, name, score, summary}]` | `SkillStore.search_cone` |
| `youmi_skill_load` | `skill_id: str` | `{name, content(SOP), bound_tool}` | `SkillStore` |
| `youmi_plan_search` | `task: str, top_k: int = 3, min_score: float = 0.85` | `[{plan_id, similarity, skeleton, steps}]` | `PlanMemory.search_plan` |
| `youmi_plan_save` | `task: str, plan_json: dict, outcome: str` | `{plan_id}` | `PlanMemory.save_plan` |
| `youmi_experience_search` | `query: str, top_k: int = 5` | `[{id, text, score, tags}]` | `GlobalMemory.search` |
| `youmi_experience_add` | `task: str, failure: str, fix: str, tags: list[str]` | `{id}` | `ExperienceExtractor` + `GlobalMemory.batch_add` |

## 6. TS 薄插件：`youmi-bridge`

目录：`dsh-plugins/youmi-bridge/`（独立 npm 包；`peerDependencies: @deepseek-ai/cordis`；ESM）。

| 插件 | 扩展点 | 行为 |
|---|---|---|
| `youmi-tool-catalog` | `agent/pre-step` | 取当前用户输入调用 `youmi_tool_search`，命中后 `ctx.tools.restrict()` 收紧可见工具集；保留 essential 工具；检索失败则不 restrict（降级为全量） |
| `youmi-plan-recall` | `agent/pre-step`、`turn/end` | pre-step 调 `youmi_plan_search`（`min_score` 默认 0.85），命中经 `agent.inject()` 注入计划骨架（附 `plan_id`）；turn/end 从 session event 提取步骤轨迹并 `youmi_plan_save` |
| `youmi-experience` | `tools/result`、`turn/end`、`agent/pre-step` | `tools/result` 观察不可变终态并缓冲失败证据；turn/end 调 `youmi_experience_add`；pre-step 调 `youmi_experience_search` 注入 Top-K（默认预算 2000 字符） |

- 配置项（`cordis.yml` 的 plugin `config`）：`youmiServerName`（默认 `youmi`）、`planMinScore`（0.85）、`injectBudgetChars`（2000）、`enableToolCatalog`（true）。
- 所有注册走 `ctx.effect()` / `ctx.on()`，卸载即回收；waterfall 监听必须调用 `next()`。
- 不新增 session 事件类型，不改 `agent-loop`；计划状态复用 DSH `plan` 包的 durable 状态。

## 7. DSH 接入配置（profile patch 示例）

```yaml
# profiles/youmi.patch.yml
- id: mcp-youmi
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: youmi
    transport: stdio
    command: python
    args: ['-m', 'youmi.mcp.stdio_server']
    env:
      YOUMI_DB_PATH: !!js process.env.YOUMI_DB_PATH
    toolCallTimeoutMs: 60000
    failOnStartupError: false
    reconnect:
      enabled: true

- id: youmi-bridge
  name: 'youmi-bridge'
  config:
    enableToolCatalog: true
    planMinScore: 0.85
    injectBudgetChars: 2000
```

启动方式：`dsh web --patch profiles/youmi.patch.yml`；`dsh --profile web --dump-config` 可校验插件树。

## 8. 分阶段实施

### M0 骨架（本分支首个 PR）
- 计划文档、目录骨架（`dsh-plugins/youmi-bridge/`、`youmi/mcp/stdio_server.py`、`profiles/youmi.patch.yml`）。
- `stdio_server` 仅实现 `initialize` / `tools/list`（空表）/ `ping`。
- 验收：`dsh` 启动后 MCP 连接成功（工具列表为空但无错误）；`pytest` 冒烟通过。

### M1 能力服务化
- 新增四个能力 Provider（tool-catalog / skill-index / plan-memory / experience），实现第 5 节 8 个工具；租户与错误码透传。
- 验收：每个工具单测；`dsh` 会话内实际调用成功；现有数据层表结构零改动。

### M2 工具目录检索插件
- `youmi-tool-catalog` + `ctx.tools.restrict()`；记录 restrict 前后工具数。
- 验收：同一任务工具集由全量收敛到 Top-K 候选；session log 可完整重建每次 restrict 依据。

### M3 计划复用与经验闭环
- `youmi-plan-recall`、`youmi-experience`。
- 验收：相似任务命中计划（相似度 ≥ 0.85）；失败任务后经验入库，且下一次同类任务 pre-step 注入命中。

### M4 退役与收口
- 删除 `gui/`、`youmi/coordinator/`、`youmi/core/agent.py`、`youmi/bus/`、`youmi/memory/`、`youmi/security/sandbox.py` 等宿主职责代码；清理对应测试。
- README 改写为 DSH 形态；CI 调整为 `pytest` + `pnpm --filter youmi-bridge build`。
- 验收：`dsh web` 端到端（任务 → 工具检索 → 计划复用 → 经验沉淀）跑通。

## 9. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| DSH 处于 developer preview，存在破坏性变更 | 插件/配置失配 | 锁定 dsh 版本；只用文档化扩展点；升级前跑会话快照 |
| 双栈运维（Node + Python） | 部署与排障复杂 | 单 sidecar 进程；`mcp-client` 自动重连；健康检查工具 `youmi_ping` |
| MCP 工具名契约被破坏 | 会话历史与权限规则失效 | `serverName` 固定 `youmi`；工具名只增不改 |
| 现有 SQLite 数据不兼容 | 历史能力数据不可用 | 复用现有 store 与表结构，迁移期只加索引不改列 |
| 团队不熟 Cordis | 插件开发效率低 | 计划内附 cookbook 链接；`youmi-bridge` 保持 3 个文件的最小形态 |
| `dsh-shell` 方向失败 | 投入浪费 | `master` 完整保留原形态；能力层与宿主层解耦，回滚成本仅限插件与服务壳 |

## 10. 非目标

- 不 fork / vendor DSH 源码，不改其内核包。
- 不在 `dsh-shell` 重新实现 agent loop、会话持久化、审批、沙箱与 UI。
- 不以 `deepseek-harness-sdk`（Python 子进程 SDK）作为主集成路径；该 SDK 仅作为后续自动化入口的备选。

## 11. 待验证事项

- DSH `skill` 包的数据模型与 YouMi `SkillStore` 字段（`bound_tool_name`、SOP 内容格式）的兼容性——M1 前验证。
- DSH telemetry 能否覆盖 YouMi 现有审计需求；不能覆盖的部分落 `youmi_experience_add`。
- Windows 下 stdio MCP 子进程的回收与重连行为（`reconnect.maxAttempts` 默认 10）。
