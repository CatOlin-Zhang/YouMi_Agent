# @youmi/dsh-bridge

把 YouMi 的四类能力接到 DeepSeek Harness 上的 Cordis 插件。

## 定位

DSH 已经是完整的 agent 宿主（agent loop、工具执行、审批、沙箱、会话、GUI），
因此 YouMi 在这里**只做能力库**，不重复实现宿主职责：

| 能力 | 解决的问题 | sidecar 工具 |
| --- | --- | --- |
| 工具目录 | 工具太多时按语义/tag/风险检索，而不是全量塞进上下文 | `youmi_tool_*` |
| Skill 索引 | 复用已有 SOP，而不是每次重新生成 | `youmi_skill_*` |
| 计划记忆 | 相似任务直接复用可行计划 | `youmi_plan_*` |
| 失败经验 | 踩过的坑沉淀为可检索条目 | `youmi_memory_*` |

真正的执行仍然走 DSH 原生工具；本插件只负责把「什么时候该调用能力库」
写进 system prompt。

## 组成

```
dsh-plugins/youmi-bridge/
├── package.json        # @youmi/dsh-bridge
├── tsconfig.json
├── cordis.patch.yml    # 可直接复制的挂载片段
├── README.md
└── src/index.ts        # 只注册一个 system prompt section
```

MCP 传输由 DSH 自带的 `@deepseek-ai/dsh-mcp-client` 负责，
本插件不建立连接、不注册工具。

## 前置条件

1. Python 3.10+，并且在 YouMi 仓库里可以执行：

   ```bash
   python -m youmi.mcp.stdio_server
   ```

   该进程只使用 stdin/stdout 通信，异常写 stderr。

2. DSH 侧已安装 `@deepseek-ai/dsh-mcp-client`（DSH 默认插件集自带）。

## 挂载

### 方式一：用仓库脚本（推荐）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-dsh.ps1
```

脚本会装好 `.dsh-runtime`、编译本插件、按本机绝对路径生成
`cordis.local.patch.yml`，然后启动 web profile。首次约 3 分钟，之后是秒级。

### 方式二：手工挂载

把 `cordis.patch.yml` 合并进 profile（或写到 `$DSH_HOME/cordis.patch.yml` 自动生效），
替换其中 `<path-to-YouMi_Agent>`，并把 `name` 改成**编译产物文件**：

```yaml
    - id: youmi-bridge
      name: 'C:\path\to\YouMi_Agent\dsh-plugins\youmi-bridge\lib\index.js'
      config:
        toolPrefix: 'mcp__youmi__'
```

## 三个必须知道的加载约束

实测启动时会依次撞上这三条，都不是 DSH 的 bug，而是 ESM 与依赖解析的固有行为。

**1. `name` 不能写目录——ESM 不读 `package.json` 的 `main`**

写目录会报 `ERR_UNSUPPORTED_DIR_IMPORT`，必须指到 `lib/index.js` 文件。
（绝对路径会被 loader 转成 `file://` URL，中文路径也能正确处理。）

**2. 插件要能解析到 `@deepseek-ai/schemastery`，但不能自带 `cordis`**

本插件 `src/index.ts` 里 `cordis` 与 `dsh-system-prompt` 都是 `import type`，
编译后只剩 `import z from '@deepseek-ai/schemastery'`（运行时唯一真实依赖）。

- 自带一份 `cordis` → 出现**双 cordis 实例**，插件的 `inject` 不会被同一容器解析，静默失效
- 什么都不带 → `ERR_MODULE_NOT_FOUND`

因此本机做法是在 `node_modules/@deepseek-ai/` 下建 junction 指向 DSH 运行时那一份：

```powershell
New-Item -ItemType Junction -Path dsh-plugins\youmi-bridge\node_modules\@deepseek-ai\schemastery `
  -Target .dsh-runtime\node_modules\@deepseek-ai\schemastery
```

这与 DSH 自己 `pnpm` 的 `nodeLinker: hoisted` + `autoInstallPeers: false` 是同一个思路：
**peer 不落地，共享宿主那一份实例**。

**3. 编译期的 `@deepseek-ai/*` 类型解析**

仓库里的 `tsconfig.json` 不写死本机路径；构建时用 `tsconfig.local.json` 覆盖：

```json
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "baseUrl": ".",
    "paths": { "@deepseek-ai/*": ["../../.dsh-runtime/node_modules/@deepseek-ai/*"] },
    "typeRoots": ["../../.dsh-runtime/node_modules/@types"]
  },
  "include": ["src"]
}
```

启动脚本会自动生成这份 override（已在 `.gitignore` 中）。

## 工具清单

DSH 看到的工具名带 `mcp__youmi__` 前缀，例如
`mcp__youmi__youmi_tool_search`。

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `youmi_tool_search` | `query`、`top_k=5`、`risk_ceiling=""` | 语义 + tag + 风险等级检索工具 |
| `youmi_tool_get` | `name`、`version=""` | 读取工具定义（含参数与版本） |
| `youmi_tool_resolve` | `name`、`call_path="direct"` | 解析应调用的版本（skill→root / direct→head） |
| `youmi_skill_index` | `query`、`top_k=5` | 检索 Skill（SOP） |
| `youmi_skill_list` | `cursor=""`、`limit=20` | 分页列出 Skill |
| `youmi_skill_get` | `skill_id` | 读取 Skill 全文 |
| `youmi_plan_search` | `task`、`top_k=3` | 检索相似任务的可行计划 |
| `youmi_plan_save` | `task`、`plan_json`、`outcome="success"` | 存回计划（`WorkflowPlan` JSON） |
| `youmi_memory_search` | `query`、`top_k=5`、`tool_name=""` | 检索失败经验 |
| `youmi_memory_add` | `task`、`failure`、`fix`、`tags=""`、`tool_name=""` | 记录失败与修复方式 |

所有工具失败时返回 `{"error": "..."}` 而非抛异常，模型可忽略并继续。

## 环境变量

sidecar 通过环境变量配置，不读取任何配置文件：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `YOUMI_DATA_DIR` | `.youmi` | 数据根目录 |
| `YOUMI_TOOLS_DB` | `<DATA_DIR>/tools.db` | 工具目录库 |
| `YOUMI_SKILLS_DB` | `<DATA_DIR>/skills.db` | Skill 库 |
| `YOUMI_PLANS_DB` | `<DATA_DIR>/plans.db` | 计划库 |
| `YOUMI_MEMORY_DB` | `<DATA_DIR>/knowledge.db` | 经验库 |
| `YOUMI_EMBEDDING_BASE_URL` | 空 | OpenAI 兼容 `/v1/embeddings` 地址；空 = 关键词降级 |
| `YOUMI_EMBEDDING_API_KEY` | 空 | 本地模型可留空 |
| `YOUMI_EMBEDDING_MODEL` | `nomic-embed-text` | 向量模型名 |
| `YOUMI_EMBEDDING_DIM` | 自动探测 | 固定维度时填写，避免首次请求 |
| `YOUMI_LOG_LEVEL` | `INFO` | 日志写 stderr，不污染协议流 |

> DSH 的 mcp-client 会清理环境变量再合并 `env`，因此上面这些值必须显式写在
> `config.env` 里；依赖 shell 继承不可靠。

## 边界

以下内容已由 DSH 原生提供，YouMi 不再实现：

- agent loop、子代理、工具执行与超时
- 审批、沙箱、权限
- 会话持久化、上下文压缩、GUI
- MCP 客户端连接与工具命名

YouMi 只保留「检索 + 沉淀」这条链路，因此删除了原仓库的
`agents/`、`bus/`、`gateway/`、`security/`、`observability/`、`scheduler/`、
`gui/`、`eval/`、`experiments/` 等宿主代码。
