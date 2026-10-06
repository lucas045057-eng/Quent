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
$commandArgs=@('-d',$Distro,'--cd',$RepoPath,'--exec',$PythonPath,'-m','dashboard.backend','stop','--root',$RepoPath,'--port',"$Port")
$result=Invoke-DashboardWslJson $commandArgs 'Dashboard stop failed; no other process was selected for termination.'
if ($result.state -eq 'NOT_OWNED') { Write-Warning 'PID ownership does not match. The existing process was preserved.'; return }
Write-Host 'Dashboard stopped.'
