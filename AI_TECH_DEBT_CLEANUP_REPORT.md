# AI 技术债清理报告

日期：2026-10-07（Asia/Shanghai）。分支：`cleanup/ai-tech-debt-v1`。审计源码基线 `4c2bc9cf7d80ddda20a316d43f18893d7a9d8031`；下述源码行号默认指该基线，实际删除和当前分支变化见本报告。ACTIVE 指正式入口可达，不能推断本机进程已启用。

本轮完成技术债治理并停止。没有实现 Freqtrade、改交易阈值、启用 Live 或部署新交易服务。测试和可启动性不等同真实交易/策略收益验收。

## 统计与提交

| 指标 | 清理前 | 清理后 | 差值 |
|---|---:|---:|---:|
| 跟踪文件 | 820 | 828 | +8 |
| Python文件 | 583 | 582 | -1 |
| Python物理行 | 124884 | 124751 | -133 |
| test_*.py文件 | 268 | 268 | +0 |
| Markdown文件 | 118 | 126 | +8 |
| Shell脚本 | 2 | 2 | +0 |
| PowerShell脚本 | 7 | 7 | +0 |
| Compose文件 | 5 | 5 | +0 |
| 配置扩展名文件（含fixture） | 47 | 47 | +0 |
| SQL migrations | 21 | 21 | +0 |

口径与审计一致：全部 Git 跟踪文件，Python含测试/注释/空行；不含依赖或用户运行制品。删除1个136行脚本与4个死符号（含空白共27行）；新增兼容helper/测试依赖守卫后，Python净减133行。44个Markdown是移动归档而非丢弃；新增8份Markdown包括6份交付、执行计划、archive索引。没有删除依赖包、migration、有效测试或整个模块。

已验证源码HEAD：`239adf8ef26b8b746b6bcc2262f79583e33f43c4`；之后仅补齐本次报告/索引/计划，最终提交号以分支和交付包 VERSION.json 为准。

```text
c117b51 docs(cleanup): record audit and dependency graph before changes
0221b18 chore(cleanup): remove four unreferenced compatibility helpers
333c956 refactor(cleanup): centralize retired news configuration compatibility
3532282 chore(cleanup): remove one-off dated acceptance secret audit
17081b6 test(cleanup): fix stale websocket fixtures and optional database preflight
1acc563 docs(cleanup): archive historical data-layer reports
bc7b8d2 docs(cleanup): archive historical phase reports batch 2
c839352 docs(cleanup): archive historical phase reports batch 3
d6d7dee docs(cleanup): archive historical phase reports batch 4
cbf8ed1 docs(cleanup): archive historical phase reports batch 5
0bc7bcd chore(cleanup): require explicit dashboard paths and correct phase9 flag docs
239adf8 test(cleanup): distinguish missing historical milestone evidence
```

最后一笔提交仅包含六份报告补齐、README/archive索引、历史readiness引用修正与执行记录。独立终审：无Critical或Important问题；原Minor引用/空白已修复。终审独立复核44份归档字节不变、统计与保护哈希、PowerShell必填路径和解析、220项skip分类、正式fail-closed与六份交付/八个问题。

## 删除与收敛证明

| 删除/收敛 | 原消费者/现状 | 替代与保留 | 实际回归 |
|---|---|---|---|
| pipeline.run_strategy_v2 | 孤立的早期 V2 包装；全仓只有定义，无 import/CLI/test/string caller | strategies.runtime.screen_batch、build_market_view、screen_market | Batch1 47 passed；Batch2 strategies 134 passed/5 skipped |
| runtime._stage1_payload | 无调用的私有 _clean wrapper | 保留所有真实 _clean 消费者与 session serialization | Batch1 47 passed |
| providers.unavailable_provider_observations | 旧 capability stub，零调用 | 保留实际 fetch/receipt/missing coverage 与 normalize_provider_observations | Batch1、全部 strategies |
| SchemaVerificationRequired | 从未 raise/catch/import 的叶子异常 | AdapterSchemaError 继续拥有实际 schema error | Batch1 adapters/pipeline |
| phase7_acceptance_secret_audit.py | 固定 2026-09-25 baseline/project/image，一次性 CLI；没有仓库 caller | 正式 data_layer_acceptance log scan 与 phase9_secret_scan；Git 可恢复旧工具 | Batch3 41 passed/1 deselected（缺原历史） |
| news_mode handling | 三份完全重复处理及 AnalysisRequest 局部重复 | 一个 contracts helper，旧配置继续可读，退役门槛仍不恢复 | Batch2 134 passed/5 skipped |

以上四符号删除只代表仓库内无调用证据；未推断公开 DTO/protocol/serializer 外部无用。没有删除有效测试，没有删除整个 Phase 或 Nautilus 包。

## 分批治理

- 审计/依赖图先提交。Batch1 四死符号；Batch2 兼容 helper；Batch3 一次性 script；每批相关测试后单主题提交。
- Batch4：测试数据库依赖缺失由 KeyError/assertion 改为明确 skip，未改实际 DB 断言或已有隔离校验。修复 WS fixture 旧字符串组和旧函数签名，仍验证 reader 在第二次握手前启动。
- `.gitattributes` 保证 Git checkout LF，避免 Windows 转换破坏 replay/stamp byte hashes。未修改 frozen manifest 或重新签名旧证据。
- 归档 44 份文档，分 10/10/10/10/4 个文件五次提交；每批 27 passed、Markdown links 无新增失效。
- Dashboard scripts 去掉开发者 home 默认，两个 Linux 路径改必填，PowerShell AST 解析/参数检查通过；Dashboard 82 passed/8 skipped。文档 PHASE9_ENABLED 改为1。
- 原 Phase9 19 个 milestone hashes 在上传快照历史中不存在；仅相应 evidence test 明确 skip，正式验收 fail-closed 保持原样。该测试组 14 passed/1 skipped。

## 测试证据

| 验证 | passed | failed | skipped | errors | 说明 |
|---|---:|---:|---:|---:|---|
| 首轮基线（CRLF） | 2148 | 21 | 128 | 99 | 原始 Windows checkout，raw日志不公开以避免原生 payload |
| 原始 Git blob LF 基线 | 2175 | 4 | 128 | 89 | 没有生产修改；DB缺失、旧WS fixture、历史Git缺失 |
| 最终最大离线集合 | 2176 | 0 | 220 | 0 | tests 全目录，52条既有warning，146.48秒，退出码0 |
| 前端 | 17 | 0 | 0 | 0 | 4 test files，类型检查退出0 |

220项跳过逐项分类：211项数据库/隔离DB配置依赖、8项公开API联网探测未opt-in、1项原历史Git证据缺失。缺 TEST_POSTGRES_DSN 的 DB/迁移/reservation/restart tests 无法在本次安全环境实际执行；没有借用真实 canonical 数据库。原历史缺失对应1项 evidence test；formal acceptance 不可因此声称通过。

补充静态验证：全部582个Python文件AST解析成功；31个Phase9兼容保护路径、共61个配置/政策/迁移/保护文件与源码基线逐字节相同；34个Dashboard bundle/stamp哈希一致；所有268个原测试文件保留；全部Markdown本地链接无缺失。Phase9正式secret scanner退出0，findings_count=0；全仓启发式扫描828个跟踪文件，25个位置复核为模板/本地默认/声明/合成fixture，无已证实生产秘密。历史扫描未完成，不能称所有Git历史没有泄漏。

## Smoke 与安全边界

- 已有 PostgreSQL 容器 Unix socket `SELECT 1` 返回1，仅验证连接；没有读真实行情/Paper账户、运行迁移或执行写SQL。
- 离线 Collector fake-client lifecycle/clean shutdown、Dashboard 真 localhost 服务临时目录启停、canonical fixture/provenance/hash 读取、配置加载由测试覆盖。没有启动用户 Collector/Engine/Paper/Live。
- 全部21 migrations、config/policies、.env.example、真实数据/凭据、原WSL工作树未修改；Docker volumes/容器/镜像未清理。新代码只在独立 clone 和 cleanup 分支。
- 没有 force push/reset --hard/git clean/删除分支或tag/改远程历史。只正常推送新 cleanup 分支；不 merge、不发布新交易服务。
- Secret 启发式扫描范围为当前跟踪文件：未跟踪真实 .env；命中位置仅报告类型，不输出值。已确认样例 DSN 占位、本地默认、SecretStr字段声明、合成测试字符串；未证实实际生产凭据。没有自动轮换密钥。原上传前Git历史缺失，完整历史扫描状态 INCOMPLETE。若后续原历史证明实际 secret 曾提交，应标 SECRET_ROTATION_REQUIRED；本轮没有凭不明线索制造该结论。

## 八个核心问题

### 1. 真正核心模块

Phase1（公共行情/基础SQL）+ Phase2（衍生品单位）+ Phase3/7（flow与proof）+ quant_instruments + quant_data_layer + strategies V2 + Phase9（证据与生命周期）+ quant_execution（中立风控账本）+ quant_realtime_paper + 当前冻结 quant_nautilus；dashboard 为只读观测。quant_features/quant_research 和 Phase4/5/6/8 为复用计算、AI、条件扩展与研究能力。

### 2. 哪些 Phase 是生产依赖

Phase1–9 均有正式入口可达能力；Phase2–8 和9多受 flag/启用条件控制。Phase6 AIService 是当前V2复用；Phase7 SBE 即使onchain关闭仍可启用。该结论不是声称当前机器9个Phase都在运行。

### 3. 哪些只是历史遗留

旧 Stage1 evaluate_stage1/run_stage1 算法、Phase9旧 deterministic __call__/paper_v1、旧Jev兼容写入、V1风险readiness与fixture CLI 属局部 LEGACY_ACTIVE/TEST_ONLY；有真实复用/测试/保护契约，冻结保留。没有可以整体认定为纯废弃的 Phase 文件夹。

### 4. Price/Kline/OI/Funding/Flow/CVD/Instrument/Risk owner



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


### 5. 剩几套 Paper/Execution

1套中立 Execution/Risk/Store，1套 Nautilus 原生 Paper 撮合核心，2种 Paper运行入口（当前 realtime + 旧fixture CLI），2代 trade-plan语义（V1/V2）。Backtest/spike是研究路径，SQLite SessionStore是观测，不是第二套撮合/账户owner。

### 6. Freqtrade后可继续删除

证明账户/仓位/未知发送恢复/reconcile 完整接管后，逐项移除 assembly 的 Nautilus特定 position manager/native instrument/preflight/adapter wiring；分离 LocalPaper 类后退休旧fixture CLI、无价值 spike/acceptance。仍需回测则保留冻结 Nautilus；最后才删 dependency。保留数据库、migration、历史journals/行情；不能仅因Freqtrade计划删除Phase1–9。

### 7. AI Fastlane 可复用



直接复用原owner：Phase1 MarketDataBatch/Repository/closed bars；Phase2 OI与8h Funding normalization + unit contracts；Phase3 trade rollup与Bybit CVD；Phase7 SBE flow receipt/proof与spot flow；quant_instruments registered identity；quant_data_layer freshness/admission/error redaction；strategies MarketView、refresh/canonical_validation、structures、evidence链；Phase9 immutable snapshot/intake/outbox/lifecycle；quant_execution risk config、approve_intent、intent契约。

边界：研究用 Phase9 features、quant_research可复用，但不是实盘策略；所有 source保留 provenance/clock/unit/coverage。Fastlane不能直接把 sources.py的 SQL投影视为新canonical owner，也不能绕过已有risk/reservation。


### 8. 下一步最小改动文件（未实现）

| 文件 | 最小动作 |
|---|---|
| `src/quant_realtime_paper/assembly.py` | 替换 native instrument、position/recovery manager、risk_inputs、preflight、adapter_factory 的完整 execution boundary；只换 factory 不够 |
| `src/quant_realtime_paper/config.py` | 明确 backend/venue/account/profile选择并保持 paper-only安全边界 |
| 新增 `src/quant_freqtrade/adapter.py`（拟定路径） | 中立 ExecutionAdapter的 dry-run bridge、订单/position/fees/funding/恢复/能力映射；不在本轮创建 |
| 新增 `infra/freqtrade/compose.yml`、`config/freqtrade.dry-run.example.json`（拟定） | 隔离dry-run部署/非秘密模板；不复用原账户owner、不含key |
| 新增 `tests/quant_freqtrade/test_adapter_contract.py`（拟定） | 证明中立contract、未知发送/reconcile、dry-run、防双owner；不在本轮创建 |
| `src/quant_realtime_paper/runtime.py`、`gates.py` | 仅按接入后 readiness/session展示需要改；不是预先强制 |
| `src/dashboard/backend/paper.py`、`service.py` | 接管旧Nautilus专用展示时才改；中立账本读路径能复用 |
| `pyproject.toml` | 新集成extras；Nautilus无回测/旧Paper消费者后才移除optional依赖 |

Phase1–8采集、21个migration、quant_execution contracts/risk/trade_plan 原则上无需改；若能力不满足须拒绝而非放宽门槛。该表是源码切点，不是已验证的Freqtrade接口设计；下一阶段先决定账户/风控owner和实际bridge方案。

## 剩余阻塞与Ready判断

**Ready for Quent + Freqtrade Fastlane: NO（运行接入就绪）。** 本轮已提供可理解的源码基线与接入边界，但隔离数据库集成回归未执行、原正式验收历史缺失、final-HEAD policy approval/真实账户完整生命周期状态未核验。现行assembly仍强绑定Nautilus；新backend能力映射、风控/账户单一owner、dry-run deployment均待下一任务。

可开始下一阶段设计与开发，不等于可以启动交易。新增cleanup HEAD会令旧 exact-HEAD审批失效，按既有逻辑fail-closed；本轮未重签批准。

剩余债务：有语义分歧且受保护的Jev双persist、局部freshness/UTC helpers、V1 readiness路径、巨型runtime/config/db、两Windows脚本共享JSONcapture helper。保留有明确调用或重构会扩大范围的内容，并记录为后续专项，不以本次清理名义改语义。

完成后停止，等待下一阶段任务。
