<#
.SYNOPSIS
    启动 OO 服务（game_server + web）—— **仓库里唯一的启动入口**。

.DESCRIPTION
    ★ 为什么只有这一个脚本（2026-11-08）：
      以前还有 start_game.bat / start_web.bat，各自写死盘符和 python 路径。
      换盘（F: -> D:\DiskF）之后那两个立刻失效，而本脚本还好 ——
      于是「启动逻辑」有了三份平行实现、两份是坏的。
      现在合并成一个：起哪个由 -Only 决定，前台还是后台由 -ShowWindow 决定。
      那两个 .bat 已删除。要新加启动方式之前，先想清楚为什么本脚本做不到。

    启动前先检查端口。为什么必须检查：
      2026-11-03 实测 - 旧版 _listen() 用 SO_REUSEADDR，在 Windows 上允许
      两个进程绑同一端口且都不报错，两个实例都打印 listening，连接被随机
      分配、两份存档互相覆盖。表现为「剧情过了但面板不开放」。
    现在 _listen() 已改成 SO_EXCLUSIVEADDRUSE（第二个实例直接启动失败），
    这里再加一道前置检查，让它在启动阶段就说清楚。

.PARAMETER Ports
    启动前要检查的端口，默认 33100 33101 33102 8399。

.PARAMETER Python
    指定 python.exe。默认依次尝试：环境变量 OO_PYTHON、仓库同级 Python\bin、PATH。

.PARAMETER NoWait
    不等待启动完成自检。

.PARAMETER Only
    只启动一部分服务：all（默认）/ game / web。
    替代已删除的 start_game.bat、start_web.bat。

.PARAMETER ShowWindow
    服务窗口显示在前台（默认最小化）。看实时输出时用。

.EXAMPLE
    .\start_all.ps1
    起全部服务，窗口最小化，等端口就绪。

.EXAMPLE
    .\start_all.ps1 -Only game -ShowWindow
    只起游戏服，窗口放前台（替代旧的 start_game.bat）。

.EXAMPLE
    .\start_all.ps1 -Only web
    只起 web（替代旧的 start_web.bat）。
#>
[CmdletBinding()]
param(
    [int[]]$Ports = @(),
    [string]$Python = "",
    [switch]$NoWait,
    # 只起一部分：game = 33100/33101/33102，web = 8399，all = 全部（默认）
    [ValidateSet('all', 'game', 'web')]
    [string]$Only = 'all',
    # 服务窗口显示在前台还是最小化。需要看实时输出时用 -ShowWindow
    [switch]$ShowWindow
)

$ErrorActionPreference = 'Stop'

# 按 -Only 推导要检查/等待的端口。显式传了 -Ports 就以传入的为准。
$PN = @{
    game = @(33100, 33101, 33102)
    web  = @(8399)
}
if ($Ports.Count -eq 0) {
    $Ports = switch ($Only) {
        'game'  { $PN.game }
        'web'   { $PN.web }
        default { $PN.game + $PN.web }
    }
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Head([string]$Text) {
    Write-Host ""
    Write-Host ("===== " + $Text + " =====") -ForegroundColor Cyan
    Write-Host ""
}

function Get-PortOwner([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    } catch { }
    return $null
}

function Test-PythonWorks([string]$Path) {
    # 光看文件存在不够 —— 实测踩过：D:\...\Python\bin\python.exe 是个 shim，
    # 它的安装路径还写着旧盘符 F:，运行时报
    #   "[ERROR] Failed to launch F:...: the install path was not found (0x0003)"。
    # 所以必须真的跑一次 --version 看退出码。
    if (-not $Path -or -not (Test-Path $Path)) { return $false }
    try {
        $null = & $Path --version 2>&1 | Out-String
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-Python([string]$Explicit) {
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw ("指定的 Python 不存在: " + $Explicit) }
        if (-not (Test-PythonWorks $Explicit)) { throw ("指定的 Python 跑不起来: " + $Explicit) }
        return $Explicit
    }
    $cands = @()
    if ($env:OO_PYTHON) { $cands += $env:OO_PYTHON }
    $repo = Split-Path -Parent $here
    $parent = Split-Path -Parent $repo
    foreach ($root in @($repo, $parent)) {
        if (-not $root) { continue }
        $cands += (Join-Path $root "Python\pythoncore-3.15-64\python.exe")
        $cands += (Join-Path $root "Python\bin\python.exe")
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $cands += $cmd.Source }
    $bad = @()
    foreach ($c in $cands) {
        if (Test-PythonWorks $c) { return $c }
        if (Test-Path $c) { $bad += $c }
    }
    if ($bad.Count -gt 0) {
        Write-Host "    以下 python 存在但跑不起来（可能是换盘符后的坏 shim）：" -ForegroundColor Yellow
        foreach ($b in $bad) { Write-Host ("      " + $b) -ForegroundColor DarkGray }
    }
    throw "找不到可用的 python.exe。用 -Python 指定，或设置环境变量 OO_PYTHON。"
}

Write-Head "OO 启动前检查"

$busy = @()
foreach ($p in $Ports) {
    $owner = Get-PortOwner $p
    if ($null -ne $owner) {
        $nm = (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName
        $busy += $p
        Write-Host ("    端口 {0,-6} 已被 PID {1,-6} ({2}) 占用" -f $p, $owner, $nm)
    }
}

if ($busy.Count -gt 0) {
    Write-Host ""
    Write-Host "    有端口被占用，拒绝启动。" -ForegroundColor Red
    Write-Host "    先执行 stop_all.ps1 收掉旧进程，再运行本脚本。" -ForegroundColor Yellow
    Write-Host ""
    return 1
}

Write-Host ("    {0} 个端口全部空闲" -f $Ports.Count) -ForegroundColor Green

$py = Resolve-Python $Python
Write-Host ("    Python  {0}" -f $py)

Write-Host ""
Write-Host "[启动] ..."

# -NoExit：服务退出后窗口保留，方便看报错
$webCmd  = $py + " -u -m web.app --port 8399"
$gameCmd = $py + " -u -m game_server.server --ms-ok --unknown-ok"
$winStyle = if ($ShowWindow) { 'Normal' } else { 'Minimized' }

if ($Only -in @('all', 'web')) {
    Start-Process -FilePath "powershell" -ArgumentList @("-NoExit","-Command",$webCmd) -WorkingDirectory $here -WindowStyle $winStyle
    Start-Sleep -Milliseconds 800
}
if ($Only -in @('all', 'game')) {
    Start-Process -FilePath "powershell" -ArgumentList @("-NoExit","-Command",$gameCmd) -WorkingDirectory $here -WindowStyle $winStyle
}

$what = switch ($Only) { 'game' { '游戏服' } 'web' { 'web' } default { '两个服务' } }
$how  = if ($ShowWindow) { '前台' } else { '最小化' }
Write-Host ("    {0}窗口已启动（{1}）" -f $what, $how)

if ($NoWait) {
    Write-Host "    日志：work\logs\last.*.log"
    return 0
}

Write-Host ""
Write-Host "[自检] 等待端口就绪..."
$ready = $false
$secs = 0
for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 1
    $up = 0
    foreach ($p in $Ports) { if ($null -ne (Get-PortOwner $p)) { $up++ } }
    $secs = $i
    if ($up -eq $Ports.Count) { $ready = $true; break }
}

Write-Host ""
if ($ready) {
    Write-Host ("    {0} 个端口全部就绪（用时 {1} 秒）" -f $Ports.Count, $secs) -ForegroundColor Green
    Write-Host ""
    Write-Host "    服务已启动。" -ForegroundColor Green
    if ($Only -in @('all', 'web')) {
        Write-Host "    Web   http://127.0.0.1:8399/"
        Write-Host "    邮件  http://127.0.0.1:8399/admin/mail"
    }
    if ($Only -in @('all', 'game')) {
        Write-Host "    游戏  127.0.0.1:33100（用 launcher\dist\OO_Launcher.exe 启动客户端）"
    }
    Write-Host "    日志  work\logs\last.game.log / last.web.log"
    return 0
} else {
    Write-Host "    [警告] 20 秒内端口没全部就绪：" -ForegroundColor Yellow
    foreach ($p in $Ports) {
        if ($null -eq (Get-PortOwner $p)) { Write-Host ("        {0,-6} 仍未监听" -f $p) -ForegroundColor Yellow }
    }
    Write-Host "    去看服务窗口里的报错（-NoExit 会保留窗口；窗口最小化时从任务栏点开）。" -ForegroundColor Yellow
    return 1
}
