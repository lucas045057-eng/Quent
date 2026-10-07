# 配置权威来源

日期：2026-10-07（Asia/Shanghai）。分支：`cleanup/ai-tech-debt-v1`。审计源码基线 `4c2bc9cf7d80ddda20a316d43f18893d7a9d8031`；下述源码行号默认指该基线，实际删除和当前分支变化见清理报告。ACTIVE 指正式入口可达，不能推断本机进程已启用。

本轮不改配置值、策略阈值、作用域或加载优先级；只收敛 `news_mode` 兼容处理的 owner 与去除 Dashboard 开发者路径默认。一个概念一个 owner，不代表不同账户、策略、源单位和展示接口应共享同一 JSON。

## 配置来源与覆盖顺序


Python does not load `.env` automatically. `rg` found no dotenv loader in these config owners. `.env`/`.env.local` take effect only through the explicitly selected Compose/env-file path or an operator's exported environment; `.env.example` is documentation, not an automatic source.

| Concept | Authority and precedence | Readers / references | Retention / issues |
|---|---|---|---|
| Core collector/engine settings | `Settings.from_env(environ)` explicit mapping, otherwise `os.environ`, otherwise per-field defaults | `src/quant_phase1/config.py:379-380`; Collector `entrypoints/collector.py:1720`; Engine `entrypoints/engine.py:916` | KEEP owner. `TRADING_MODE` must be paper; private exchange credentials rejected (`config.py:381-388`). Do not consolidate changing defaults or bounds. |
| Core DB | `POSTGRES_DSN`, otherwise core default | `src/quant_phase1/config.py:519` | Never emit actual operator DSN; KEEP user config. Docker Compose environment supplies profile-specific DB address. |
| Phase8 options | explicit mapping > process env > bounded `Phase8Settings` defaults | `src/quant_phase8/config.py:109-110`; Collector `entrypoints/collector.py:299` | Separate concept from Phase1/7 settings; KEEP. |
| Phase9 enable/resource ceilings | explicit mapping > process env > frozen ceilings; enabled only exact `0`/`1` | `src/quant_phase9/config.py:48-71`; Engine `entrypoints/engine.py:715,918`; Paper `assembly.py:314` | KEEP; documentation `true` spelling is wrong. |
| Paper root / symbols / poll / session file | RuntimeConfig explicit map > process env > repository root, BTC/ETH, 15s; optional CLI `--state-dir` overrides state DB | `src/quant_realtime_paper/config.py:38-53`; CLI `cli.py:175-179` | KEEP user SQLite/session history. CLI directory does not alter risk/strategy policy. |
| Paper canonical DB | `QUANT_REALTIME_PAPER_DSN` > `POSTGRES_DSN` > None | `src/quant_realtime_paper/config.py:52`; `scripts/start-realtime-paper.sh:34-50` | Shell adapter additionally reads existing Collector DSN if neither is exported and replaces container hostname with existing PostgreSQL IP. KEEP explicit WSL adapter. |
| Dashboard DB/root/port | `QUANT_DASHBOARD_DSN` only; `QUANT_DASHBOARD_ROOT`/`PORT` defaults, then CLI root/port override | `src/dashboard/backend/config.py:34-39`; `__main__.py:18-19` | Deliberately does not fall back to Paper/core DSN. Loopback-only validation at `config.py:24-31` is a safety boundary, not accidental duplication. |
| Strategy execution policy | `QUANT_V2_EXECUTION_POLICY_PATH` > repository `config/strategy_policy_v2.json` | `src/strategies/runtime.py:170-174`; Engine `entrypoints/engine.py:721`; Paper `assembly.py:331` | KEEP default + full-market config. Full-market file is not auto-loaded solely because it exists; selected path determines active allowlist. Missing/bad policy blocks Paper, digest must match approved manifest (`assembly.py:332-334`). |
| Policy manifest / approval | environment path > RuntimeConfig path > retained `policies/phase9_policy_v1.json` and approval filename; relative paths rooted by assembly | `src/quant_realtime_paper/config.py:54-64`; `assembly.py:279-285`; Engine `entrypoints/engine.py:724-725` | Filename is V1 for deployment compatibility, content is V2 (`docs/QUANT_PAPER_V2.md:15`). Not duplicate V1 policy. Approval binds final commit and remains mandatory. |
| V2 risk | `QUANT_RISK_CONFIG_PATH` > `config/risk_policy_v2.json`; valid revision checked at new-intent boundary | `src/quant_realtime_paper/assembly.py:335-337`; `src/dashboard/backend/service.py:22`; `src/quant_execution/risk_config.py:72-117` | KEEP. Dashboard and Paper use same logical file. Paper resolves relative path against project root; Dashboard directly creates Path, so relative override depends on process CWD: document absolute override rather than changing behavior in this batch. |
| Legacy RiskPolicyV1 monitor binding | `QUANT_REALTIME_PAPER_RISK_POLICY_PATH`; parsed separately from V2 risk config | `src/quant_realtime_paper/config.py:34,65,80`; `runtime.py:189-209,265`; test `tests/quant_realtime_paper/test_runtime.py:521` | LEGACY_ACTIVE/TEST path. Config field itself is not consumed by assembly V2, but readiness helper still reads env. UNKNOWN for removal; do not delete or alias to V2 JSON (different schemas). |
| Paper execution profile | environment path > RuntimeConfig configured path; no synthesized default profile/account | `src/quant_realtime_paper/assembly.py:338-346`; `config.py:66` | KEEP operator file. Requires existing account, BITGET_PAPER venue and explicit costs. Do not fabricate profile to simplify startup. |
| External research credentials | explicit `QUANT_V2_SERVICES_CONFIG_PATH` > existing `config/research_services.local.json` > environment credentials | `src/strategies/service_config.py:59-67` | Config file authoritative; invalid file raises sanitized error, never falls back. `.gitignore:20` and `.dockerignore:9` exclude default secret file. Retired service keys intentionally discarded at `service_config.py:51-54` for old user files; preserve compatibility. |
| OI unit confirmation | explicit `BITGET_OI_UNIT_CONTRACT_PATH` via Settings > repository `bitget_oi_unit_contract_full_market.json` if present > None | `src/quant_phase1/config.py:532`; Phase2 `runtime.py:113`; `src/quant_phase2/unit_contracts.py:25-30` | KEEP user-confirmed proof and smaller contract. `tests/test_bitget_uta_open_interest.py:102` explicitly reads the older smaller contract. No inference of ticker units from endpoint contract. |
| SBE flow scope | function argument > `BITGET_SBE_FLOW_SCOPE_PATH` > explicit legacy BTC/ETH scope | `src/quant_phase7/flow_scope.py:48-54`; `src/quant_phase1/config.py:736` | KEEP full-market scope JSON. It is opt-in configuration and cached by resolved path; do not silently switch scope/default. |
| Sector taxonomy | `PHASE5_TAXONOMY_PATH` > `config/phase5_sector_map.csv` | `src/quant_phase1/config.py:665` | KEEP data/config asset. |
| Phase7 labels / whale thresholds | environment-specified reviewed files; both optional, missing sources remain unavailable | `src/quant_phase1/config.py:747-748`; `src/quant_phase7/context_config.py:21,48,131` | `phase7-config/*.json` are ignored user-owned assets. Never delete. |
| Compatibility closure marker | exact marker file content and protected tree digest | `src/quant_phase9/compatibility_gate.py:37-69,86-87`; tests `test_compatibility_gate.py:138` | KEEP. `.gitignore` belongs to protected digest, so broad ignore refactors have evidence implications. |


## 生产、部署与验收配置区别



Compose interpolation is a separate layer from container environment. Profile `environment` entries override `env_file` entries. Python then consumes the resulting process environment; an arbitrary exported variable is not forwarded into a container unless the Compose model passes it.

| File | Purpose / caller evidence | Decision |
|---|---|---|
| `docker-compose.yml` | Base development: builds collector/engine, owns `phase1_pgdata`; Phase3 off. Explicit same entrypoints at `:24,:46`. `tests/test_resource_limits.py:8-33`, `tests/test_phase3_safety_resources.py:19`, `tests/test_phase4_safety_resources.py:121`, Phase6 safety tests reference file. | KEEP TEST/DEV topology. Not equivalent to WSL local profile. |
| `docker-compose.local.yml` | Local stack fixed container names, separately owned `quant_local_pgdata`; explicit collector at `:18`; engine inherits Dockerfile CMD; optional maintenance migrate at `:134-142`. Phase2-6 on and Phase7-9 opt-in; 768/768/512MiB current resource contract. Tests `test_resource_limits.py:44-57`, phase5/6/7/8 safety/integration suites. | KEEP intended WSL profile. Actual currently deployed Compose/overlay UNKNOWN. |
| `docker-compose.server.yml` | Two services, `.env` at `:5-6,:29-30`, external existing network at `:46-48`, no local PostgreSQL service/volume. `tests/test_resource_limits.py:36-41`; phase3/4 safety resources. | KEEP LEGACY_ACTIVE/DEPLOYMENT until verified unused externally. Named network is deployment coupling; do not remove without deployment evidence. |
| `docker-compose.phase7-acceptance.yml` | Disposable/isolated Phase7 services/project volume, no published ports, explicit local image. Used by `phase7_acceptance_preflight.py:15`, old secret audit `:16`, required in `tests/test_phase7_safety_gates.py:63-91`. | KEEP formal test ability even if old CLI audit deleted. |
| `docker-compose.data-layer-acceptance.yml` | Integrated Stage8 isolation and fixed resource/swap/log contract. `scripts/data_layer_acceptance.py:36,1098-1108,1447-1468`; `tests/test_data_layer_acceptance_runner.py`. | KEEP formal acceptance topology. |

`docs/LOCAL_RUNTIME_RESOURCE_BUDGET.md:19` records the 2026-09-30 Collector from another checkout plus `/tmp/quant-phase3-stream-overlay-compose.yml`. This is historical evidence only; do not claim current WSL deployment established by a filename or this old observation.


## 清理后的权威规则

- Python 不自动读 `.env`。实际来源取决于明确选用的 Compose env_file/环境导出，不能把 `.env.example` 当运行配置。
- `news_mode` 三个 policy validator 与 AnalysisRequest 共用 `strategies.contracts.discard_retired_news_mode`。旧本地配置仍能读取，不恢复退役新闻/宏观门槛，不修改输入 dict。
- `PHASE9_ENABLED` 只接受 `0`/`1`；文档已改成 `1`。不改 loader。
- `QUANT_RISK_CONFIG_PATH` 建议用 Linux 绝对路径。Paper 以项目 root 解析相对路径，Dashboard 以工作目录解析相对路径；不在清理中改变这一现有语义。
- Dashboard Windows wrapper 的 `-RepoPath`、`-PythonPath` 必填；`-Distro` 默认 Ubuntu、`-Port` 默认 3000。配置留在操作命令/受保护环境，不写进源码。
- full-market strategy、OI、flow scope 是现行有效配置。默认文件和小样本文件有不同 scope/测试消费者，不能按 new/old 命名删除。
- `QUANT_REALTIME_PAPER_RISK_POLICY_PATH` 仍为 LEGACY_ACTIVE 的 V1 readiness 分支，和 V2 risk schema 不同，未冒险别名合并。
- `research_services.local.json`、`phase7-config/*.json`、批准文件、execution profile 都是用户资产或激活边界，未读取/删除/制造。
- 5 个 Compose 均有用途。精确部署 overlay UNKNOWN，已有 PostgreSQL 容器 labels 为空，主机采集/Engine/Paper 进程本次读到的列表为空；不能宣布已运行本分支或选择哪份 overlay。
