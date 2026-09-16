<#
    Windows 一键启动脚本（PowerShell）。

    .\run.ps1 demo                 # 跑全部演示场景
    .\run.ps1 demo -Scenario hitl  # 跑单个场景
    .\run.ps1 check                # 环境变量自查
    .\run.ps1 check -Ping          # 自查 + 连通性探测
    .\run.ps1 info                 # 打印运行环境概况

    会自动确认 Python 版本（LangChain 1.x 要求 >= 3.10），再调用 main.py。
    若提示禁止运行脚本，先执行一次：
        Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("demo", "check", "mcp", "info")]
    [string]$Command = "demo",

    [string]$Scenario,
    [string]$Provider,
    [switch]$Ping,

    # 解释器：默认优先项目内 .venv，其次 conda 环境 langchain
    [string]$Python = "auto"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# ---------- 1. 挑选解释器 ----------
# 优先级：项目内 .venv  >  conda 环境 langchain  >  系统 python
$py = $null
if ($Python -eq "auto") {
    $venvPy = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        $py = $venvPy
        Write-Host "[env] 使用项目虚拟环境 .venv（推荐，PyCharm 默认就是这个）" -ForegroundColor Cyan
    }
}
    $condaPy = Join-Path $env:USERPROFILE "anaconda3\envs\langchain\python.exe"
    if (Test-Path $condaPy) {
        $py = $condaPy
        Write-Host "[env] 使用 conda 环境 langchain" -ForegroundColor Cyan
    }
    else {
        Write-Host "[warn] 未找到 conda 环境 langchain，退回系统 python" -ForegroundColor Yellow
    }
}
elseif ($Python -like "conda:*") {
if (-not $py) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { Write-Host "[error] 找不到 python，请先安装并加入 PATH" -ForegroundColor Red; exit 2 }
}

# ---------- 2. 版本门槛：LangChain 1.x 需要 >= 3.10 ----------
$ver = & $py -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$verNum = [version]"$ver.0"
if ($verNum -lt [version]"3.10.0") {
    Write-Host "[error] 当前 Python $ver，LangChain 1.x 要求 >= 3.10" -ForegroundColor Red
    Write-Host "        建议：conda create -n langchain python=3.12" -ForegroundColor Yellow
    exit 2
}
Write-Host "[env] Python $ver  ->  $py" -ForegroundColor Cyan

# ---------- 3. 组装参数并转发给 main.py ----------
$argList = @("main.py", $Command)
if ($Scenario) { $argList += @("--scenario", $Scenario) }
if ($Provider) { $argList += @("--provider", $Provider) }
if ($Ping) { $argList += "--ping" }

Write-Host "[run] python $($argList -join ' ')" -ForegroundColor Cyan
Write-Host ("-" * 72)
& $py @argList
exit $LASTEXITCODE
