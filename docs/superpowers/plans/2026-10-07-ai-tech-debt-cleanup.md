# AI 技术债清理执行计划

> 执行方式：本会话由主执行者逐批实现；独立子任务只读审计，最后独立审查。

**Goal:** 在保护数据、迁移和现行安全/策略语义的前提下删除已证明 DEAD 内容，收敛重复兼容处理，归档历史报告，交付六份报告。

**Architecture:** 保留现有 Phase/module 路径，import 图先于删除；Nautilus 冻结，Freqtrade 未实现。依据用户附件的完整清理规范（本次引用聊天的粘贴文本）。

**Tech Stack:** Python 3.12、pytest、PostgreSQL、React/Vite、WSL2；使用现有环境，仅本项目操作。

## 约束与审查重点

- UNKNOWN 不删；迁移、.env、API Key、真实历史行情、数据库、Docker volume 全部保护。
- 禁止 Live、force push、reset --hard、git clean、删其它分支/tag、改远程历史。
- 不变更 threshold/RR/A-B-C/数据单位/新鲜度策略，不新增 Freqtrade。
- 完整测试包含真实暂不可用依赖时明确跳过/失败；不制造验收证据、不重签 frozen manifest。
- Markdown 搬迁修引用；保留 formal acceptance 历史引用路径和既有保护文件。

## 任务

- [x] Audit：全仓统计、AST 与 CLI/Compose 引用、配置/owners审计；记录基线。
- [x] Batch 1：删四个无引用死符号，复测 pipeline/normalization/providers/realtime runtime。
- [x] Batch 2：三份 news_mode 兼容 validator 使用 contracts 同一 helper；保留 Record extra=forbid，复测全部 strategies。
- [x] Batch 3：删除只针对 2026-09-25 的无人调用 phase7_acceptance_secret_audit，保留正式安全能力；测试 phase7 safety 与正式扫描。
- [x] Batch 4：收敛测试数据库可选依赖错误为显式 skip；保留所有实际断言，未提供 TEST_POSTGRES_DSN 时不连接库；复测相关 fixtures。
- [x] Batch 5：小批次归档根目录历史 reports，保留有 formal/path 依赖的文档；逐批查 Markdown 链接与相关测试。
- [x] Batch 6：去除 Dashboard 脚本开发者路径默认，显式要求 RepoPath/PythonPath；更新操作说明，解析并检查参数。
- [x] Final：更新六份报告、基线/之后统计、删除证据、模块/owner/Paper/后续接入文件、复跑最大可运行测试及安全 smoke；独立 review，正常 push 指定分支，停下。

## 执行验证记录

源码回归：2176 passed / 220 skipped / 0 failed / 0 errors，前端17 passed及类型检查退出0。220 skipped分类为211项数据库依赖、8项未opt-in公开API探测、1项原Git历史证据不可用；均未伪造验证通过。44份历史文档分五批归档。最终报告统计：828个文件、582个Python文件、124751行、268个test文件；保护路径与基线逐字节一致。独立终审完成：无Critical/Important；报告与保护路径核对通过。最后发布步骤为本提交后正常push指定cleanup分支，再核对远程HEAD并保存到交付包VERSION.json；不merge、不启动运行服务。
