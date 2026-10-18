# OpenOrange: install root CA into "Trusted Root Certification Authorities"
# Needed only when using the proxy (so the client trusts our fake cert).
# Requires Administrator privileges.

$ErrorActionPreference = "Stop"

$Ca = Join-Path $PSScriptRoot "cert\ca.crt"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " OpenOrange - Install Root CA" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $Ca)) {
    Write-Host "[ERROR] not found: $Ca" -ForegroundColor Red
    Write-Host "        run first: python tools/gen_cert.py" -ForegroundColor Yellow
    exit 1
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[ERROR] Administrator privileges required." -ForegroundColor Red
    Write-Host "        Right-click Start -> Terminal (Admin), then run this script." -ForegroundColor Yellow
    exit 1
}

Write-Host "[1/2] Importing into LocalMachine\Root ..." -ForegroundColor Yellow
Import-Certificate -FilePath $Ca -CertStoreLocation "Cert:\LocalMachine\Root" | Out-Null
Write-Host "      [OK]" -ForegroundColor Green

Write-Host "[2/2] Importing into CurrentUser\Root ..." -ForegroundColor Yellow
Import-Certificate -FilePath $Ca -CertStoreLocation "Cert:\CurrentUser\Root" | Out-Null
Write-Host "      [OK]" -ForegroundColor Green

Write-Host ""
Write-Host "Done. Installed certificate:" -ForegroundColor Cyan
Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like "*OpenOrange*" } |
    Select-Object Subject, Thumbprint, NotAfter | Format-List

Write-Host "To uninstall: certmgr.msc -> Trusted Root Certification Authorities -> delete OpenOrange Fake CA" -ForegroundColor Gray
