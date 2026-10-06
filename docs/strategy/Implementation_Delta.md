# Quant Paper V1 Final Implementation Delta

BASE b64eb84c3374f6050ca003271beb64856155c827；branch fix/rc24h-phase9-runtime-wiring。仅沿用现有Phase9→Risk→Intent→native LocalPaper→PostgreSQL/journal/runtime边界；无新增migration。

| REQUIRED | Final implementation | Tests |
|---|---|---|
| numeric evidence/predicate | EvidenceItem optional Decimal value/unit；GT/GTE/LT/LTE/BETWEEN；缺失/单位不符/值冲突UNKNOWN；旧无数值序列化hash保留 | quant_phase9/test_paper_v1.py；legacy contracts/golden replay |
| breakout/retest transformer | 已闭合连续bars、前20冻结位/volume、ATR、独立1–4bar retest、invalid/chase、左右2bar pivot | test_breakout_requires_separate_retest_and_frozen_level；test_bad_breakout_cannot_confirm；test_pivot_is_confirmed_only_after_two_right_bars |
| flow/CVD/OI/funding semantics | 同venue真实session两增量；CVD健康/digest、重复key/reset/gap；验证base OI精确15m；current funding8H及每venue健康 | test_flow_two_ratios_and_cvd_increments；source-reader CVD/session/funding regressions；OI/funding bounds |
| core/aux scoping | 7类core控制validator与runtime bridge；aux缺失仍报告；只HIGH intent | test_core_aux_scope_and_hard_veto_consumer；risk HIGH/identity/core-tamper |
| hard veto consumer | approved predicate veto + benchmark opposition、EXTREME、event risk；无AI/RS override；独立clock/quality | macro inclusive boundaries；test_stale_volatility_regime_cannot_be_promoted_by_fresh_benchmark；Phase5 vocabulary/quality regressions |
| dynamic stop/target/max-hold exits | snapshot-bound plan+cost净R、equity risk clamps；digest-bound target/hold；native reduce-only全量exit、残余stop保护、durable replay；approval阻断时recovery-only退出 | quant_execution/test_paper_v1_risk.py；quant_nautilus/test_paper_v1_exits.py（两方向、partial entry/exit、replay、expired intent、real DB recovery） |
| stop direction key normalization | LONG/SHORT与BULLISH/BEARISH canonical alias；重复alias不同数值拒绝 | risk profile alias tests |
| event-risk health/recheck | calendar/security覆盖交集、60s freshness、3H coverage、空查询非健康；risk前+submit前双检查；known-no-submit fenced REJECTED/释放reservation | quant_realtime_paper/test_paper_v1_runtime.py；test_security_health_needs_its_own_coverage_bounds；test_current_event_rejection_is_fenced_durable_and_releases_reservation |

独立审阅7项Important均有RED→GREEN修复；额外真实DB测试发现不存在的intent created_at排序列，已使用现有valid_until/intent_id稳定排序。两项旧测试替身未接收BASE已有的Stage1 TTL keyword，仅修正替身签名，未改动Phase1运行代码。

保留边界：LocalPaper单历史intent/isolated-account、固定Sandbox初始余额/fee model；不宣称跨trade复用或现实source健康。真实流coverage/session与双事件health缺证明仍NO TRADE。完整限制及审阅裁决见Review_and_Rulings.md。

V2 deferred：其它patterns/horizons/indicator、heatmap/RS override、partial TP/trailing/pyramid/average down、研究排名与近触发名单、新增数据源。Production policy/approval不变；A1–A9、Docker build、24H、Live均未执行。完整测试及HEAD见Final_Test_Report.md。
