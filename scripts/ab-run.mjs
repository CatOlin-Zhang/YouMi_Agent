#!/usr/bin/env node
// A/B 驱动：同一批任务分别跑「裸 DSH」与「DSH + YouMi」，自动采集指标并断言结果。
//
// 用法：
//   node scripts/ab-run.mjs --phase cold --repeat 3
//   node scripts/ab-run.mjs --phase warm --repeat 3
//   node scripts/ab-run.mjs --phase cold --repeat 1 --only t1-count-todo
//
// A 组 = 仅凭证 overlay；B 组 = 凭证 overlay + YouMi 挂载（mcp-client + bridge）。
// 两组唯一的差异就是 cordis.local.patch.yml 挂不挂，保证对照公平。
import fs from 'node:fs'
import path from 'node:path'
import { spawn, spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import tasks from './ab/tasks.mjs'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const DSH_BIN = path.join(ROOT, '.dsh-runtime', 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')
const CRED_PATCH = path.join(ROOT, 'scripts', 'ab-credentials.patch.yml')
const TEMPLATE_PATCH = path.join(ROOT, 'cordis.local.patch.yml')
const AB_DIR = path.join(ROOT, '.ab')
const RUN_TIMEOUT_MS = 15 * 60 * 1000

function arg(name, fallback) {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

/**
 * B 组用的挂载配置：把 sidecar 数据目录指到本阶段独立目录，
 * 避免冷启动阶段和热库阶段互相污染，也不碰开发用的 .youmi。
 */
function treatmentPatch(phase, dataDir) {
  const template = fs.readFileSync(TEMPLATE_PATCH, 'utf8')
  const patched = template.replace(/YOUMI_DATA_DIR:\s*'[^']*'/, `YOUMI_DATA_DIR: '${dataDir}'`)
  fs.mkdirSync(AB_DIR, { recursive: true })
  const file = path.join(AB_DIR, `treatment-${phase}.patch.yml`)
  fs.writeFileSync(file, patched)
  return file
}

function materialize(sandbox, fixture) {
  fs.rmSync(sandbox, { recursive: true, force: true })
  fs.mkdirSync(sandbox, { recursive: true })
  for (const [relative, content] of Object.entries(fixture)) {
    const target = path.join(sandbox, relative)
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.writeFileSync(target, content)
  }
}

/** 跑一次 headless，逐行解析 NDJSON 事件流，并记录到达时间用于算首 token 延迟。 */
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
  let steps = 0
  let reason = null
  let finalText = ''
  const calls = []
  const results = new Map()

  for (const event of events) {
    if (event.type === 'status') {
      if (event.phase === 'turn_start') turns += 1
      else if (event.phase === 'step_start') steps += 1
      else if (event.phase === 'step_end' && event.usage) {
        const usage = event.usage
        inputTokens += usage.inputTokens || 0
        outputTokens += usage.outputTokens || 0
        cacheReadTokens += usage.cacheReadTokens || 0
        cacheWriteTokens += usage.cacheWriteTokens || 0
        reportedTokens += usage.totalTokens || 0
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
  const youmiDetails = calls
    .filter(call => String(call.tool).startsWith('mcp__youmi__'))
    .map(call => {
      const outcome = results.get(call.callId)
      return {
        tool: String(call.tool).replace('mcp__youmi__', ''),
        input: call.input,
        status: outcome?.status ?? 'unknown',
        result: String(outcome?.result ?? '').slice(0, 400),
      }
    })

  const weightedTokens = Math.round(
    inputTokens * COST_WEIGHTS.input
    + cacheReadTokens * COST_WEIGHTS.cacheRead
    + outputTokens * COST_WEIGHTS.output,
  )

  return {
    inputTokens,
    outputTokens,
    cacheReadTokens,
    cacheWriteTokens,
    totalTokens: reportedTokens || inputTokens + outputTokens,
    weightedTokens,
    turns,
    steps,
    reason,
    finalText,
    toolCalls,
    toolErrors: calls.filter(call => results.get(call.callId)?.status === 'error').length,
    youmiCalls: youmiDetails.length,
    youmiDetails,
  }
}

function runCheck(check, sandbox) {
  // 模型常借 PowerShell 写文件，会带 UTF-8 BOM；BOM 属环境噪声，不应算作答案错误。
  const read = relative => fs.readFileSync(path.join(sandbox, relative), 'utf8').replace(/^\uFEFF/, '')
  const normalize = text => text.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n').trim()
  try {
    switch (check.type) {
      case 'fileExists':
        return { ok: fs.existsSync(path.join(sandbox, check.path)), detail: check.path }
      case 'fileContains':
        return { ok: read(check.path).includes(check.value), detail: `${check.path} ∋ ${check.value}` }
      case 'fileEquals':
        return { ok: normalize(read(check.path)) === normalize(check.value), detail: `${check.path} == ${check.value}` }
      case 'fileMatches':
        return { ok: new RegExp(check.pattern).test(read(check.path)), detail: `${check.path} ~ /${check.pattern}/` }
      case 'noFileContains':
        return {
          ok: check.paths.every(relative => !read(relative).includes(check.value)),
          detail: `none ∋ ${check.value}`,
        }
      case 'jsonIncludes': {
        const actual = JSON.parse(read(check.path))
        const ok = Object.entries(check.value).every(([key, expected]) =>
          JSON.stringify(actual?.[key]) === JSON.stringify(expected))
        return { ok, detail: `${check.path} ⊇ ${JSON.stringify(check.value)}` }
      }
      case 'commandExitZero': {
        const result = spawnSync(check.command, check.args ?? [], { cwd: sandbox, encoding: 'utf8' })
        return { ok: result.status === 0, detail: `${check.command} exit=${result.status}` }
      }
      default:
        return { ok: false, detail: `未知断言类型 ${check.type}` }
    }
  } catch (error) {
    return { ok: false, detail: `${check.type} 失败: ${error.message}` }
  }
}

function summarize(rows) {
  const byGroup = new Map()
  for (const row of rows) {
    if (!byGroup.has(row.group)) byGroup.set(row.group, [])
    byGroup.get(row.group).push(row)
  }
  const lines = []
  const header = [
    '组', '次数', '成功', '成功率', '输入tok', '缓存读', '输出tok', '总tok', '加权tok',
    '轮次', '步数', '耗时s', '首token', '工具', '工具错', 'youmi',
  ]
  lines.push(header.join('\t'))
  for (const [group, groupRows] of [...byGroup.entries()].sort()) {
    const n = groupRows.length
    const sum = key => groupRows.reduce((acc, row) => acc + (row[key] || 0), 0)
    const avg = key => (sum(key) / n).toFixed(1)
    const toolCallAvg = (groupRows.reduce((acc, row) => acc + (row.toolCalls?.length || 0), 0) / n).toFixed(1)
    const success = groupRows.filter(row => row.success).length
    lines.push([
      group,
      n,
      success,
      `${((success / n) * 100).toFixed(1)}%`,
      Math.round(sum('inputTokens') / n),
      Math.round(sum('cacheReadTokens') / n),
      Math.round(sum('outputTokens') / n),
      Math.round(sum('totalTokens') / n),
      Math.round(sum('weightedTokens') / n),
      avg('turns'),
      avg('steps'),
      (sum('wallMs') / n / 1000).toFixed(1),
      Math.round(sum('ttftMs') / n),
      toolCallAvg,
      sum('toolErrors'),
      sum('youmiCalls'),
    ].join('\t'))
  }
  return lines.join('\n')
}

/** 逐任务对比：看清收益/损失集中在哪些任务上，避免被均值掩盖。 */
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
      success: list.filter(row => row.success).length / n,
      tokens: list.reduce((acc, row) => acc + row.totalTokens, 0) / n,
      weighted: list.reduce((acc, row) => acc + row.weightedTokens, 0) / n,
      wall: list.reduce((acc, row) => acc + row.wallMs, 0) / n / 1000,
      youmi: list.reduce((acc, row) => acc + row.youmiCalls, 0),
    }
  }
  const delta = (base, next) => (base === 0 ? '—' : `${(((next - base) / base) * 100).toFixed(0)}%`)

  const lines = [[
    '任务', 'A成功', 'B成功', 'A tok', 'B tok', 'tok差',
    'A加权', 'B加权', '加权差', 'A耗时', 'B耗时', '耗时差', 'A youmi', 'B youmi',
  ].join('\t')]
  for (const [taskId, groups] of byTask) {
    const a = statOf(groups.A ?? [])
    const b = statOf(groups.B ?? [])
    lines.push([
      taskId,
      `${Math.round(a.success * 100)}%`,
      `${Math.round(b.success * 100)}%`,
      Math.round(a.tokens),
      Math.round(b.tokens),
      delta(a.tokens, b.tokens),
      Math.round(a.weighted),
      Math.round(b.weighted),
      delta(a.weighted, b.weighted),
      a.wall.toFixed(1),
      b.wall.toFixed(1),
      delta(a.wall, b.wall),
      a.youmi,
      b.youmi,
    ].join('\t'))
  }
  return lines.join('\n')
}

async function main() {
  // 供 ab-seed.py 读取任务原文，保证种子数据与实验任务单调一致（单一数据源）。
  const dumpTasks = arg('--dump-tasks', '')
  if (dumpTasks) {
    const payload = tasks.map(({ id, title, prompt, checks }) => ({ id, title, prompt, checks }))
    fs.writeFileSync(dumpTasks, JSON.stringify(payload, null, 2), 'utf8')
    console.log(`已导出 ${payload.length} 个任务 → ${dumpTasks}`)
    return
  }

  const phase = arg('--phase', 'cold')
  const repeat = Number(arg('--repeat', '3'))
  const only = arg('--only', '')
  const groups = arg('--groups', 'A,B').split(',').map(value => value.trim()).filter(Boolean)

  if (!fs.existsSync(DSH_BIN)) throw new Error(`找不到 DSH 运行时：${DSH_BIN}`)
  if (!fs.existsSync(TEMPLATE_PATCH)) throw new Error(`找不到挂载配置：${TEMPLATE_PATCH}`)

  const selected = tasks.filter(task => !only || task.id === only)
  const dataDir = path.join(AB_DIR, 'youmi-data', phase)
  fs.mkdirSync(dataDir, { recursive: true })
  const patches = { A: [CRED_PATCH], B: [CRED_PATCH, treatmentPatch(phase, dataDir)] }

  const resultsFile = path.join(AB_DIR, `results-${phase}.jsonl`)
  fs.mkdirSync(path.dirname(resultsFile), { recursive: true })

  console.log(`阶段=${phase} 重复=${repeat} 任务=${selected.length} 组=${groups.join(',')} 数据=${dataDir}`)
  const rows = []
  let index = 0
  const total = selected.length * repeat * groups.length

  // 顺序：任务 → 重复 → 组，让同一轮 A/B 相邻，减少环境漂移。
  for (const task of selected) {
    for (let rep = 1; rep <= repeat; rep += 1) {
      for (const group of groups) {
        index += 1
        const sandbox = path.join(AB_DIR, 'runs', phase, group, task.id, `r${rep}`, 'workspace')
        materialize(sandbox, task.fixture)
        process.stdout.write(`[${index}/${total}] ${task.id} ${group} r${rep} ... `)

        const run = await runDsh({ sandbox, patches: patches[group], prompt: task.prompt })
        const metrics = extractMetrics(run.events)
        const checks = task.checks.map(check => runCheck(check, sandbox))
        const checksOk = checks.every(item => item.ok)
        const success = run.code === 0 && metrics.reason === 'completed' && checksOk

        const row = {
          phase,
          taskId: task.id,
          group,
          rep,
          success,
          exitCode: run.code,
          reason: metrics.reason,
          checksOk,
          failedChecks: checks.filter(item => !item.ok).map(item => item.detail),
          wallMs: run.wallMs,
          ttftMs: run.ttftMs ?? run.wallMs,
          inputTokens: metrics.inputTokens,
          outputTokens: metrics.outputTokens,
          cacheReadTokens: metrics.cacheReadTokens,
          cacheWriteTokens: metrics.cacheWriteTokens,
          totalTokens: metrics.totalTokens,
          weightedTokens: metrics.weightedTokens,
          turns: metrics.turns,
          steps: metrics.steps,
          toolCalls: metrics.toolCalls,
          toolErrors: metrics.toolErrors,
          youmiCalls: metrics.youmiCalls,
          youmiDetails: metrics.youmiDetails,
          finalText: metrics.finalText.slice(0, 500),
          stderrTail: run.stderr.slice(-400),
        }
        rows.push(row)
        fs.appendFileSync(resultsFile, `${JSON.stringify(row)}\n`)
        console.log(`${success ? 'OK' : 'FAIL'} tok=${row.totalTokens} turns=${row.turns} ${(row.wallMs / 1000).toFixed(1)}s`)
      }
    }
  }

  console.log(`\n== 汇总（${phase}）==`)
  console.log(summarize(rows))
  console.log(`\n== 逐任务对比（${phase}）==`)
  console.log(compareByTask(rows))
  console.log(`\n明细：${resultsFile}`)
}

await main()
