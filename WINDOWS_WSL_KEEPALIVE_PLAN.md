# Windows WSL Keepalive Plan

## Scope

This is a host-lifecycle measure for the native Docker Engine running inside
Ubuntu WSL2. It does not change Quant business logic, migrations, algorithms,
resource limits, or exchange connectivity.

## Planned task

- Task name: `Quant-WSL-Keepalive`
- Trigger: At the interactive Windows user logon
- Program: `wsl.exe`
- Arguments: `-d Ubuntu --exec /bin/sleep infinity`
- Working model: one low-CPU foreground `sleep` process keeps the Ubuntu WSL
  distro alive while systemd, dockerd, containerd, and the Quant containers run
  normally. It emits no continuous log output and has an explicit stop path.
- Registration: completed and verified after the manual proof-of-concept passed.

## Manual proof-of-concept

Start from Windows:

```powershell
Start-Process -FilePath wsl.exe -ArgumentList '-d','Ubuntu','--exec','/bin/sleep','infinity' -WindowStyle Hidden
```

After the process is started, close other WSL terminals, keep Windows awake and
logged in, wait 10–15 minutes, and reconnect to Ubuntu. Verify:

```bash
uptime
systemctl is-active docker
docker info
docker ps
```

`WSL_KEEPALIVE_POC_PASS` was observed before task registration. The registered
task was then verified separately with one active task instance.

## Stop

Stop only the POC process by locating the explicit `wsl.exe` process launched
for this test and terminating that process. Do not use `wsl --shutdown`.

## Scheduled-task removal (after registration only)

```powershell
Stop-ScheduledTask -TaskName 'Quant-WSL-Keepalive'
Unregister-ScheduledTask -TaskName 'Quant-WSL-Keepalive' -Confirm:$false
```

The task must not run every minute and must not create overlapping
`sleep infinity` processes.

## Verification after registration

1. Confirm one task instance and one keepalive process.
2. Close WSL terminals and wait 15 minutes.
3. Reconnect and verify systemd, Docker, container uptime, restart counts,
   OOM flags, database size, and latest Phase 5 timestamps.
4. Run an independent 30-minute host stability window while Windows remains
   awake and logged in.

## Failure modes

- Windows sleep/hibernate or reboot can still pause or terminate WSL.
- Docker daemon failure is not hidden by this keepalive and must remain visible.
- A stopped WSL distro or stopped task means the Quant runtime is not expected
  to remain available.
- `restart: unless-stopped` is a separate container-recovery measure and is not
  a substitute for host persistence.

## Rollback

Stop and remove the scheduled task if it has been registered. If the runtime
must be fully stopped, the operator may explicitly run `wsl --terminate Ubuntu`.
This plan never automatically runs `wsl --shutdown` because that could affect
other WSL distributions.
