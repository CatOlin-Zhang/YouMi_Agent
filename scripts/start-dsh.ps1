<#
.SYNOPSIS
    本机启动 DSH，并把 YouMi 能力库作为 MCP 服务挂上去。

.DESCRIPTION
    首次运行会自动完成三件事：
      1. 把 @deepseek-ai/dsh 装到 .dsh-runtime（不入库）
      2. 用该运行时里的 tsc 编译 dsh-plugins/youmi-bridge
      3. 按本机绝对路径生成 cordis.local.patch.yml（不入库）
    之后重复运行只做启动。

.PARAMETER Port
    web 服务端口，默认 8080。

.PARAMETER NoOpen
    不自动打开浏览器。

.PARAMETER Force
    强制重新安装运行时、重新编译插件、重新生成 patch 文件。

.PARAMETER HeadlessTask
    给出任务文本时以 headless 模式跑一轮（不启动 UI），适合跑基线。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start-dsh.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start-dsh.ps1 -HeadlessTask "读取 README 并总结"
#>
param(
    [int]$Port = 8080,
    [switch]$NoOpen,
    [switch]$Force,
    [string]$HeadlessTask = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root '.dsh-runtime'
$pluginDir = Join-Path $root 'dsh-plugins\youmi-bridge'
$dshBin = Join-Path $runtime 'node_modules\@deepseek-ai\dsh\lib\bin.js'
$tscBin = Join-Path $runtime 'node_modules\typescript\bin\tsc'
$patchFile = Join-Path $root 'cordis.local.patch.yml'
$python = 'D:\ANACONDA\envs\AI\python.exe'

function Write-Step([string]$text) { Write-Host "[dsh] $text" -ForegroundColor Cyan }

# --- 1. 运行时 ---
if ($Force -or -not (Test-Path $dshBin)) {
    Write-Step "安装 DSH 运行时到 .dsh-runtime（首次约 3 分钟）"
    & npm.cmd install '@deepseek-ai/dsh@0.2.1-rc.2' 'typescript@5.9.3' '@types/node@22.10.5' `
        --prefix $runtime --no-fund --no-audit
    if ($LASTEXITCODE -ne 0) { throw "npm install 失败（exit=$LASTEXITCODE）" }
}

# --- 2. 本机路径 patch 文件 ---
if ($Force -or -not (Test-Path $patchFile)) {
    Write-Step "生成 $patchFile"
    $yaml = @"
# 本机专用挂载配置 —— 不入库（.gitignore 已忽略）
# 由 scripts/start-dsh.ps1 生成；模板见 dsh-plugins/youmi-bridge/cordis.patch.yml
- insert:
    - id: mcp-youmi
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: stdio
        serverName: youmi
        command: '$python'
        args:
          - '-m'
          - 'youmi.mcp.stdio_server'
        cwd: '$root'
        env:
          YOUMI_DATA_DIR: '$root\.youmi'
          YOUMI_EMBEDDING_BASE_URL: ''
          YOUMI_EMBEDDING_MODEL: 'nomic-embed-text'
          YOUMI_LOG_LEVEL: 'INFO'
        toolCallTimeoutMs: 30000
        failOnStartupError: false

    - id: youmi-bridge
      # 必须指向编译产物文件：Node ESM 不支持目录 import（不读 package.json main）
      name: '$pluginDir\lib\index.js'
      config:
        toolPrefix: 'mcp__youmi__'
        includeWorkflowGuidance: true
        extraGuidance: ''
"@
    Set-Content -Path $patchFile -Value $yaml -Encoding UTF8
}

# --- 3. 编译插件 ---
# 仓库里的 tsconfig.json 不写死本机路径；构建期用 override 把 @deepseek-ai/* 指向运行时那一份，
# 这样插件目录不需要自己的 node_modules，也就不会出现第二份 cordis。
$tsconfigLocal = Join-Path $pluginDir 'tsconfig.local.json'
if (-not (Test-Path $tsconfigLocal)) {
    $override = @"
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "baseUrl": ".",
    "paths": { "@deepseek-ai/*": ["../../.dsh-runtime/node_modules/@deepseek-ai/*"] },
    "typeRoots": ["../../.dsh-runtime/node_modules/@types"]
  },
  "include": ["src"]
}
"@
    Set-Content -Path $tsconfigLocal -Value $override -Encoding UTF8
}

$pluginLib = Join-Path $pluginDir 'lib\index.js'
if ($Force -or -not (Test-Path $pluginLib)) {
    Write-Step "编译 youmi-bridge"
    & node $tscBin -p $tsconfigLocal
    if ($LASTEXITCODE -ne 0) { throw "插件编译失败（exit=$LASTEXITCODE）" }
}

# --- 4. 插件依赖 junction（复用运行时那一份，避免 cordis 出现双实例） ---
$linkRoot = Join-Path $pluginDir 'node_modules\@deepseek-ai'
New-Item -ItemType Directory -Force -Path $linkRoot | Out-Null
foreach ($name in @('cordis', 'schemastery')) {
    $target = Join-Path $runtime "node_modules\@deepseek-ai\$name"
    $link = Join-Path $linkRoot $name
    if ((Test-Path $target) -and -not (Test-Path $link)) {
        New-Item -ItemType Junction -Path $link -Target $target | Out-Null
    }
}

# --- 5. 启动 ---
$env:NODE_NO_WARNINGS = '1'
if ($HeadlessTask -ne '') {
    Write-Step "headless: $HeadlessTask"
    & node $dshBin --profile headless --patch $patchFile $HeadlessTask
} else {
    $appArgs = @('--profile', 'web', '--patch', $patchFile, '--port', $Port)
    if ($NoOpen) { $appArgs += '--no-open' }
    Write-Step "启动 DSH web：http://127.0.0.1:$Port"
    & node $dshBin @appArgs
}
