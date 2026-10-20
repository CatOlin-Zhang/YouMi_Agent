# YouMi × DeepSeek Harness 迁移记录（dsh-shell 分支）

- 状态：**代码融合已落地**（sidecar + 插件骨架 + 测试全绿），待接入 DSH 实机跑基线
- 分支：`dsh-shell`（对应远程 `origin/dsh-shell`）；`master` 原形态冻结
- DSH 源码基线：`dsh/` 浅克隆，`0.2.1-rc.2`，commit `687f282`（2026-10-17），不入库
- 迁移清单来源：`新建 文本文档 (2).txt`（运行时 → 工具/技能 → 记忆/基础设施 → 不迁移）

## 1. 目标与原则

**目标形态**：DSH 是宿主（agent loop、工具执行、审批、沙箱、会话、GUI），YouMi 是
MCP 能力服务（工具目录检索、Skill 索引、Plan 复用、失败经验）。`dsh-shell` 分支
实质是**给 DSH 写插件 + 一个 Python sidecar**。

五条硬性原则：

1. **只迁移 DSH 缺的能力**：工具目录检索、Skill 索引、Plan 复用、失败经验。
2. **Python 只做 sidecar**：不让 Python 重新承担 agent loop / 编排 / GUI。
3. **fail-open**：sidecar 挂掉不阻断 DSH 主链路，工具返回结构化错误。
4. **每阶段 A/B**：以 DSH 基线为对照，证明收益后才进入下一阶段。
5. **不建第二套**：不保留第二套 loop、orchestrator、消息协议、审批队列、审计存储。

## 2. 分支与代码策略

| 项 | 规则 |
|---|---|
| `master` | 原形态冻结，只接受 bug fix 与安全修复 |
| `dsh-shell` | DSH 插件形态，承载全部迁移工作 |
| `dsh/` | DSH 官方源码浅克隆，已加入 `.gitignore`，**不入库**；仅供本地查源码、跑 `pnpm`、对类型 |
| 插件代码 | `dsh-plugins/<plugin-name>/`，独立 npm 包，ESM，`peerDependencies` 指向 `@deepseek-ai/cordis` 与所需 dsh 包 |

更新 DSH 源码（走本机代理）：

```powershell
git -C dsh -c http.proxy=http://127.0.0.1:7897 pull --ff-only
```

## 3. 当前架构

```text
┌──────────────── DSH 宿主（Node 22+，TS，Cordis 全插件）────────────────┐
│  dsh web · dsh-agent-loop · dsh-session · dsh-tools · dsh-subagent     │
│  dsh-user-approval · dsh-sandbox · dsh-compaction · dsh-mcp-client     │
│                                                                        │
│  @youmi/dsh-bridge（本仓库 dsh-plugins/youmi-bridge）                   │
│    只注册一段 system prompt：何时调用 youmi_* 工具                      │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ stdio MCP（serverName=youmi）
                                ▼
┌──────────────── YouMi sidecar（Python，逻辑与数据都在这里）──────────────┐
│  youmi_tool_search / get / resolve   （ToolStore · ConeRetriever）      │
│  youmi_skill_index / list / get      （SkillStore）                    │
│  youmi_plan_search / save            （PlanMemory · WorkflowPlan）     │
│  youmi_memory_search / add           （GlobalMemory · ToolExperience）  │
│  EmbeddingClient · 版本路由 · SQLite + sqlite-vec                       │
└────────────────────────────────────────────────────────────────────────┘
```

**为什么只有一个插件**：MCP 连接由官方 `dsh-mcp-client` 负责，再写
`tool-catalog` / `skills` / `memory` / `plan` 四个 TS 插件只会重复 sidecar 已有的
检索逻辑，并在 DSH 侧再造一层分发。因此融合后只保留一个 prompt 注入插件，
能力全部收敛到 10 个 MCP 工具。

## 4. 分期计划与落地状态

| 阶段 | 计划交付物 | 实际状态 |
|---|---|---|
| **P0** | sidecar 骨架 + bridge 插件 + 基线 | ✅ sidecar 与插件骨架已完成；DSH 实机基线待跑 |
| **P1** | `search_tools` + 工具目录索引 | ✅ `youmi_tool_search/get/resolve` 已实现 |
| **P2** | skill provider | ✅ 以 `youmi_skill_*` 三个工具提供，未接 `ctx.skills.registerProvider` |
| **P3** | 失败经验 + 可选 breaker | 🟡 `youmi_memory_*` 已实现；breaker 未做（DSH 已有 retry policy） |
| **P4** | plan cache + `run_plan` 工具 | 🟡 `youmi_plan_search/save` 已实现；`run_plan` 未做（DSH 有 subagent + plan-mode） |
| **P5** | HOT/WARM/COLD 渐进暴露 | ⏸ 未做（`tools.restrict` 会破坏 prefix cache，需 A/B 后再定） |
| **P6** | 可选 UI / 个性化 | ⏸ 不做（DSH GUI 已覆盖） |

## 5. Sidecar 能力契约（已实现）

- 传输：stdio，newline-delimited JSON-RPC 2.0，协议版本 `2024-11-05`
- 入口：`python -m youmi.mcp.stdio_server`，或安装后的 `youmi-sidecar`
- 模型侧工具名：`mcp__youmi__<rawName>`（`serverName=youmi` 固定契约，只增不改）
- fail-open：任一存储初始化失败只降级对应工具，进程不退出；stdout 只写协议，日志走 stderr

| MCP 工具（rawName） | 入参 | 出参 | 数据源 |
|---|---|---|---|
| `youmi_tool_search` | `query, top_k=5, risk_ceiling=""` | `[{name, score, summary, risk_level, definition}]` | `ToolStore.search_cone` |
| `youmi_tool_get` | `name, version=""` | `{name, version, definition, ...}` | `ToolStore.get_tool` |
| `youmi_tool_resolve` | `name, call_path="direct"` | `{name, version, ...}` | `CallPathRouter` |
| `youmi_skill_index` | `query, top_k=5` | `[{skill_id, skill_name, score, summary, tags}]` | `SkillStore.search_cone` |
| `youmi_skill_list` | `cursor="", limit=20` | `{items, next_cursor, has_more}` | `SkillStore.list_skills` |
| `youmi_skill_get` | `skill_id` | `{skill_name, content, bound_tool_name, ...}` | `SkillStore.get_skill` |
| `youmi_plan_search` | `task, top_k=3` | `[{plan_name, similarity, steps}]` | `PlanMemory.search_plan` |
| `youmi_plan_save` | `task, plan_json, outcome="success"` | `{plan_id}` | `WorkPlan.validate()` + `PlanMemory.save_plan` |
| `youmi_memory_search` | `query, top_k=5, tool_name=""` | `[{entry_id, tool_name, content, ...}]` | `GlobalMemory.search` |
| `youmi_memory_add` | `task, failure, fix, tags="", tool_name=""` | `{entry_id, ...}` | `GlobalMemory.add_experience` |

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `YOUMI_DATA_DIR` | `.youmi` | 数据根目录 |
| `YOUMI_TOOLS_DB` / `YOUMI_SKILLS_DB` / `YOUMI_PLANS_DB` / `YOUMI_MEMORY_DB` | 各自独立 | 覆盖单库路径 |
| `YOUMI_EMBEDDING_BASE_URL` | 空 | 空 = 关闭向量检索，关键词降级 |
| `YOUMI_EMBEDDING_API_KEY` / `_MODEL` / `_DIM` | 空 / `nomic-embed-text` / 自动 | Embedding 配置 |
| `YOUMI_LOG_LEVEL` | `INFO` | stderr 日志级别 |

## 6. DSH 插件（已落地）

| 插件 | 扩展点 | 职责 |
|---|---|---|
| `@youmi/dsh-bridge`（`dsh-plugins/youmi-bridge`） | `ctx.systemPrompt.section()` | 注册 `youmi:capabilities` 提示段（order 3050），说明 10 个工具的使用时机与 fail-open 语义 |

约束：

- 注册走 `ctx.effect()` / `ctx.systemPrompt.section()`，卸载即回收
- 不新增 session 事件类型、不改 `agent-loop`
- 部署参数全部走 `cordis.yml` 的 plugin `config`，代码不硬编码路径

`cordis.patch.yml` 示例见 `dsh-plugins/youmi-bridge/cordis.patch.yml`。

## 7. 已删除清单（映射到 DSH 原生）

| YouMi 原实现 | 处置 | DSH 替代 |
|---|---|---|
| `core/agent.py` ReAct、状态机、`ToolRegistry`、`ToolExecutionMixin` | 已删除 | `dsh-agent-loop` / `dsh-tools` |
| `HookRegistry` / `PluginManager` / `PromptAssembler` | 已删除 | Cordis 事件监听 + `ctx.systemPrompt` |
| `mcp/client.py`、`bridge.py`、`context.py`、`confirm.py` | 已删除 | `dsh-mcp-client` |
| `ApprovalManager`、`audit_gate.py` | 已删除 | `dsh-user-approval` / `dsh-permission-presets` |
| `security/`（sandbox + auth） | 已删除 | `dsh-sandbox*`、`dsh-authorization` |
| `memory/`（Full/Summary/LSTM、Compactor） | 已删除 | `dsh-session`、`dsh-compaction-basic` |
| `bus/`、`coordinator/handoff.py`、`subprocess_agent.py` | 已删除 | `ctx.subagents`、`dsh-subagent-*` |
| `scheduler/` | 已删除 | `dsh-schedule` / `dsh-tool-schedule` |
| `gateway/`、`gui/` | 已删除 | `dsh web` / headless / ACP |
| `tools/` 9 个内置工具 | 已删除 | `dsh-tool-fs/bash/pwsh/web`（sandbox-aware） |
| `observability/`、`core/resilience.py` | 已删除 | DSH telemetry / retry policy |
| `eval/`、`experiments/`、`MockLLMServer` | 已删除 | 迁移到 DSH 的 eval / benchmark；需要时从 `master` 取回 |
| `knowledge/feedback.py`、`coordinator/post_task.py` | 已删除 | 经验闭环改由 `youmi_memory_add` 显式调用 |

保留并重构：

| 原位置 | 现位置 | 说明 |
|---|---|---|
| `coordinator/plan.py` 的 `WorkflowPlan` / `WorkflowStep` / `StepStatus` | `youmi/plan/models.py` | 去掉 `StepResult` / `WorkflowExecutor` |
| `coordinator/plan_memory.py` | `youmi/plan/plan_memory.py` | `git mv` 保留历史，仅改导入 |
| `coordinator/post_task.py` 的 `ToolExperience` | `youmi/knowledge/models.py` | 纯数据模型 |
| `llm/client.py` 的 `is_local_base_url` | `youmi/llm/embeddings.py` | 内联，消除对已删模块的依赖 |

## 8. 本轮验证

```bash
# 导入与契约
python -c "import youmi; print(youmi.__version__)"        # 0.2.0

# MCP 握手
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python -m youmi.mcp.stdio_server
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'     | python -m youmi.mcp.stdio_server

# 测试
pytest -q                                                  # 233 passed
```

顺带修复：`ToolDefinition.from_function` 在 `from __future__ import annotations`
下把所有参数都判成 `string`，导致 MCP schema 丢失 `integer` / `boolean` 类型；
改为先用 `typing.get_type_hints` 解析注解，失败再回退。回归用例在
`tests/test_capabilities.py`。

## 9. 已核实的 DSH API（本地源码）

| 结论 | 证据 |
|---|---|
| `ctx.subagents.startActivation(spec)` 存在 | `dsh/packages/subagent/subagent/src/index.ts:255` |
| `ctx.skills.registerProvider(create)` 存在 | `dsh/packages/skill/skill/src/index.ts:390` |
| `ctx.tools.restrict({allow,deny})` 存在，但**必须用 agent 作用域** | `dsh/packages/core/tools/src/index.ts:1105-1130` |
| `ctx.systemPrompt.section({name, order, text})` 存在，order 必须有限 | `dsh/packages/core/system-prompt/src/index.ts:478` |
| `agent.inject(message: UserMessage)` 只接受 UserMessage | `dsh/packages/core/agent/src/runtime-types.ts:241` |
| 官方 MCP client 支持 stdio，工具名为 `mcp__<serverName>__<raw>` | `dsh/packages/mcp/mcp-client/README.md` |

## 10. 风险与回滚

| 风险 | 影响 | 对策 |
|---|---|---|
| DSH `0.2.1-rc.2` 为 developer preview | 插件/配置失配 | 锁定版本 + SHA；只用第 9 节核实过的扩展点 |
| 双栈运维（Node + Python） | 排障复杂 | 单 sidecar；`dsh-mcp-client` 自动重连；插件 fail-open |
| 工具名契约被破坏 | 会话历史/权限规则失效 | `serverName=youmi` 固定，工具名只增不改 |
| `dsh-shell` 方向失败 | 投入浪费 | `master` 完整保留；sidecar 与插件解耦，可整体回退 |

## 11. 待验证

- DSH 实机挂载 sidecar 后的基线：成功率、总轮次、首工具准确率、p95 延迟
- Windows 下 stdio 子进程回收与 `reconnect.maxAttempts`（默认 10）表现
- 是否把 `SkillStore` 直接接成 `ctx.skills.registerProvider`，替代 `youmi_skill_*` 工具
- `youmi_plan_save` 的 `plan_json` 是否要兼容 DSH `dsh-plan` 的计划结构
