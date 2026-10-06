param([int]$Port=3002)
$ErrorActionPreference='Stop'
$repo=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$savedDsn=$env:QUANT_DASHBOARD_DSN
$savedSchema=$env:QUANT_DASHBOARD_SCHEMA
$savedBridge=$env:WSLENV
try {
    $env:QUANT_DASHBOARD_DSN='postgresql://postgres@127.0.0.1:1/unreachable'
    $env:QUANT_DASHBOARD_SCHEMA='launcher_readonly_probe'
    & (Join-Path $repo 'scripts/start-dashboard.ps1') -Port $Port
    $response=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/overview" -TimeoutSec 10
    if ($response.data.database -ne 'ERROR') { throw 'Windows database configuration did not reach the read-only backend.' }
    if ($env:WSLENV -cne $savedBridge) { throw 'Launcher changed the caller environment bridge.' }
    if (($response | ConvertTo-Json -Depth 20) -match 'unreachable|postgresql://') { throw 'Private database configuration leaked into the API.' }
    Write-Host 'PASS: Windows configuration forwarding, bridge restoration, safe database error.'
} finally {
    & (Join-Path $repo 'scripts/stop-dashboard.ps1') -Port $Port
    $env:QUANT_DASHBOARD_DSN=$savedDsn
    $env:QUANT_DASHBOARD_SCHEMA=$savedSchema
    $env:WSLENV=$savedBridge
}
