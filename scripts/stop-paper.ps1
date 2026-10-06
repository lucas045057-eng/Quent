param([string]$Distro = "Ubuntu")
$repo = (& wsl.exe -d $Distro --exec wslpath -a (Join-Path $PSScriptRoot "..")).Trim()
if ($LASTEXITCODE -ne 0 -or -not $repo) { throw "Could not resolve the WSL project path." }
$helper = "$repo/scripts/realtime-paper-control.sh"
& wsl.exe -d $Distro --exec bash -lc "bash '$helper' stop '$repo'"
if ($LASTEXITCODE -ne 0) { throw "Realtime Paper stop request failed." }
