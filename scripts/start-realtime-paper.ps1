param(
  [string]$Duration = "24h",
  [string]$Distro = "Ubuntu"
)
if ($Duration -notmatch '^[1-9][0-9]*(s|m|h|d)$') {
  throw "Duration must look like 15m, 24h, 72h, or 7d."
}
$repo = (& wsl.exe -d $Distro --exec wslpath -a (Join-Path $PSScriptRoot "..")).Trim()
if ($LASTEXITCODE -ne 0 -or -not $repo) { throw "Could not resolve the WSL project path." }
$helper = "$repo/scripts/start-realtime-paper.sh"
& wsl.exe -d $Distro --exec bash -lc "bash '$helper' '$Duration' '$repo'"
if ($LASTEXITCODE -ne 0) { throw "Realtime Paper runner did not start; see the message above." }