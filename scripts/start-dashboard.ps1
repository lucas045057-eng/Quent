param(
    [ValidatePattern('^[A-Za-z0-9_. -]+$')][string]$Distro='Ubuntu',
    [ValidatePattern('^/[A-Za-z0-9_./ -]+$')][string]$RepoPath='/home/lucas045057/projects/quant-integration-nautilus-v1',
    [ValidatePattern('^/[A-Za-z0-9_./ -]+$')][string]$PythonPath='/home/lucas045057/projects/quant-dashboard-v1-env/bin/python',
    [ValidateRange(1024,65535)][int]$Port=3000
)
$ErrorActionPreference='Stop'
function Invoke-DashboardWslJson {
    param([string[]]$CommandArguments,[string]$FailureMessage)
    $previousPreference=$ErrorActionPreference
    try {
        $ErrorActionPreference='Continue'
        $captured=@(& wsl.exe @CommandArguments 2>&1)
        $nativeExitCode=$LASTEXITCODE
    } finally { $ErrorActionPreference=$previousPreference }
    if ($nativeExitCode -ne 0) { throw $FailureMessage }
    $jsonLines=@($captured | Where-Object {
        $_ -isnot [System.Management.Automation.ErrorRecord] -and $_.ToString().TrimStart().StartsWith('{')
    } | ForEach-Object { $_.ToString() })
    if ($jsonLines.Count -ne 1) { throw $FailureMessage }
    try { return ($jsonLines[0] | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw $FailureMessage }
}
$savedBridge=$env:WSLENV
try {
# Forward only the two explicitly supplied read-only database settings. The
# values stay in the child environment, never in command arguments or logs.
$forwardNames=@('QUANT_DASHBOARD_DSN','QUANT_DASHBOARD_SCHEMA')
$bridgeParts=@($savedBridge -split ':' | Where-Object { $_ -and (($_ -split '/')[0] -notin $forwardNames) })
foreach ($name in $forwardNames) {
    if ([Environment]::GetEnvironmentVariable($name,'Process')) { $bridgeParts += "$name/u" }
}
$env:WSLENV=$bridgeParts -join ':'
$baseArgs=@('-d',$Distro,'--cd',$RepoPath,'--exec',$PythonPath,'-m','dashboard.backend')
$statusArgs=$baseArgs+@('status','--root',$RepoPath,'--port',"$Port")
$status = Invoke-DashboardWslJson $statusArgs 'Dashboard environment unavailable. Follow docs/DASHBOARD_V1_OPERATIONS.md.'
if ($status.state -eq 'RUNNING') { Write-Host "Dashboard already running: $($status.url)"; return }
$hostDir=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'QuantDashboard'
New-Item -ItemType Directory -Path $hostDir -Force | Out-Null
$runId=[guid]::NewGuid().ToString('N')
$startArgs=$baseArgs+@('start','--root',$RepoPath,'--port',"$Port",'--hold')
$quotedArgs=($startArgs | ForEach-Object { if ($_ -match '\s') { '"'+$_+'"' } else { $_ } }) -join ' '
$hostProcess=Start-Process -FilePath (Get-Command wsl.exe).Source -ArgumentList $quotedArgs -WindowStyle Hidden -WorkingDirectory $hostDir -RedirectStandardOutput (Join-Path $hostDir "$runId.out.log") -RedirectStandardError (Join-Path $hostDir "$runId.err.log") -PassThru
$deadline=(Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    try {
        $status=Invoke-DashboardWslJson $statusArgs 'Dashboard status unavailable.'
        if ($status.state -eq 'RUNNING') { Write-Host "Dashboard running: $($status.url) (PID $($status.pid))"; return }
    } catch { if ($hostProcess.HasExited) { break } }
    if ($hostProcess.HasExited) { break }
}
throw 'Dashboard did not start. Port may be occupied or the frontend bundle requires rebuilding. See docs/DASHBOARD_V1_OPERATIONS.md.'
} finally {
    $env:WSLENV=$savedBridge
}
