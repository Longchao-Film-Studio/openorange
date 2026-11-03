<#
.SYNOPSIS
    收掉 OO 的全部服务（按端口归属，不按进程名）。

.DESCRIPTION
    服务共占 4 个端口，但正常只有 2 个进程监听：
        game_server -> 33100 MS + 33101 实体 + 33102 管理
        web.app     -> 8399

    一旦多开，端口会被拆散到不同进程上（实测踩过：33100/33101 在新进程，
    33102 还挂在旧进程上）。于是网页命令打到旧进程，玩家连的是新进程，
    两份内存各走各的 —— 表现为「剧情过了但面板不开放」「网页发的邮件游戏里收不到」。

    为什么按端口找进程，不按进程名或 PID：
      - 按名字会误杀你自己的其它 python（数据脚本等）
      - 记 PID 容易过期
      - 端口是唯一能证明「这是本项目的服务」的证据

.PARAMETER Ports
    要清理的端口，默认 33100 33101 33102 8399。

.PARAMETER Force
    跳过确认，直接杀。

.PARAMETER WhatIf
    只列出会被杀的进程，不动手。

.EXAMPLE
    .\stop_all.ps1 -WhatIf
    .\stop_all.ps1 -Force
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [int[]]$Ports = @(33100, 33101, 33102, 8399),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

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

Write-Head "OO 服务停机"

$targets = @{}
$freeCount = 0
Write-Host "[1/3] 检查端口占用..."

foreach ($p in $Ports) {
    $procId = Get-PortOwner $p
    if ($null -eq $procId) { $freeCount++; continue }
    if (-not $targets.ContainsKey($procId)) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        $targets[$procId] = [pscustomobject]@{
            Id    = $procId
            Name  = if ($proc) { $proc.ProcessName } else { "?" }
            Ports = [System.Collections.Generic.List[int]]::new()
        }
    }
    $targets[$procId].Ports.Add($p)
    Write-Host ("    端口 {0,-6} <- PID {1,-6} ({2})" -f $p, $procId, $targets[$procId].Name)
}

if ($targets.Count -eq 0) {
    Write-Host ("    {0} 个端口全部空闲，没有需要停的进程。" -f $Ports.Count)
    Write-Host ""
    Write-Host "提示：重新启动用 start_all.ps1"
    return 0
}

Write-Host ("    共 {0} 个进程占用 {1} 个端口。" -f $targets.Count, ($Ports.Count - $freeCount))

if (-not $Force -and -not $WhatIfPreference) {
    Write-Host ""
    foreach ($t in $targets.Values) {
        Write-Host ("    将停止 PID {0} ({1})  端口 {2}" -f $t.Id, $t.Name, ($t.Ports -join ",")) -ForegroundColor Yellow
    }
    Write-Host ""
    $ans = Read-Host "    确认停止？(y/N)"
    if ($ans -notmatch "^[yY]") { Write-Host "    已取消。" -ForegroundColor DarkGray; return 0 }
}

Write-Host ""
Write-Host "[2/3] 停止进程..."
$killed = 0
foreach ($t in $targets.Values) {
    if ($PSCmdlet.ShouldProcess(("PID {0} {1}" -f $t.Id, $t.Name), "停止进程")) {
        try {
            Stop-Process -Id $t.Id -Force -ErrorAction Stop
            Write-Host ("    PID {0} ({1}) 已停止" -f $t.Id, $t.Name)
            $killed++
        } catch {
            Write-Host ("    PID {0} 停止失败：{1}" -f $t.Id, $_.Exception.Message) -ForegroundColor Red
        }
    }
}

if ($WhatIfPreference) {
    Write-Host ""
    Write-Host "    [WhatIf] 以上是预览，未执行任何停止操作。" -ForegroundColor Cyan
    return 0
}

Write-Host ""
Write-Host "[3/3] 等待端口释放..."
$released = $false
$elapsed = 0
for ($i = 1; $i -le 15; $i++) {
    Start-Sleep -Seconds 1
    $elapsed = $i
    $busy = @()
    foreach ($p in $Ports) { if ($null -ne (Get-PortOwner $p)) { $busy += $p } }
    if ($busy.Count -eq 0) { $released = $true; break }
}

Write-Host ""
if ($released) {
    Write-Host ("    端口已全部释放（用时 {0} 秒）" -f $elapsed) -ForegroundColor Green
    Write-Host ""
    Write-Host ("    全部服务已停止（共停 {0} 个进程）。" -f $killed) -ForegroundColor Green
} else {
    Write-Host "    [警告] 仍有端口被占用：" -ForegroundColor Yellow
    foreach ($p in $Ports) {
        $owner = Get-PortOwner $p
        if ($null -ne $owner) {
            $nm = (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName
            Write-Host ("        {0,-6} <- PID {1} ({2})" -f $p, $owner, $nm) -ForegroundColor Yellow
        }
    }
    Write-Host ""
    Write-Host "    可能原因：进程需要管理员权限才能杀。请用管理员身份重跑本脚本。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "提示：重新启动用 start_all.ps1"
if ($released) { return 0 } else { return 1 }
