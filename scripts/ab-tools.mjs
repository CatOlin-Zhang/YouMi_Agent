#!/usr/bin/env node
// A/B 驱动：验证「海量工具全量注册」与「按需激活」在上下文成本上的差异。
//
// 用法：
//   node scripts/ab-tools.mjs                      # 8 任务 × 2 组 × 3 轮 = 48 次
//   node scripts/ab-tools.mjs --repeat 1 --only t1-refund
//   node scripts/ab-tools.mjs --catalog-size 400
//
// A 组 = mock server --eager，初始化即把全部工具摊进 tools/list。
// B 组 = mock server 默认（lazy），只暴露 2 个元工具，命中后经 list_changed 注入。
// 两组共用同一份工具目录、同一批 prompt，唯一差异就是暴露策略。
import fs from 'node:fs'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import tasks from './ab/tool-tasks.mjs'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const DSH_BIN = path.join(ROOT, '.dsh-runtime', 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')
const CRED_PATCH = path.join(ROOT, 'scripts', 'ab-credentials.patch.yml')
const MOCK_SERVER = path.join(ROOT, 'scripts', 'ab', 'mock_tool_server.py')
const PYTHON = process.env.PYTHON || 'D:\\ANACONDA\\envs\\AI\\python.exe'
const AB_DIR = path.join(ROOT, '.ab')
const RUN_TIMEOUT_MS = 10 * 60 * 1000

function arg(name, fallback) {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

/**
 * 生成两组挂载配置。patch 由驱动写而不是手工维护，
 * 保证工具目录规模、python 解释器路径和任务集始终同源。
 */
function mockPatch(group, catalogSize) {
  const eager = group === 'A'
  const file = path.join(AB_DIR, eager ? 'mock-tools-eager.patch.yml' : 'mock-tools.patch.yml')
  const args = [MOCK_SERVER, '--catalog-size', String(catalogSize)]
  if (eager) args.push('--eager')
  const body = [
    '- insert:',
    '    - id: mcp-mock-tools',
    "      name: '@deepseek-ai/dsh-mcp-client'",
    '      config:',
    '        transport: stdio',
    '        serverName: tools',
    `        command: '${PYTHON}'`,
    '        args:',
    ...args.map(value => `          - '${value}'`),
    `        cwd: '${ROOT}'`,
    '        toolCallTimeoutMs: 30000',
    '        failOnStartupError: false',
    '',
  ].join('\n')
  fs.mkdirSync(AB_DIR, { recursive: true })
  fs.writeFileSync(file, body, 'utf8')
  return file
}

/** 跑一次 headless，逐行解析 NDJSON 事件流。 */
function runDsh({ sandbox, patches, prompt }) {
  return new Promise(resolve => {
    const startedAt = Date.now()
    const args = ['--profile', 'headless']
    for (const patch of patches) args.push('--patch', patch)
    args.push('--json', prompt)

    const child = spawn(process.execPath, [DSH_BIN, ...args], {
      cwd: sandbox,
      env: { ...process.env, NODE_NO_WARNINGS: '1' },
      windowsHide: true,
    })

    const events = []
    let buffer = ''
    let stderr = ''
    let ttftMs = null
    let settled = false

    const finish = code => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve({ code, events, stderr, wallMs: Date.now() - startedAt, ttftMs })
    }

    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      finish(-1)
    }, RUN_TIMEOUT_MS)

    child.stdout.setEncoding('utf8')
    child.stdout.on('data', chunk => {
      buffer += chunk
      let index
      while ((index = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, index)
        buffer = buffer.slice(index + 1)
        if (!line.trim()) continue
        try {
          const event = JSON.parse(line)
          event.atMs = Date.now() - startedAt
          events.push(event)
          if (ttftMs === null && (event.type === 'text' || event.type === 'thinking')) ttftMs = event.atMs
        } catch {
          // 非 JSON 行（启动期告警）忽略
        }
      }
    })
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', chunk => { stderr += chunk })
    child.on('close', finish)
    child.on('error', () => finish(-1))
  })
}

// 缓存读按 1/4 价、输出按 4 倍价折算（DeepSeek 量级），得到可直接相加比较的「等价输入 token」。
// 只用于跨组对比相对成本，不代表账单金额。
const COST_WEIGHTS = { input: 1, cacheRead: 0.25, output: 4 }

function extractMetrics(events) {
  let inputTokens = 0
  let outputTokens = 0
  let cacheReadTokens = 0
  let cacheWriteTokens = 0
  let reportedTokens = 0
  let turns = 0
  let reason = null
  let finalText = ''
  const stepUsages = []
  const calls = []
  const results = new Map()

  for (const event of events) {
    if (event.type === 'status') {
      if (event.phase === 'turn_start') turns += 1
      else if (event.phase === 'step_end' && event.usage) {
        const usage = event.usage
        inputTokens += usage.inputTokens || 0
        outputTokens += usage.outputTokens || 0
        cacheReadTokens += usage.cacheReadTokens || 0
        cacheWriteTokens += usage.cacheWriteTokens || 0
        reportedTokens += usage.totalTokens || 0
        stepUsages.push({
          input: usage.inputTokens || 0,
          cacheRead: usage.cacheReadTokens || 0,
          output: usage.outputTokens || 0,
        })
      } else if (event.phase === 'turn_end') reason = event.reason?.kind ?? null
    } else if (event.type === 'tool_call') {
      // 投影事件里的工具名字段是 tool；早期版本按 name 取值导致全部为 null。
      calls.push({ callId: event.callId, tool: event.tool, input: event.input })
    } else if (event.type === 'tool_result') {
      results.set(event.callId, { status: event.status, result: event.result })
    } else if (event.type === 'final') {
      finalText = event.text ?? ''
    }
  }

  const toolCalls = calls.map(call => call.tool)
  const weightedTokens = Math.round(
    inputTokens * COST_WEIGHTS.input
    + cacheReadTokens * COST_WEIGHTS.cacheRead
    + outputTokens * COST_WEIGHTS.output,
  )
  // 未缓存输入占比越高，说明前缀复用越差。B 组每次 list_changed 都会让前缀失效，
  // 这个指标就是「动态注入的隐性税」。
  const cacheHitRate = inputTokens + cacheReadTokens === 0
    ? 0
    : cacheReadTokens / (inputTokens + cacheReadTokens)

  return {
    inputTokens,
    outputTokens,
    cacheReadTokens,
    cacheWriteTokens,
    totalTokens: reportedTokens || inputTokens + outputTokens,
    weightedTokens,
    cacheHitRate,
    stepUsages,
    turns,
    steps: stepUsages.length,
    reason,
    finalText,
    toolCalls,
    toolErrors: calls.filter(call => results.get(call.callId)?.status === 'error').length,
  }
}

/** 每次 list_changed 都会让客户端重新拉全量工具列表，前缀随之失效。
 * mock server 在推送时打点，DSH 自身的 info 日志不进 stderr，所以只能数这份。
 */
function countToolListChanges(stderr) {
  const matches = stderr.match(/tools\/list_changed/g)
  return matches ? matches.length : 0
}

function summarize(rows) {
  const byGroup = new Map()
  for (const row of rows) {
    if (!byGroup.has(row.group)) byGroup.set(row.group, [])
    byGroup.get(row.group).push(row)
  }
  const lines = [[
    '组', '次数', '命中', '命中率', '输入tok', '缓存读', '缓存率', '输出tok', '总tok', '加权tok',
    '步数', '耗时s', '首token', '检索', 'listChanged', '工具错',
  ].join('\t')]
  for (const [group, groupRows] of [...byGroup.entries()].sort()) {
    const n = groupRows.length
    const sum = key => groupRows.reduce((acc, row) => acc + (row[key] || 0), 0)
    const avg = key => (sum(key) / n).toFixed(1)
    const hit = groupRows.filter(row => row.success).length
    lines.push([
      group,
      n,
      hit,
      `${((hit / n) * 100).toFixed(1)}%`,
      Math.round(sum('inputTokens') / n),
      Math.round(sum('cacheReadTokens') / n),
      `${((sum('cacheReadTokens') / Math.max(sum('inputTokens') + sum('cacheReadTokens'), 1)) * 100).toFixed(1)}%`,
      Math.round(sum('outputTokens') / n),
      Math.round(sum('totalTokens') / n),
      Math.round(sum('weightedTokens') / n),
      avg('steps'),
      (sum('wallMs') / n / 1000).toFixed(1),
      Math.round(sum('ttftMs') / n),
      avg('searchCalls'),
      avg('listChanges'),
      sum('toolErrors'),
    ].join('\t'))
  }
  return lines.join('\n')
}

/** 逐任务对比：均值会掩盖差异，必须看到收益和损失分别落在哪些任务上。 */
function compareByTask(rows) {
  const byTask = new Map()
  for (const row of rows) {
    if (!byTask.has(row.taskId)) byTask.set(row.taskId, { A: [], B: [] })
    const bucket = byTask.get(row.taskId)
    if (bucket[row.group]) bucket[row.group].push(row)
  }

  const statOf = list => {
    const n = list.length || 1
    return {
      hit: list.filter(row => row.success).length / n,
      input: list.reduce((acc, row) => acc + row.inputTokens, 0) / n,
      cacheRead: list.reduce((acc, row) => acc + row.cacheReadTokens, 0) / n,
      weighted: list.reduce((acc, row) => acc + row.weightedTokens, 0) / n,
      steps: list.reduce((acc, row) => acc + row.steps, 0) / n,
      wall: list.reduce((acc, row) => acc + row.wallMs, 0) / n / 1000,
      search: list.reduce((acc, row) => acc + row.searchCalls, 0) / n,
    }
  }
  const delta = (base, next) => (base === 0 ? '—' : `${(((next - base) / base) * 100).toFixed(0)}%`)

  const lines = [[
    '任务', 'A命中', 'B命中', 'A输入', 'B输入', '输差',
    'A缓存', 'B缓存', 'A加权', 'B加权', '加权差', 'A步', 'B步', 'A耗时', 'B耗时', 'B检索',
  ].join('\t')]
  for (const [taskId, groups] of byTask) {
    const a = statOf(groups.A ?? [])
    const b = statOf(groups.B ?? [])
    lines.push([
      taskId,
      `${Math.round(a.hit * 100)}%`,
      `${Math.round(b.hit * 100)}%`,
      Math.round(a.input),
      Math.round(b.input),
      delta(a.input, b.input),
      Math.round(a.cacheRead),
      Math.round(b.cacheRead),
      Math.round(a.weighted),
      Math.round(b.weighted),
      delta(a.weighted, b.weighted),
      a.steps.toFixed(1),
      b.steps.toFixed(1),
      a.wall.toFixed(1),
      b.wall.toFixed(1),
      b.search.toFixed(1),
    ].join('\t'))
  }
  return lines.join('\n')
}

/**
 * 缓存落差：把每一步的「未缓存输入」按步序铺开。
 * A 组前缀固定，第 2 步起应几乎全额命中；B 组注入新工具后前缀变了，
 * 落差就直接体现在这里。
 */
function cacheProfile(rows) {
  const byGroup = new Map()
  for (const row of rows) {
    if (!byGroup.has(row.group)) byGroup.set(row.group, [])
    byGroup.get(row.group).push(row.stepUsages)
  }
  const lines = [['组', '步1输入', '步1缓存', '步2输入', '步2缓存', '步3输入', '步3缓存', '步4输入', '步4缓存'].join('\t')]
  for (const [group, profiles] of [...byGroup.entries()].sort()) {
    const cells = []
    for (let step = 0; step < 4; step += 1) {
      const inputs = profiles.map(usages => usages[step]?.input ?? 0)
      const caches = profiles.map(usages => usages[step]?.cacheRead ?? 0)
      const mean = list => Math.round(list.reduce((acc, value) => acc + value, 0) / Math.max(list.length, 1))
      cells.push(mean(inputs), mean(caches))
    }
    lines.push([group, ...cells].join('\t'))
  }
  return lines.join('\n')
}

async function main() {
  const repeat = Number(arg('--repeat', '3'))
  const only = arg('--only', '')
  const groups = arg('--groups', 'A,B').split(',').map(value => value.trim()).filter(Boolean)
  const catalogSize = Number(arg('--catalog-size', '120'))

  if (!fs.existsSync(DSH_BIN)) throw new Error(`找不到 DSH 运行时：${DSH_BIN}`)
  if (!fs.existsSync(CRED_PATCH)) throw new Error(`找不到凭证 overlay：${CRED_PATCH}`)
  if (!fs.existsSync(MOCK_SERVER)) throw new Error(`找不到 mock server：${MOCK_SERVER}`)

  const selected = tasks.filter(task => !only || task.id === only)
  const patches = {}
  for (const group of groups) patches[group] = [CRED_PATCH, mockPatch(group, catalogSize)]

  const resultsFile = path.join(AB_DIR, 'results-tools.jsonl')
  fs.mkdirSync(AB_DIR, { recursive: true })
  fs.writeFileSync(resultsFile, '', 'utf8')

  console.log(
    `工具量=${catalogSize} 重复=${repeat} 任务=${selected.length} 组=${groups.join(',')}`
    + ` 总运行=${selected.length * repeat * groups.length}`,
  )

  const rows = []
  let index = 0
  const total = selected.length * repeat * groups.length

  // 顺序：任务 → 轮次 → 组，让同一轮 A/B 相邻，减少服务端缓存与网络漂移。
  for (const task of selected) {
    for (let rep = 1; rep <= repeat; rep += 1) {
      for (const group of groups) {
        index += 1
        const sandbox = path.join(AB_DIR, 'runs', 'tools', group, task.id, `r${rep}`, 'workspace')
        fs.rmSync(sandbox, { recursive: true, force: true })
        fs.mkdirSync(sandbox, { recursive: true })
        process.stdout.write(`[${index}/${total}] ${task.id} ${group} r${rep} ... `)

        const run = await runDsh({ sandbox, patches: patches[group], prompt: task.prompt })
        const metrics = extractMetrics(run.events)

        // 只看实际调用了哪个工具，不看模型怎么描述。
        const businessCalls = metrics.toolCalls
          .map(name => String(name).replace('mcp__tools__', ''))
          .filter(name => !name.startsWith('youmi_tool_'))
        const searchCalls = metrics.toolCalls
          .filter(name => String(name).includes('youmi_tool_')).length
        const hitExpect = businessCalls.includes(task.expect)
        const wrongPicks = businessCalls.filter(name => task.distractors.includes(name))
        const success = run.code === 0 && metrics.reason === 'completed' && hitExpect

        const row = {
          catalogSize,
          taskId: task.id,
          expect: task.expect,
          group,
          rep,
          success,
          hitExpect,
          businessCalls,
          wrongPicks,
          searchCalls,
          listChanges: countToolListChanges(run.stderr),
          exitCode: run.code,
          reason: metrics.reason,
          wallMs: run.wallMs,
          ttftMs: run.ttftMs ?? run.wallMs,
          inputTokens: metrics.inputTokens,
          outputTokens: metrics.outputTokens,
          cacheReadTokens: metrics.cacheReadTokens,
          cacheWriteTokens: metrics.cacheWriteTokens,
          totalTokens: metrics.totalTokens,
          weightedTokens: metrics.weightedTokens,
          cacheHitRate: metrics.cacheHitRate,
          steps: metrics.steps,
          stepUsages: metrics.stepUsages,
          turns: metrics.turns,
          toolErrors: metrics.toolErrors,
          toolCalls: metrics.toolCalls,
          stderrTail: run.stderr.slice(-800),
          finalText: metrics.finalText.slice(0, 300),
        }
        rows.push(row)
        fs.appendFileSync(resultsFile, `${JSON.stringify(row)}\n`)

        const pick = wrongPicks.length ? ` 错选=${wrongPicks.join(',')}` : ''
        console.log(
          `${success ? 'OK' : 'FAIL'} in=${row.inputTokens} cache=${row.cacheReadTokens}`
          + ` out=${row.outputTokens} 加权=${row.weightedTokens} ${(row.wallMs / 1000).toFixed(1)}s${pick}`,
        )
      }
    }
  }

  console.log('\n== 汇总 ==')
  console.log(summarize(rows))
  console.log('\n== 逐任务对比 ==')
  console.log(compareByTask(rows))
  console.log('\n== 缓存剖面（按步序均值）==')
  console.log(cacheProfile(rows))
  console.log(`\n明细：${resultsFile}`)
}

await main()
