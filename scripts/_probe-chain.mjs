#!/usr/bin/env node
// 临时探针：验证 DSH 是否跟随 tools/list_changed 动态更新工具表。
// 用法：node scripts/_probe-chain.mjs "<任务>" [patch...]
import fs from 'node:fs'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const DSH_BIN = path.join(ROOT, '.dsh-runtime', 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')
const CRED = path.join(ROOT, 'scripts', 'ab-credentials.patch.yml')
// 默认挂「按需激活」形态；MOCK_PATCH 可指向 eager 形态做对照
const MOCK = process.env.MOCK_PATCH
  ? path.resolve(ROOT, process.env.MOCK_PATCH)
  : path.join(ROOT, '.ab', 'mock-tools.patch.yml')

// 两组使用同一任务文本，避免 prompt 差异污染对比
const DEFAULT_TASK = '发起退款：订单 A123，金额 199，原因：重复支付。请先找到合适的工具再执行。'
const prompt = process.env.PROMPT || process.argv[2] || DEFAULT_TASK
const extra = process.argv.slice(3)

const startedAt = Date.now()
const child = spawn(
  process.execPath,
  [DSH_BIN, '--profile', 'headless', '--patch', CRED, '--patch', MOCK, ...extra, '--json', prompt],
  { cwd: ROOT, env: { ...process.env, NODE_NO_WARNINGS: '1' }, windowsHide: true },
)

let buffer = ''
let stderr = ''
const seenTools = new Map()
const totals = { input: 0, output: 0, cacheRead: 0 }

child.stdout.setEncoding('utf8')
child.stdout.on('data', chunk => {
  buffer += chunk
  let index
  while ((index = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, index)
    buffer = buffer.slice(index + 1)
    if (!line.trim()) continue
    let event
    try {
      event = JSON.parse(line)
    } catch {
      continue
    }
    const elapsed = Date.now() - startedAt
    if (event.type === 'status' && event.phase === 'step_start') {
      console.log(`+${elapsed}ms  [step ${event.step}] 开始`)
    } else if (event.type === 'status' && event.phase === 'step_end' && event.usage) {
      const u = event.usage
      totals.input += u.inputTokens || 0
      totals.output += u.outputTokens || 0
      totals.cacheRead += u.cacheReadTokens || 0
      console.log(`+${elapsed}ms  [step ${event.step}] 结束 in=${u.inputTokens} out=${u.outputTokens} cacheRead=${u.cacheReadTokens ?? 0}`)
    } else if (event.type === 'tool_call') {
      seenTools.set(event.tool, (seenTools.get(event.tool) || 0) + 1)
      console.log(`+${elapsed}ms  [call] ${event.tool}  ${JSON.stringify(event.input).slice(0, 200)}`)
    } else if (event.type === 'tool_result') {
      console.log(`+${elapsed}ms  [result] ${event.status}  ${String(event.result ?? '').slice(0, 300)}`)
    } else if (event.type === 'thinking' && event.text) {
      console.log(`+${elapsed}ms  [思考] ${String(event.text).slice(0, 200)}`)
    } else if (event.type === 'final') {
      console.log(`\n+${elapsed}ms  [final] ${String(event.text ?? '').slice(0, 600)}`)
    }
  }
})
child.stderr.setEncoding('utf8')
child.stderr.on('data', chunk => { stderr += chunk })

child.on('close', code => {
  console.log(`\nexit=${code}  总耗时=${((Date.now() - startedAt) / 1000).toFixed(1)}s`)
  console.log('工具调用统计:', JSON.stringify(Object.fromEntries(seenTools)))
  console.log(`累计 ${JSON.stringify(totals)}`)
  for (const key of ['tool list changed', 're-sync', 'refreshed', 'tools changed']) {
    if (stderr.includes(key)) console.log(`stderr 命中 "${key}"`)
  }
  const tail = stderr.split('\n').filter(line => /tool list|re-sync|register/i.test(line)).slice(-6)
  if (tail.length) console.log('stderr 相关行:\n' + tail.join('\n'))
})
