# WSL2 Quant Runtime Guide

## Architecture

This host uses a native Docker Engine inside Ubuntu WSL2, not Docker Desktop
integration:

- WSL distribution: `Ubuntu`
- PID 1: `systemd`
- Docker context: `default`
- Docker socket: `unix:///var/run/docker.sock`
- Services: `docker.service` and `containerd.service`
- Quant services: `quant-postgres`, `quant-collector`, `quant-engine`
- Trading mode: `paper`

Windows must remain awake and logged in for the local runtime persistence
guarantee. Windows sleep, hibernate, reboot, or explicit WSL termination can
pause or stop the runtime.

## How it starts

Ubuntu systemd starts Docker and containerd. The Windows scheduled task
`Quant-WSL-Keepalive` starts one low-resource process:

```text
wsl.exe -d Ubuntu --exec /bin/sleep infinity
```

The process keeps the Ubuntu WSL distribution alive without a busy loop or
continuous logging. The three containers use `restart: unless-stopped`; this
recovers them after a Docker daemon restart, but does not replace health checks
or runtime monitoring.

## Verify

From PowerShell:

```powershell
wsl.exe -l -v
wsl.exe -d Ubuntu -- uptime
wsl.exe -d Ubuntu -- systemctl is-active docker
wsl.exe -d Ubuntu -- systemctl is-active containerd
wsl.exe -d Ubuntu -- docker ps
```

From Ubuntu:

```bash
docker inspect quant-postgres quant-collector quant-engine \
  --format '{{.Name}} state={{.State.Status}} restart={{.RestartCount}} oom={{.State.OOMKilled}} policy={{.HostConfig.RestartPolicy.Name}}'
docker exec quant-postgres pg_isready -U quant -d quant
```

The expected policy is `unless-stopped`; resource limits remain PostgreSQL
768 MiB, collector 256 MiB, and engine 384 MiB.

## Manually stop Quant

This stops only the three Quant containers and leaves WSL/Docker available:

```bash
docker stop quant-collector quant-engine quant-postgres
```

Because the policy is `unless-stopped`, explicitly stopped containers are not
automatically restarted. Start them again with:

```bash
docker start quant-postgres quant-collector quant-engine
```

Wait for `pg_isready` before checking application health.

## Stop the keepalive

Stop the scheduled task and its one process from PowerShell:

```powershell
Stop-ScheduledTask -TaskName 'Quant-WSL-Keepalive'
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -eq 'wsl.exe' -and $_.CommandLine -match 'Ubuntu.*sleep.*infinity' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId }
```

Do not terminate unrelated WSL processes.

## Stop WSL

Only when the operator explicitly wants to stop this distribution:

```powershell
wsl.exe --terminate Ubuntu
```

This guide never runs `wsl.exe --shutdown`, because that affects every WSL
distribution.

## Restart

1. Start or log in to Windows so the keepalive task runs.
2. Reconnect to Ubuntu.
3. Confirm systemd, Docker, and container status.
4. Confirm PostgreSQL readiness.
5. Confirm migration versions 001–010 and advancing Phase 5 timestamps.

For a Docker-only restart, use the existing systemd service and then verify all
three containers:

```bash
sudo systemctl restart docker
docker ps
docker exec quant-postgres pg_isready -U quant -d quant
```

## Remove the scheduled task

```powershell
Stop-ScheduledTask -TaskName 'Quant-WSL-Keepalive' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'Quant-WSL-Keepalive' -Confirm:$false
```

Removing the task does not delete containers, images, volumes, or source files.

## Recover Docker

```bash
systemctl is-enabled docker containerd
systemctl is-active docker containerd
sudo systemctl restart containerd
sudo systemctl restart docker
docker info
docker ps -a
```

If Docker does not recover, preserve `docker info`, `systemctl status`,
`journalctl -b`, and container inspect output before making further changes.

## Known limitations

- Windows sleep/hibernate and host reboot are outside this acceptance scope.
- The keepalive does not hide Docker daemon failure.
- `unless-stopped` does not prove application health and must not replace
  readiness probes, logs, restart counts, or OOM checks.
- The local environment currently provides legacy `docker-compose` 1.29.2
  rather than the v2 `docker compose` subcommand.
- This guide does not enable live trading, private APIs, or remote deployment.
