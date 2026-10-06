# Local runtime resource budget

This document defines the expected memory limits for the repository's future local Compose runtime. It does not authorize or perform a Compose restart, recreate, or update. The existing containers and canonical PostgreSQL instance were inspected read-only and remain untouched.

## Expected local Compose limits

| Service | `docker-compose.local.yml` | Expected runtime limit |
| --- | ---: | ---: |
| Collector | 768 MiB | 768 MiB |
| Engine | 512 MiB | 512 MiB |
| PostgreSQL | 768 MiB | 768 MiB |

The `docker-compose.local.yml` file is the profile for this decision. The base development, server, and isolated acceptance profiles retain their profile-specific historical budgets; their limits are not evidence of the local long-running service's working set.

## Why the repository and deployed runtime differed

The local Compose file at commit `72a9807` set Collector to 256 MiB, Engine to 384 MiB, and PostgreSQL to 768 MiB. That commit explicitly restored the Collector's prior repository budget. The historical `docs/realtime-paper-readiness-report.md` separately records a temporary runtime-only Collector override of 768 MiB (and 1536 MiB memory-plus-swap) after an existing Collector had exited with code 137. That temporary override did not update the repository Compose file or image.

The currently running, pre-release containers are not sourced from this reconciliation worktree. Docker's read-only labels identify the Collector Compose files as `/home/lucas045057/projects/quant/docker-compose.local.yml` plus `/tmp/quant-phase3-stream-overlay-compose.yml`; the external overlay sets Collector to 512 MiB with a 1 GiB memory-plus-swap limit. The current container limits inspected on 2026-09-30 were Collector 512 MiB, Engine 384 MiB, and PostgreSQL 768 MiB. Collector and Engine have different historical sources: the 256 MiB checked into Compose, the documented temporary 768 MiB override, and the later 512 MiB overlay. They should not be treated as one canonical configuration.

The previously exited code-137 containers inspected in Docker reported `OOMKilled=false`, including exited Collector instances and an Engine instance. Exit code 137 alone therefore does not establish an OOM cause. The new limits are selected from measured headroom, not from an unsupported diagnosis of those exits.

## Runtime evidence used for the budget

Read-only samples from the existing containers on 2026-09-30 showed:

| Service | Existing cap | Docker working-set sample | cgroup current / peak | Process RSS / high-water RSS | OOM evidence |
| --- | ---: | ---: | ---: | ---: | --- |
| Collector | 512 MiB | 481 MiB (about 94%) | 482.1 / 524.5 MiB | 493.2 / 524.4 MiB | `oom=0`, `oom_kill=0`; cgroup `max` counter 36,735 |
| Engine | 384 MiB | 334.7 MiB (about 87%) | 336.2 / 377.2 MiB | 354.4 / 394.3 MiB | `oom=0`, `oom_kill=0`; cgroup `max` counter 0 |
| PostgreSQL | 768 MiB | 461.7 MiB (about 60%) | 766.9 / 768.1 MiB | PID 1 RSS is not representative of PostgreSQL child processes | `oom=0`, `oom_kill=0`; cgroup `max` counter about 2,997,090 |

Docker working-set values are not directly interchangeable with raw cgroup `memory.current`: PostgreSQL's cgroup sample included about 720.3 MiB of file cache (about 299.4 MiB inactive and 284.3 MiB active), while the Docker working-set sample was lower. Keep PostgreSQL at its existing 768 MiB cap; this closure does not alter the database or its container.

Collector's cgroup and process high-water samples were both about 524 MiB under the currently inspected 512 MiB cap, while the cgroup recorded memory-limit events but no OOM kill. A 768 MiB future cap leaves about 244 MiB above that observed peak. Engine's cgroup peak was about 377 MiB under 384 MiB, leaving little headroom; a 512 MiB cap leaves about 135 MiB above that peak. These are observed pre-release process samples, not measurements of the reconciliation HEAD.

These samples support service-level limits only. They do not certify a simultaneous combined-load envelope or an extended soak on the release candidate. No existing container was updated to the expected values in this change.
