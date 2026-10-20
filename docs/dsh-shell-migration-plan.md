# YouMi Agent × DeepSeek Harness 迁移计划（dsh-shell 分支）

- 状态：已按 DSH 内 agent 产出的迁移清单重构，P0 待启动
- 分支：`dsh-shell`（对应远程 `origin/dsh-shell`）；`master` 原形态冻结
- DSH 源码基线：`dsh/` 浅克隆，`0.2.1-rc.2`，commit `687f282`（2026-10-17），不入库
- 迁移清单来源：`新建 文本文档 (2).txt`（运行时 → 工具/技能 → 记忆/基础设施 → 不迁移）

## 1. 目标与原则

**目标形态**：把 YouMi 的核心特征（工具目录检索、Skill 索引、Plan 复用、失败经验）迁入 DSH，`dsh-shell` 分支实质是**给 DSH 写插件**；YouMi 其余部分一律用 DSH 原生能力替代。

五条硬性原则：

1. **只迁移 DSH 缺的能力**：工具目录检索、Skill 索引、Plan 复用、失败经验。其余全部丢弃/映射。
2. **Python 只做 sidecar**：逻辑留在 Python，Node 只做薄适配；不让 Python 重新承担 agent loop / 编排 / GUI。
3. **fail-open**：sidecar 挂掉或超时不得阻断 DSH 主链路；工具集检索失败降级为全量工具。
4. **每阶段 A/B**：以 P0 建立的 DSH 基线为对照，证明优化真实有效后才进入下一阶段。
5. **不建第二套**：不保留第二套 loop、orchestrator、消息协议、审批队列与审计存储。

## 2. 分支与代码策略

| 项 | 规则 |
|---|---|
| `master` | 原形态冻结，只接受 bug fix 与安全修复 |
| `dsh-shell` | DSH 插件形态，承载全部迁移工作 |
| `dsh/` | DSH 官方源码浅克隆，已加入 `.gitignore`，**不入库**；仅供本地查源码、跑 `pnpm`、对类型 |
| 插件代码 | 放 `dsh-plugins/<plugin-name>/`，独立 npm 包，ESM，`peerDependencies` 指向 `@deepseek-ai/cordis` 与所需 dsh 包 |

更新 DSH 源码（走本机代理）：

```powershell
git -C dsh -c http.proxy=http://127.0.0.1:7897 pull --ff-only
# 或重克隆：git -c http.proxy=http://127.0.0.1:7897 clone --depth 1 https://github.com/deepseek-ai/deepseek-harness.git dsh
```

若后续需要协作者复现同一份源码，将该目录改为 git submodule（仓库只记录指针，不膨胀）。

## 3. 目标架构

```text
┌──────────────────── DSH 宿主（Node 22+，TS，Cordis 全插件）────────────────────┐
│  dsh web（127.0.0.1:3080）                                                       │
│  dsh-agent-loop · dsh-session · dsh-tools · dsh-subagent · dsh-user-approval     │
│  dsh-sandbox · dsh-compaction · dsh-skill · dsh-plan-mode · dsh-mcp-client       │
│                                                                                  │
│  dsh-youmi-*（本仓库插件，TS 薄适配）                                             │
│    bridge  tool-catalog  skills  memory  plan  breaker                           │
└───────────────────────────────┬──────────────────────────────────────────────────┘
                                │ stdio MCP（serverName=youmi，新行分隔 JSON-RPC）
                                ▼
┌──────────────── YouMi sidecar（Python，逻辑与数据都在这里）─────────────────────┐
│  tool.search / tool.get / tool.resolve      （ToolStore · ConeRetriever · Vault） │
│  skill.index / skill.list / skill.get       （SkillStore · skill_ingest）         │
│  plan.search / plan.save                    （PlanMemory）                        │
│  memory.search / memory.add                 （GlobalMemory · ExperienceExtractor）│
│  sidecar 内部：EmbeddingClient、版本路由、租户过滤、SQLite + sqlite-vec            │
└──────────────────────────────────────────────────────────────────────────────────┘
```

## 4. 分期计划（P0 → P6）

| 阶段 | 交付物 | 覆盖能力（清单编号） | 验收 |
|---|---|---|---|
| **P0** | Python sidecar 骨架 + `dsh-youmi-bridge` + 基线评测 | A17/A18 接口预留、B1、C17 | sidecar 可用率 100%，挂掉 fail-open；DSH 基线指标齐全（成功率为 0 的对照组不存在） |
| **P1** | `search_tools` + 工具目录索引 + `dsh-youmi-tool-catalog` | B4–B6、B11、B13、C10、C23 | 目录覆盖 100%，p95 < 100ms，不改变原有行为 |
| **P2** | `dsh-youmi-skills`（provider + search） | B14–B16 | skill 可列/可载，选择准确率对比基线 |
| **P3** | `dsh-youmi-memory`（失败经验 guard + feedback）+ 可选 `dsh-youmi-breaker` | A10、A18、A19、C6–C8、C15 | held-out 重复失败率下降，误 deny < 1% |
| **P4** | `dsh-youmi-plan`（plan cache + `run_plan` 工具）+ agent presets | A11–A16、C24 | 端到端成功率不降；计划命中精度可解释 |
| **P5** | HOT/WARM/COLD 渐进暴露（`agent.ctx.tools.restrict`） | B7、B8 | 首工具准确率不降、总轮次不增；PTC 模式下默认关闭 |
| **P6** | 可选 UI / 个性化（LSTM 记忆层等） | C3、C20 | 不影响主链路；默认关闭 |

执行要求：阶段顺序固定 P0→P6，**不按清单表格从上到下**；每阶段结束用 DSH 基线跑 A/B，未证明收益不进入下一阶段。

## 5. Sidecar 能力契约（serverName = `youmi`）

- 传输：stdio，newline-delimited JSON-RPC 2.0；入口 `python -m youmi.mcp.stdio_server`（新增 `[project.scripts] youmi-mcp`）。
- 环境变量：`YOUMI_DB_PATH`、`YOUMI_TENANT_ID`、`YOUMI_LOG_LEVEL`。
- 模型侧工具名固定为 `mcp__youmi__<rawName>`；`serverName` 与工具名是稳定契约，只增不改。
- **fail-open**：sidecar 未启动/超时/异常时，`dsh-youmi-bridge` 必须放行原始行为（工具目录检索失败 = 不 restrict；plan/memory 检索失败 = 不注入）。

| MCP 工具（rawName） | 入参 | 出参 | 数据源 |
|---|---|---|---|
| `youmi_tool_search` | `query, top_k=5, risk_ceiling=""` | `[{name, score, summary, definition}]` | `ToolStore.search_cone` / `ConeRetriever` |
| `youmi_tool_get` | `name` | `{name, definition, schema}` | `ToolVault` / `ToolStore` |
| `youmi_tool_resolve` | `name, call_path` | `{resolved_name, version}` | `version_router`（仅动态工具） |
| `youmi_skill_index` | `query, top_k=5` | `[{skill_id, name, score, summary}]` | `SkillStore.search_cone` |
| `youmi_skill_list` | `cursor` | `{items, next_cursor}` | `SkillStore` |
| `youmi_skill_get` | `skill_id` | `{name, content(SOP), bound_tool}` | `SkillStore` |
| `youmi_plan_search` | `task, top_k=3, min_score=0.85` | `[{plan_id, similarity, skeleton, steps}]` | `PlanMemory.search_plan` |
| `youmi_plan_save` | `task, plan_json, outcome` | `{plan_id}` | `PlanMemory.save_plan` |
| `youmi_memory_search` | `query, top_k=5` | `[{id, text, score, tags}]` | `GlobalMemory.search` |
| `youmi_memory_add` | `task, failure, fix, tags` | `{id}` | `ExperienceExtractor` + `GlobalMemory.batch_add` |

## 6. DSH 插件清单

| 插件 | 阶段 | 扩展点 | 职责 |
|---|---|---|---|
| `dsh-youmi-bridge` | P0 | `ctx.systemPrompt.section()`、sidecar 连接管理 | sidecar 生命周期、健康检查、fail-open 开关、配置与日志 |
| `dsh-youmi-tool-catalog` | P1/P5 | `agent.ctx.tools.restrict()`、`search_tools` 工具 | 工具目录检索与渐进暴露；只注册检索/加载工具，不复制工具定义 |
| `dsh-youmi-skills` | P2 | `ctx.skills.registerProvider()` | 把 YouMi SkillStore 暴露为 DSH skill provider（只负责 list/get） |
| `dsh-youmi-memory` | P3 | `tools/result`、`turn/end`、`agent/pre-step` | 失败证据收集、经验入库、经验注入；Guardian 子 Agent 只修 MCP/动态工具 |
| `dsh-youmi-plan` | P4 | `agent/pre-step`、`turn/end`、`run_plan` 工具 | 计划检索/注入/归档；`run_plan` 用 `ctx.subagents.startActivation` + 拓扑排序，必须补 artifacts 数据流 |
| `dsh-youmi-breaker` | P3（可选） | `agent/request-error`、`tools/execute` | 按失败率熔断；与 `dsh-llm-retry` 并存时避免双重重试 |

约束：

- 所有注册走 `ctx.effect()` / `ctx.on()`，卸载即回收；waterfall 监听必须 `next()`。
- 不新增 session 事件类型、不改 `agent-loop`；需要持久化的状态复用 DSH `plan`/`session` 机制。
- 插件配置使用 `cordis.yml` 的 plugin `config` 字段，不在代码里硬编码部署参数。

## 7. 不迁移清单（映射到 DSH 原生）

| YouMi 现状 | 处置 | DSH 替代 |
|---|---|---|
| `core/agent.py` ReAct、状态机、`ToolRegistry`、`ToolExecutionMixin` | 丢弃 | `dsh-agent-loop` / `dsh-tools` |
| `HookRegistry` / `PluginManager` | 映射 | Cordis 事件监听 + scope |
| `PromptAssembler` | 映射 | `ctx.systemPrompt.section()` |
| `MCPIntegrationMixin` / `mcp/client.py` | 丢弃 | `dsh-mcp-client` |
| `ApprovalManager`、`audit_gate` | 丢弃 | `dsh-user-approval` / `dsh-permission-presets` |
| `security/sandbox.py`、`security/auth.py` | 丢弃 | `dsh-sandbox*`、`dsh-authorization` |
| `MemoryManager`、Full/Summary、`ContextCompactor` | 丢弃 | `dsh-session`、`dsh-compaction-basic` |
| `bus/`、`HandoffProtocol`、`SubProcessAgentRunner` | 丢弃 | `ctx.subagents`、`dsh-subagent-*` |
| `scheduler/` | 丢弃 | `dsh-schedule` / `dsh-tool-schedule` |
| `gateway/`、`gui/` | 丢弃 | `dsh web` / headless / ACP |
| 多租户传播 | 丢弃 | sidecar 内保留租户过滤，DSH 无 tenant 概念 |
| `tools/builtin.py` 9 个内置工具 | 丢弃 | `dsh-tool-fs/bash/pwsh/web`（sandbox-aware） |
| `eval/`、`experiments/`、`MockLLMServer` | 保留 | 不进运行时，作为 P0 基线与论文实验 |

## 8. 已核实的 DSH API（本地源码）

| 结论 | 证据 |
|---|---|
| `ctx.subagents.startActivation(spec)` 存在 | `dsh/packages/subagent/subagent/src/index.ts:255` |
| `ctx.skills.registerProvider(create)` 存在 | `dsh/packages/skill/skill/src/index.ts:390` |
| `ctx.tools.restrict({allow,deny})` 存在，但**必须用 agent 作用域**（`agent.ctx`），上下文全局调用会抛错；空过滤器视为 bug | `dsh/packages/core/tools/src/index.ts:1105-1130` |
| `agent.inject(message: UserMessage)` 只接受 UserMessage | `dsh/packages/core/agent/src/runtime-types.ts:241` |
| 官方 MCP client 支持 stdio，工具名为 `mcp__<serverName>__<raw>`，可配 `reconnect` / `toolCallTimeoutMs` | `dsh/packages/mcp/mcp-client/README.md` |

## 9. 风险与回滚

| 风险 | 影响 | 对策 |
|---|---|---|
| DSH `0.2.1-rc.2` 为 developer preview，存在破坏性变更 | 插件/配置失配 | 锁定版本 + SHA；只用第 8 节核实过的扩展点；升级前跑 P0 基线 |
| 双栈运维（Node + Python） | 排障复杂 | 单 sidecar；`dsh-mcp-client` 自动重连；插件内 fail-open |
| `tools.restrict` 破坏 prefix cache | P5 收益被抵消 | P5 默认关闭，仅 PTC 外启用；A/B 验证总轮次 |
| `agent.inject` 只收 UserMessage | 注入形态受限 | 经验/计划以 UserMessage 注入；控制预算（默认 2000 字符） |
| 工具名契约被破坏 | 会话历史/权限规则失效 | `serverName=youmi` 固定，工具名只增不改 |
| `dsh-shell` 方向失败 | 投入浪费 | `master` 完整保留；sidecar 与插件解耦，可整体回退 |

## 10. 待验证

- DSH skill provider 的 `SkillProvider` 返回结构与本仓库 `SkillStore` 字段（`bound_tool_name`、SOP 格式）的映射细节——P2 前完成。
- `dsh-llm-retry` 与 `dsh-youmi-breaker` 的职责边界，避免双重重试——P3 前完成。
- Windows 下 stdio MCP 子进程回收与 `reconnect.maxAttempts`（默认 10）的实际表现——P0 验证。
- P0 基线任务集与指标（成功率、总轮次、首工具准确率、p95 延迟）需固化，供 P1–P5 A/B 使用。
