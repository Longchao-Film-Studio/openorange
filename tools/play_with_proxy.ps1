# OpenOrange: launch the game with proxy env vars set.
# Usage: double-click tools\play_with_proxy.bat, or run this script in pwsh.

$ErrorActionPreference = "Continue"

$GameDir  = "F:/haowancheng/GuiLongchao/game"
$GameExe  = Join-Path $GameDir "GuiLongchao.exe"
$ProxyUrl = "http://127.0.0.1:4430"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " OpenOrange - Launch Game (with proxy)" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $GameExe)) {
    Write-Host "[ERROR] game not found: $GameExe" -ForegroundColor Red
    exit 1
}

# 1) check local services
Write-Host "[1/4] Checking local services..." -ForegroundColor Yellow
$ports = [ordered]@{ 8443 = "auth"; 8322 = "web"; 33100 = "game"; 8080 = "serverlist"; 4430 = "proxy" }
$allUp = $true
foreach ($p in $ports.Keys) {
    $ok = Test-NetConnection -ComputerName 127.0.0.1 -Port $p -WarningAction SilentlyContinue -InformationLevel Quiet
    if ($ok) {
        Write-Host ("      [OK]      {0,-11} port {1}" -f $ports[$p], $p) -ForegroundColor Green
    } else {
        Write-Host ("      [MISSING] {0,-11} port {1}" -f $ports[$p], $p) -ForegroundColor Red
        $allUp = $false
    }
}
if (-not $allUp) {
    Write-Host ""
    Write-Host "  Start the servers first, in another window:" -ForegroundColor Yellow
    Write-Host "      cd F:/haowancheng/OpenOrange" -ForegroundColor White
    Write-Host "      python tools/run_all.py --proxy" -ForegroundColor White
    Write-Host ""
    $go = Read-Host "  Continue launching the game anyway? (y/N)"
    if ($go -ne "y" -and $go -ne "Y") { exit 1 }
}

# 2) proxy env vars (FPX.dll embeds libcurl and reads these)
Write-Host ""
Write-Host "[2/4] Setting proxy environment variables..." -ForegroundColor Yellow
$env:http_proxy  = $ProxyUrl
$env:https_proxy = $ProxyUrl
$env:all_proxy   = $ProxyUrl
$env:HTTP_PROXY  = $ProxyUrl
$env:HTTPS_PROXY = $ProxyUrl
$env:ALL_PROXY   = $ProxyUrl
$env:no_proxy    = "localhost,127.0.0.1,::1"
$env:NO_PROXY    = "localhost,127.0.0.1,::1"
Write-Host "      http_proxy  = $ProxyUrl" -ForegroundColor Gray
Write-Host "      https_proxy = $ProxyUrl" -ForegroundColor Gray
Write-Host "      all_proxy   = $ProxyUrl" -ForegroundColor Gray

# 3) register URL
Write-Host ""
Write-Host "[3/4] Register / login here first:" -ForegroundColor Yellow
Write-Host "      http://127.0.0.1:8322" -ForegroundColor White
Write-Host "      (register + login, or the game cannot get a code)" -ForegroundColor Gray

# 4) launch
Write-Host ""
Write-Host "[4/4] Launching game..." -ForegroundColor Yellow
Write-Host "      $GameExe" -ForegroundColor Gray
Write-Host ""
Write-Host "Keep this window open while playing." -ForegroundColor DarkYellow
Write-Host ""

Push-Location $GameDir
try {
    & $GameExe
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Game exited." -ForegroundColor Cyan
