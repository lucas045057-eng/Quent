# Final Review and Rulings

独立fresh-context reviewer：gpt-6.1-sol（最初gpt-6-astra请求因额度不可用失败，未产生review）；审阅范围BASE b64eb84→dd8512c。审阅报告0 Critical、7 Important、0 deferred minor；原审阅结论pending fixes。修复提交463eedd以回归测试证明，不发起第二轮review。

| Important finding | Reproduction and resolution |
|---|---|
| partial entry exits伪FILLED | 两方向target/stop/emergency及durable partial-entry replay：RED quantity-conservation failure；GREEN保持真实entry终态/累计fill |
| approval阻断关闭已有退出 | RED blocked monitor无position manager；GREEN独立recovery-only manager；真实DB+durable plan+真实quote-shaped fixture关闭已有position，entry pipeline仍None |
| current event veto留下SUBMITTING | RED SUBMITTING≠REJECTED；GREEN fenced terminal零fill result释放reservation；错误owner拒绝；recover_pending为空 |
| stale volatility标FRESH | RED fresh benchmark遮住61s旧regime；GREEN每个required context clock/quality检查并留在canonical payload |
| CVD quality/session/digest被丢弃 | RED PARTIAL CVD仍AVAILABLE、aggregation_version冒session、duplicate覆盖；GREEN保留CVD provenance/health，缺session/重复key/冲突阻断 |
| fresh funding遮旧venue | RED STALE member仍核心FRESH；GREEN各当前venue都需FRESH |
| security coverage丢失 | RED security缺/过期end仍clear；GREEN双source覆盖交集，任一缺失NO TRADE |

额外边界修复：实际Phase5 TREND_UP/DOWN语义到benchmark veto；source质量对象PARTIAL不升级；新退出query排序使用existing schema；旧测试替身兼容BASE已有TTL参数。

## Rulings I made（完整）

1. 复用已有SMA(TR,14)、strict confirmed pivots与quote turnover。理由：保持当前公式和可重放因果定义。若不适用：须新审阅policy版本，不能视为收益最优公式。
2. 不从空表或numeric completeness升级coverage。理由：保持core fail-closed。代价：缺source健康证明会阻断Paper，需RC验证。
3. OD DECIDED限V1子集；OD02/23继续research deferred。理由：不发明V2/部署默认。代价：未覆盖研究报告不变。
4. 缺session、显式coverage、health时不从successful reads推定。理由：source事实不足就是UNKNOWN。代价：现有source contracts/config可能需另行健康证明才可激活。
5. Reviewer未裁决的multi-intent restoration/fixed Sandbox cash继续保留。理由：是BASE既有pilot架构，用户禁止重设计；本轮只完成8项消费者能力。代价：RC必须注明单历史intent/account，不能声称多次交易账户复用或实际paper余额/fee一致性已验证。
6. Reviewer未裁决的新ingestion/session/coverage sources本轮不实现。理由：新增source超出8项REQUIRED；保持UNKNOWN。代价：现实source未满足证明时持续NO TRADE。
7. 盈利、V2、production approval、A1–A9、24H、Live不裁决不运行。理由：用户明确排除。代价：测试只证明实现/安全拒绝行为，最后RC与盈利评估未完成。
8. 全量测试分两DB批次。理由：现有guard要求Phase9用quant_phase9_test，Phase8用quant_phase8_test，不能共享单dbname。代价：报告是穷尽普通pytest集合的两个串行batch，不是单次进程；8项public Live probes按用户边界跳过。

## Deferred minors

无。
