// A/B 任务集：每个任务自带 fixture（写入沙箱）与自动断言（零人工判定）。
//
// 设计约束：
// - 单文件体积小，保证 8 个任务能在同一轮实验里跑完
// - 断言只检查产出物，不检查过程，避免因为工具路径不同而误判
// - 任务覆盖：纯读取、多文件合并、调试、跨文件重命名、扫描统计、空格路径、脚手架、日志解析
export default [
  {
    id: 't1-count-todo',
    title: '统计 TODO 数量',
    prompt: '读取当前目录下的 notes.md，数出其中 TODO 出现的次数，把数字（只写数字）写入 todo-count.txt。',
    fixture: {
      'notes.md': [
        '# 项目笔记',
        '',
        '- TODO: 补齐单元测试',
        '- 已完成：初始化仓库',
        '- TODO: 更新 README',
        '- 讨论记录：先冻结接口',
        '- TODO: 清理临时脚本',
        '',
      ].join('\n'),
    },
    checks: [{ type: 'fileEquals', path: 'todo-count.txt', value: '3' }],
  },
  {
    id: 't2-merge-json',
    title: '合并两个 JSON 配置',
    prompt: '当前目录有 a.json 和 b.json。把两者合并成 merged.json：同名键以 b.json 为准，其余键保留。再把 merged.json 的顶层键数量写入 key-count.txt（只写数字）。',
    fixture: {
      'a.json': '{"host":"localhost","port":8080,"debug":true}',
      'b.json': '{"port":9090,"timeout":30}',
    },
    checks: [
      { type: 'jsonIncludes', path: 'merged.json', value: { host: 'localhost', port: 9090, debug: true, timeout: 30 } },
      { type: 'fileEquals', path: 'key-count.txt', value: '4' },
    ],
  },
  {
    id: 't3-fix-range',
    title: '修好失败的测试',
    prompt: '当前目录有一个 Node 项目，运行 node test.mjs 会失败。请定位并修复实现代码（不要修改测试文件），让测试通过。',
    fixture: {
      'range.mjs': [
        'export function range(n) {',
        '  const out = []',
        '  for (let i = 1; i <= n; i += 1) out.push(i)',
        '  return out',
        '}',
        '',
      ].join('\n'),
      'test.mjs': [
        "import assert from 'node:assert'",
        "import { range } from './range.mjs'",
        '',
        'assert.deepEqual(range(3), [0, 1, 2])',
        'assert.deepEqual(range(1), [0])',
        'assert.deepEqual(range(0), [])',
        "console.log('ok')",
        '',
      ].join('\n'),
    },
    checks: [{ type: 'commandExitZero', command: 'node', args: ['test.mjs'] }],
  },
  {
    id: 't4-rename-symbol',
    title: '跨文件重命名符号',
    prompt: '把项目里所有 calcTotal 改名为 computeTotal（包括定义与全部调用点），不要改动其他逻辑。',
    fixture: {
      'price.js': [
        'export function calcTotal(items) {',
        '  return items.reduce((sum, item) => sum + item.price * item.qty, 0)',
        '}',
        '',
      ].join('\n'),
      'cart.js': [
        "import { calcTotal } from './price.js'",
        '',
        'export function checkout(items) {',
        '  return { total: calcTotal(items) }',
        '}',
        '',
      ].join('\n'),
      'main.js': [
        "import { checkout } from './cart.js'",
        '',
        "console.log(checkout([{ price: 2, qty: 3 }]))",
        '',
      ].join('\n'),
    },
    checks: [
      { type: 'noFileContains', paths: ['price.js', 'cart.js', 'main.js'], value: 'calcTotal' },
      { type: 'fileContains', path: 'price.js', value: 'computeTotal' },
      { type: 'fileContains', path: 'cart.js', value: 'computeTotal' },
    ],
  },
  {
    id: 't5-todo-report',
    title: '生成 TODO 统计表',
    prompt: '扫描 src 目录下所有 .js 文件，统计每个文件里 TODO 注释的条数，生成 report.md：一个两列的 Markdown 表格（文件名、TODO 条数），按文件名升序排列。',
    fixture: {
      'src/alpha.js': '// TODO: 支持重试\n// TODO: 加日志\nexport const a = 1\n',
      'src/beta.js': 'export const b = 2\n',
      'src/gamma.js': '// TODO: 拆分函数\nexport const c = 3\n',
    },
    checks: [
      { type: 'fileMatches', path: 'report.md', pattern: 'alpha\\.js\\s*\\|?\\s*2' },
      { type: 'fileMatches', path: 'report.md', pattern: 'gamma\\.js\\s*\\|?\\s*1' },
    ],
  },
  {
    id: 't6-space-path',
    title: '处理带空格的文件名',
    prompt: '当前目录有一个文件名带空格的文件 "my report.txt"。请读取它，统计行数，并把 "行数: N" 写入 "my report.summary.txt"。',
    fixture: {
      'my report.txt': 'line one\nline two\nline three\n',
    },
    checks: [
      { type: 'fileExists', path: 'my report.summary.txt' },
      { type: 'fileContains', path: 'my report.summary.txt', value: '3' },
    ],
  },
  {
    id: 't7-scaffold',
    title: '按规范创建包结构',
    prompt: '创建 Python 包结构：pkg/__init__.py 中导出 add；pkg/math_ops.py 定义 add(a, b) 返回两数之和；tests/test_math_ops.py 用 unittest 断言 add(1, 2) == 3。不需要安装依赖。',
    fixture: {},
    checks: [
      { type: 'fileExists', path: 'pkg/__init__.py' },
      { type: 'fileContains', path: 'pkg/__init__.py', value: 'add' },
      { type: 'fileContains', path: 'pkg/math_ops.py', value: 'def add' },
      { type: 'fileExists', path: 'tests/test_math_ops.py' },
    ],
  },
  {
    id: 't8-parse-log',
    title: '解析访问日志',
    prompt: '当前目录有 access.log，每行一次请求。统计状态码 404 出现的次数，以及出现次数最多的请求路径（并列时取字典序最小的），写入 stats.json：{"not_found": 数字, "top_path": "路径"}。',
    fixture: {
      'access.log': [
        'GET /index.html 200',
        'GET /api/user 200',
        'GET /missing.png 404',
        'GET /api/user 200',
        'GET /missing.png 404',
        'GET /api/user 200',
        'GET /missing.png 404',
        'GET /api/user 200',
        '',
      ].join('\n'),
    },
    checks: [{ type: 'jsonIncludes', path: 'stats.json', value: { not_found: 3, top_path: '/api/user' } }],
  },
]
