# OpenOrange 服务器启动（PowerShell 包装）
# 真正的逻辑在 start_server.py —— 那边引号处理更可靠。
#
# 用法:
#     .\start_server.ps1            # 后端 + 游戏服
#     .\start_server.ps1 -Backend    # 只启后端
#     .\start_server.ps1 -Game       # 只启游戏服

param(
    [switch]$Backend,
    [switch]$Game
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$args2 = @()
if ($Backend -and -not $Game) { $args2 += "--backend" }
if ($Game -and -not $Backend) { $args2 += "--game" }

& python (Join-Path $root "start_server.py") @args2
