/**
 * YouMi bridge —— 把 Python sidecar 的四类能力告诉模型。
 *
 * 传输层不在本插件内：MCP 连接由 DSH 自带的 `@deepseek-ai/dsh-mcp-client`
 * 以 stdio 子进程方式建立，工具名形如 `mcp__youmi__youmi_tool_search`。
 * 本插件只注册一段 system prompt，说明何时调用这些工具、以及任务结束后
 * 如何把计划与失败经验沉淀回 sidecar。
 *
 * fail-open：本插件不校验 sidecar 是否在线；检索失败由 sidecar 返回结构化
 * 错误，模型继续用原生工具完成任务即可。
 *
 * @module @youmi/dsh-bridge
 */

import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
// 副作用类型导入：把 ctx.systemPrompt 声明合并进 Context。
import type {} from '@deepseek-ai/dsh-system-prompt'

/** Cordis 插件名（loader 诊断用） */
export const name = 'youmi-bridge'

/** 本插件依赖系统提示服务 */
export const inject = ['systemPrompt']

/** 提示段位置：工具说明之后、MCP 服务器清单之前 */
const SECTION_ORDER = 3050

/** 提示段名称；同名 section 会被上层 preset 覆盖，便于定制 */
const SECTION_NAME = 'youmi:capabilities'

/** 插件配置 */
export interface Config {
  /** mcp-client 生成的工具名前缀，默认 `mcp__youmi__` */
  toolPrefix?: string
  /** 是否注入能力使用指引，默认 true */
  includeWorkflowGuidance?: boolean
  /** 追加在默认指引之后的自定义文本 */
  extraGuidance?: string
}

/** Schemastery 校验（缺省值在这里补齐） */
export const Config: z<Config> = z.object({
  toolPrefix: z.string().default('mcp__youmi__'),
  includeWorkflowGuidance: z.boolean().default(true),
  extraGuidance: z.string().default(''),
})

/**
 * 渲染能力清单。
 * @param prefix - mcp-client 注册工具时使用的前缀。
 * @param extra - 部署侧追加的指引。
 * @returns 直接拼接进 system prompt 的文本。
 */
export function renderGuidance(prefix: string, extra = ''): string {
  const tool = (toolName: string): string => `\`${prefix}${toolName}\``
  return [
    '## YouMi 能力库',
    '',
    '本地有一个可复用的能力库（工具目录 / Skill 索引 / 计划记忆 / 失败经验）。',
    '它不是执行器：真正干活仍然用 DSH 原生工具，能力库只负责检索与沉淀。',
    '',
    '使用规则：',
    `- 不确定该用哪个工具时，先调用 ${tool('youmi_tool_search')}，query 用当前任务的自然语言描述。`,
    `- 需要工具的具体用法或版本时，调用 ${tool('youmi_tool_get')}；外部工具用 ${tool('youmi_tool_resolve')} 解析调用路径。`,
    `- 需要复用已有 SOP 时，用 ${tool('youmi_skill_index')} 检索、${tool('youmi_skill_get')} 读取全文，分页用 ${tool('youmi_skill_list')}。`,
    `- 接到与以往相似的任务时，先调用 ${tool('youmi_plan_search')} 复用可行计划，不要从零规划。`,
    `- 工具调用失败并修复后，用 ${tool('youmi_memory_add')} 记录「任务 / 失败现象 / 修复方式」，避免重复踩坑。`,
    `- 不确定历史经验时，用 ${tool('youmi_memory_search')} 检索。`,
    `- 非平凡的多步任务成功完成后，用 ${tool('youmi_plan_save')} 把计划存回能力库。`,
    '',
    '检索结果为空属于正常情况：直接继续用原生工具完成任务，不要因为检索无结果而阻塞或反复重试。',
    extra.trim(),
  ].filter(line => line.length > 0).join('\n')
}

/**
 * 注册提示段。Cordis 在插件卸载时自动回收该 section。
 * @param ctx - 插件上下文，需已注入 systemPrompt。
 * @param config - 已由 Schemastery 补齐缺省值的配置。
 */
export function apply(ctx: Context, config: Config): void {
  if (config.includeWorkflowGuidance === false) return
  const prefix = config.toolPrefix ?? 'mcp__youmi__'
  ctx.systemPrompt.section({
    name: SECTION_NAME,
    order: SECTION_ORDER,
    // 静态文本，不做 {{variable}} 插值
    interpolate: false,
    text: renderGuidance(prefix, config.extraGuidance ?? ''),
  })
}
