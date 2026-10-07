# AI 技术债审计

审计日期：2026-10-07（Asia/Shanghai）。源码基线：`4c2bc9cf7d80ddda20a316d43f18893d7a9d8031`，来源 `origin/fix/phase9-decision-advisory-lock`；GitHub main 仅交接文档，不能作为源码基线。清理分支：`cleanup/ai-tech-debt-v1`。

范围：全部 Git 跟踪文件；不把 node_modules、Git 对象、运行制品、用户数据算作源码。先只读统计/AST import/CLI/Compose/脚本/测试引用，再创建分支。UNKNOWN 不删除。没有读取本地私钥、.env 或真实行情内容。

## 基线统计

| 指标 | 值 |
|---|---:|
| files | 820 |
| python_files | 583 |
| python_loc | 124884 |
| test_files | 268 |
| markdown | 118 |
| shell | 2 |
| powershell | 7 |
| compose | 5 |
| config_files | 47 |
| migrations | 21 |

配置统计口径：跟踪的 JSON/TOML/YAML/CSV/INI 文件，包含 fixture manifest，并非 47 套独立生产配置。Python LOC 为全部跟踪 Python 文件物理行数，包括测试、注释、空行。测试文件按 tests 下 test_*.py。

## 一级 Python 模块

| 模块 | 文件 | 分类 | 职责/保留理由 | 生产引用 | 测试引用 | fixture-only/被替代/删除 |
|---|---:|---|---|---:|---:|---|
| dashboard | 15 | ACTIVE | 只读 FastAPI、静态前端、数据库与历史制品展示 | 0 | 78 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_data_layer | 12 | ACTIVE | 准入、背压、新鲜度、错误/观测标准；replay 子目录为 TEST_ONLY | 42 | 46 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_execution | 9 | ACTIVE | 中立 intent、Python Risk、成本、预留、执行账本；paper_v1 为兼容政策 | 38 | 76 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_features | 2 | ACTIVE | 框架中立的 feature 计算 | 7 | 4 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_instruments | 2 | ACTIVE | symbol identity 与 canonical instrument 映射 | 4 | 5 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_nautilus | 12 | ACTIVE / 冻结 | Paper 原生运行与恢复；spike/acceptance/fixture_setup 仅研究验收 | 6 | 46 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_phase1 | 30 | ACTIVE / 部分 LEGACY_ACTIVE | 价格、Kline、Collector/Engine、基础模型、SQL 与旧 Stage1 兼容 | 99 | 307 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_phase2 | 16 | ACTIVE | OI/Funding 标准化、单位合约、衍生品观测、metadata | 5 | 51 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_phase3 | 23 | ACTIVE | 永续逐笔、Flow/CVD、去重、排序、补洞与持久化 | 15 | 72 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_phase4 | 17 | ACTIVE / 条件 | 跨市场、基差、清算与 long/short 扩展 | 6 | 61 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_phase5 | 14 | ACTIVE / 条件 | 市场 breadth/regime/sector context | 2 | 23 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_phase6 | 14 | ACTIVE / 条件及兼容 | 现行 AIService/security；旧外部 context ingestion 仍可按 flag 启用 | 16 | 66 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_phase7 | 21 | ACTIVE / 条件 | Spot Flow、SBE、全市场 scope；onchain context 可选 | 6 | 96 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_phase8 | 11 | ACTIVE / 条件 | Deribit options context，非 Fastlane 最小依赖但 Collector 已接线 | 2 | 43 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_phase9 | 34 | ACTIVE / 部分 LEGACY_ACTIVE | V2 证据/生命周期/持久化；旧 V1 和验收兼容不能整包删 | 66 | 177 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_realtime_paper | 11 | ACTIVE | 现行 assembly/monitor/store/gates/CLI | 1 | 39 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| quant_research | 4 | ACTIVE / 研究入口 | Nautilus 本地回测、研究导出与存储 | 0 | 4 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |
| strategies | 32 | ACTIVE | V2 market view、screening、证据链、AI 提案、执行准入 | 29 | 147 | 非整包 fixture-only；局部替代详见依赖图；整包 KEEP |

引用计数是 AST import 语句数，包含条件/函数内部 import，不等于当前进程已启用。每条精确证据见 MODULE_DEPENDENCY_MAP。

## 重复与多代实现

- 整文件精确重复：0（UTF-8 内容 SHA256；不据此宣称语义无重复）。
- `discard_legacy_news_mode` 在 analysis/evidence/execution 三处完全相同，保留旧配置兼容但收敛至同一个 contracts helper。
- Price/Kline/MarketObservation、各 Phase DataStatus/Provenance/Freshness、DecisionCandidate/ExecutionIntent/PositionSnapshot 属分层边界，字段、单位、持久化语义不同，禁止按同名合并。
- Phase9 两个 persist_jev_review 具有不同验证与引用；compatibility protected paths 保留，不做危险合并。
- retry_delay、UTC、adapter transport 相似实现：源速率策略/依赖边界不同，本轮不跨模块重写。
- Nautilus 原生 Sandbox 是现行 Paper 的真实依赖，不是 DEAD。

## 精确重复函数线索（不是删除授权）

- `src/quant_phase1/adapters/bitget_v3/rest.py:18:retry_delay`；`src/quant_phase2/adapters/base.py:73:retry_delay`
- `src/quant_phase1/adapters/bitget_v3/rest.py:40:__aexit__`；`src/quant_phase2/adapters/base.py:64:__aexit__`
- `src/quant_phase1/adapters/bitget_v3/rest.py:43:close`；`src/quant_phase8/adapters/deribit_rest.py:105:close`
- `src/quant_phase1/contracts.py:30:_check_timestamp`；`src/quant_phase6/contracts.py:49:_check_optional_utc`
- `src/quant_phase3/adapters/bitget.py:23:__init__`；`src/quant_phase3/adapters/bybit.py:19:__init__`；`src/quant_phase3/adapters/hyperliquid.py:18:__init__`；`src/quant_phase4/adapters/bitget_uta_v3.py:90:__init__`；`src/quant_phase4/adapters/bybit_v5.py:212:__init__`；`src/quant_phase4/adapters/hyperliquid_public.py:31:__init__`；`src/quant_phase4/adapters/hyperliquid_public.py:70:__init__`；`src/quant_phase4/adapters/hyperliquid_public.py:105:__init__`
- `src/quant_phase3/adapters/bitget.py:91:identity_key`；`src/quant_phase3/adapters/bybit.py:67:identity_key`；`src/quant_phase3/adapters/hyperliquid.py:59:identity_key`
- `src/quant_phase3/cross_exchange.py:14:_utc`；`src/quant_phase3/dedup.py:12:_utc`；`src/quant_phase3/recovery.py:13:_utc`
- `src/quant_phase3/freshness.py:11:_utc`；`src/quant_phase3/ordering.py:12:_utc`
- `src/quant_phase3/health.py:11:_utc`；`src/quant_phase3/queue.py:17:_utc`
- `src/quant_phase4/adapters/bitget_classic_v2.py:47:configure_transport`；`src/quant_phase4/adapters/bitget_uta_v3.py:41:configure_transport`；`src/quant_phase4/adapters/bybit_v5.py:56:configure_transport`；`src/quant_phase4/adapters/bybit_v5.py:109:configure_transport`
- `src/quant_phase4/adapters/bitget_uta_v3.py:24:_usdt_symbol`；`src/quant_phase4/adapters/bybit_v5.py:39:_usdt_symbol`
- `src/quant_phase4/adapters/bybit_v5.py:51:__init__`；`src/quant_phase4/adapters/bybit_v5.py:104:__init__`
- `src/quant_phase4/aggregation.py:18:_utc`；`src/quant_phase4/cross_exchange.py:28:_utc`；`src/quant_phase4/liquidation.py:17:_utc`
- `src/quant_phase4/persistence.py:27:_utc`；`src/quant_phase5/persistence.py:24:_utc`
- `src/quant_phase5/regime.py:31:_leader_status`；`src/quant_phase5/relative_strength.py:21:_status`；`src/quant_phase5/sector_context.py:16:_context_status`
- `src/quant_phase6/contract_v1.py:489:_require_utc`；`src/quant_phase6/persistence.py:36:_utc`
- `src/quant_phase6/contract_v1.py:448:handle_starttag`；`src/quant_phase6/contract_v1.py:454:handle_startendtag`
- `src/quant_phase6/sources.py:106:all`；`src/quant_phase7/sources.py:146:all`
- `src/quant_phase7/enrichment.py:23:_utc`；`src/quant_phase7/retention.py:36:_utc`
- `src/quant_phase7/labels.py:54:_text`；`src/quant_phase7/whale.py:37:_text`
- `src/quant_phase7/labels.py:63:_utc`；`src/quant_phase7/whale.py:46:_utc`
- `src/quant_phase8/freshness.py:63:_utc`；`src/quant_phase8/persistence.py:42:_utc`
- `src/quant_phase9/sources/phase5.py:31:_json`；`src/quant_phase9/sources/phase6.py:32:_json`
- `src/strategies/analysis/deep_analyzer.py:23:discard_legacy_news_mode`；`src/strategies/evidence/evidence_chain.py:20:discard_legacy_news_mode`；`src/strategies/execution/execution_policy.py:23:discard_legacy_news_mode`

## 巨型文件

| 文件 | LOC | 本轮决定 |
|---|---:|---|
| `scripts/data_layer_acceptance.py` | 2499 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/test_phase7_runtime_integration.py` | 2036 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase7/runtime.py` | 1852 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase1/entrypoints/collector.py` | 1738 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase4/runtime.py` | 1667 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/test_phase4_runtime.py` | 1475 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/test_data_layer_acceptance_runner.py` | 1303 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase1/db.py` | 1259 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_realtime_paper/runtime.py` | 1202 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase8/adapters/deribit_ws.py` | 1164 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/test_phase8_runtime_integration.py` | 1119 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase8/runtime.py` | 1107 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase7/persistence.py` | 991 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `scripts/run_phase7_resource_replay_v3_docker.py` | 990 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase1/entrypoints/engine.py` | 955 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `scripts/phase8_source_contract_probe.py` | 922 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `scripts/run_phase9_acceptance.py` | 919 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/test_gap_recovery.py` | 907 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase4/persistence.py` | 906 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase6/runtime.py` | 832 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase6/persistence.py` | 823 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase7/contracts.py` | 817 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase1/config.py` | 775 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase9/runtime.py` | 761 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase9/contracts.py` | 749 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `scripts/run_data_layer_replay_v1.py` | 733 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase5/contracts.py` | 731 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/test_phase7_contracts.py` | 731 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase9/intake.py` | 722 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/test_phase6_runtime_integration.py` | 705 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase8/context.py` | 703 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase9/jev.py` | 699 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/test_phase7_persistence.py` | 667 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_data_layer/observability.py` | 647 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/run_phase6_replay_v2_docker.py` | 639 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase8/persistence.py` | 578 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase8/universe.py` | 575 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/quant_realtime_paper/test_runtime.py` | 565 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/test_phase4_persistence.py` | 551 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_realtime_paper/assembly.py` | 550 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/phase6_loaded_replay_runner.py` | 544 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase5/runtime.py` | 536 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/test_data_layer_db_admission.py` | 536 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase9/compatibility_gate.py` | 535 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `scripts/phase7_source_contract_probe.py` | 532 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase8/adapters/deribit_rest.py` | 525 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `tests/quant_phase9/test_compatibility_gate.py` | 517 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase9/policy.py` | 516 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase6/ai.py` | 510 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |
| `src/quant_phase7/spot.py` | 508 | KEEP；跨模块拆分扩大范围，留到后续专项治理 |

## TODO/FIXME/HACK/TEMP/WORKAROUND/XXX 全部命中

- `src/quant_phase9/paper_v1.py:222`：HACK。有效禁止的 HACK 标签/测试断言，不是待移除注释。
- `src/quant_phase9/sources/paper_v1.py:62`：HACK。有效禁止的 HACK 标签/测试断言，不是待移除注释。
- `tests/quant_phase9/test_paper_v1.py:93`：HACK。有效禁止的 HACK 标签/测试断言，不是待移除注释。
- `tests/test_data_layer_db_admission.py:92`：TEMP。测试临时 SQL 资源语法，保留有效测试。
- `tests/test_phase3_persistence.py:183`：TEMP。测试临时 SQL 资源语法，保留有效测试。
- `tests/test_phase4_persistence.py:515`：TEMP。测试临时 SQL 资源语法，保留有效测试。
- `tests/test_phase4_persistence.py:521`：TEMP。测试临时 SQL 资源语法，保留有效测试。
- `tests/test_phase4_persistence.py:525`：TEMP。测试临时 SQL 资源语法，保留有效测试。

AST 扫描 broad except：180 处，其中单纯 pass/return body：36。print：108 处（包括 CLI 和测试报告）。异常吞咽与输出逐类保留安全 fail-closed、观测、CLI 协议；没有证据支持批量删除。没有发现可以确定为几十行旧实现的注释块。

## 基线测试与安全

首轮（Windows CRLF checkout，经 WSL Python 3.12）：2148 passed / 21 failed / 128 skipped / 99 errors。前端：17 passed（4 files）；tsc typecheck 通过。缺 TEST_POSTGRES_DSN、受哈希保护制品被 CRLF 转换、以及旧验收硬编码原仓库 commit 是主要原因；复测和具体残留见清理报告。不会用真实库补测试 DSN，不重签旧 acceptance。

WSL 原工作树 `/home/lucas045057/projects/quant-a6-secret-scan-fix` 干净，HEAD `9d2f11a`，origin 指向本地旧 Windows 工作树；未修改。现有 Docker PostgreSQL/AgentOps 未重启、删除或清理。.env.example 是模板，不是真实 .env。迁移 001–021 全部 KEEP。secret 审计详见清理报告；历史凭据扫描不等于当前跟踪文件扫描。

## 清理候选证明与计划

只删除无 import/CLI/config/test/subprocess caller 的四个小符号与冻结日期的一次性 secret audit；保留正式验收扫描器。根目录历史报告分批移动到 docs/archive，不搬受 formal contract 保护路径或原测试直接读取的报告。不改阈值、RR、A/B/C、OI/Funding/CVD 含义。

## 独立审计补充



|概念|采集/计算的正式 owner|持久化/读取 owner|调用证据与保留说明|
|---|---|---|---|
|Price / Ticker|`quant_phase1/adapters/bitget_v3/parsers.py:112`，REST/WS；契约 `quant_phase1/contracts.py:90`|`quant_phase1/repositories.py:100` 写 market_snapshots；`:268` load_latest_market_batch|`entrypoints/collector.py:265` CanonicalStore、`:813` 周期写入；Engine `:568`、Paper assembly `:126` 从 canonical batch 读取。ACTIVE KEEP。|
|Kline|`quant_phase1/adapters/bitget_v3/parsers.py:143`；`contracts.py:121` Candle；`pipeline.py:26` MarketDataCollector；closed bars/gap recovery|`repositories.py:68` upsert_candles → klines，`:268` canonical batch；migration 001|Collector `:739` 标 closed_klines admission，`:1083` 读闭合历史、`:1210` reconciliation；`strategies/market_view.py:54` structure_window 读取。ACTIVE KEEP。|
|OI|`quant_phase2/adapters/bitget.py:25` 等公共 adapter；`normalization.py:22` normalize_open_interest；`unit_contracts.py:9` 源单位契约；`contracts.py:91` OIObservation|`quant_phase2/persistence.py:71` 写 open_interest；`:170` 恢复历史；`quant_phase9/sources/phase2.py:36` 验证已持久化归一化；`strategies/sources.py:28` 消费|Engine `:813` Phase2DerivativeRuntime；Bitget adapter `:135/:237/:338` 调正式 normalization；V2 sources `:56` 复用 `_valid_oi_normalization`。不能以显示层再次计算验证为重复采集删掉。|
|Funding（市场率）|`quant_phase2/funding.py:19` normalize_funding_to_8h；adapter `bitget.py:105/:156`；`contracts.py:126` FundingObservation|`quant_phase2/persistence.py:99` funding_rates；V2 `strategies/sources.py:82` 读取/验证标准化率|与 execution funding cash ledger 不同语义。ACTIVE KEEP；V2 源当前重复算 8h 公式用于一致性验证，后续可提取共同验证函数，但不能直接换掉 freshness/interval 语义。|
|Perpetual Flow|多交易所旧公共 trade：`quant_phase3/contracts.py:68` CanonicalTrade、`flow.py:81` TradeFlowWindowBuilder、runtime；当前 Bitget SBE：`quant_phase7/bitget_sbe.py:190` worker、`:161` validate_flow_proof|`quant_phase3/persistence.py:68` → trade_flow_windows；SBE `bitget_sbe.py:405` 写同表|Collector `:325/:336` Phase3 trade runtimes，`:1406` 推 CVD；Phase7 runtime `:542` 创建 SBE worker。V2 `strategies/sources.py:89–110` **需要 SBE coverage proof**，旧可用 trade 行不能代替。两条 ingestion 语义/证据不同，保留。|
|Spot Flow|`quant_phase7/spot_flow.py:192` aggregate_spot_window（Binance source），`bitget_sbe.py`（Bitget source）|`quant_phase7/persistence.py:882` → phase7_spot_flow_windows；SBE `:403` 写同表|Phase7 runtime `:1326` aggregation；V2 sources 与 perp 共用验证闭合 candle proof。跨 source 不能合并成一条流。|
|CVD|`quant_phase3/cvd.py:29` BybitCVDBuilder：完整1m delta 的 15m/1H/4H/24H 滚动总和；`quant_phase7/spot_flow.py:192` 接 previous_cvd：source-specific spot 累计|cvd_snapshots（migration 008）；phase7_spot_flow_windows.cvd（migration 012）；Phase7 repository `:545` load_latest_spot_cvd|Collector `:322/:710/:1406` 实际构造/恢复/计算；Phase7 runtime `:1321/:1334` 读上次累计。Bitget SBE `:398` 显式 cvd=None，V2 使用 delta ratio，不能称其已替代 Bybit CVD。两种 CVD 不同 scope/窗口，KEEP。|
|Instrument|Phase1 `contracts.py:66` 原始市场元数据 → symbols；Phase2 `contracts.py:64` venue metadata → exchange_instruments；`quant_instruments/identity.py:31` registered identity owner|Phase1 repository `:27` 写 symbols；Phase2 repository `:46` 写 exchange_instruments；identity resolver 查询两表|Phase9 sources/phase2 `:83`、phase3 `:50`、phase4 `:36` 用 resolver。V2 `strategies/market_view.py:9` 是只读策略 projection；Execution `contracts.py:48` 是交易规格；Nautilus `instruments.py:8` 是框架 adapter；不是四套竞争 canonical model。可收敛边界转换，不可删消费者 DTO。|
|Risk|`quant_execution/risk_config.py:21` RiskConfigV2，`:96` reload loader，`:122` 转 RiskPolicyV1；`risk.py:65` approve_intent 唯一 sizing/批准边界|`quant_execution/persistence.py:57` reserve 在持久化层落实账户/notional/slot/fence；`strategies/execution/execution_policy.py:35` 是策略证据 gate|Paper assembly `:336/:428` 读 risk config、`:476–479` trade plan/resolve risk；wiring `:221` 检查 policy 后 approve_intent。策略 PASS 不等于风险批准；reserve 防竞态不是重复 Risk。|

正式 data quality/freshness owner：`quant_data_layer/freshness.py:19` 和 `FRESHNESS_POLICY`。Phase1 config、Paper config、Phase2 runtime、Risk 均引用它；strategies/refresh.py:7 的 MAX_AGE_SECONDS 仍有局部结构/refresh 时效映射，属于有消费者的独立策略阶段约束，当前不修改数值。


## 历史报告分类

- KEEP：README、当前 V2/Operations/Integration 文档、21 migrations、5 份有路径契约的根报告、用户历史/配置。
- ARCHIVE：44 份旧报告/设计和旧 current-strategy-map；清单见 docs/archive/README.md。
- DELETE：仅无调用的一次性旧 secret audit 与四个 dead 叶子符号；未找到可确认完全重复且无历史价值的整份文档，未凭命名删除。
- 既有 outputs/docs、旧实现计划仍有设计引用；本轮不做额外目录搬迁。

## Python 依赖与生成资产



- Runtime-required generated assets: `dashboard/frontend/dist/index.html`, all referenced hashed assets, `dist/build-stamp.json`. `src/dashboard/backend/lifecycle.py:18-40` verifies source and asset hashes before start; `app.py:109-124` serves those files. Frontend build script (`package.json:9`) regenerates both bundle and stamp. Static checker validated 34 hashes. Source files remain required by the stamp even when generated JS serves the UI.
- Test-required artifacts: replay fixtures/manifests, policies/approval fixtures and provenance-listed builder/profile scripts. Historical `.superpowers`/artifacts/user state cannot be classified by ignored status alone; archive/delete only after checking acceptance evidence references.
- `outputs/docs/` are authored early design documentation, not application runtime. `docs/superpowers/plans/2026-09-20-phase1-implementation.md:11,152` references them. ARCHIVE historical design as one group with relative links; not proven duplicate or safe DELETE.
- All base Python dependencies have direct production imports (AST counts): aiohttp 19; websockets 3; pydantic 14; psycopg 74. Optional FastAPI 4, uvicorn 1, nautilus_trader 46. KEEP; no unused-library deletion supported.
- `httpx` has zero direct production imports but `fastapi.testclient.TestClient` appears in at least five dashboard test files (`test_dashboard_api.py:8`, `test_realtime_paper.py:9`, `test_dashboard_events.py:71`, `test_dashboard_decisions.py:77`, `test_dashboard_launcher.py:86`). It is the TestClient backend requirement. Do not remove and lose tests; possible later split into dashboard-test extra if install docs and full API tests move together. Current dashboard install command explicitly installs dev,nautilus,dashboard (`docs/DASHBOARD_V1_OPERATIONS.md:46`).
- Frontend `react-is` has no obvious direct source import but may be recharts runtime dependency; keep until installed dependency tree/bundler evidence establishes optionality. No dependency install/network undertaken.
