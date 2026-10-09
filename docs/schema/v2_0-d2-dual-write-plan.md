# v2.0 D2 · 双写 / 兼容视图方案（只设计，不执行）

> **状态：§5.1 已拍板；副本演练进行中。现网双写仍默认关。**  
> 前置：D1 已验收（副本 `data/v2d1/app.db` + timeline/snapshot；现网 API 0.3.9+ dispatch 新 rule）。  
> 验收锚点：V3 compare 哈希 `<指纹已移除>` 与默认数字（… / MIRROR … / stack …）与 **….8/….9** 一致。

## 1. 目标

1. 用 `odds_snapshot`（默认 `channel=rule`，`market=asian`，`point∈{open,mid,close}`）生成与现网引擎可读的 **兼容投影**（VIEW `v_odds_asian_rule` 已在 v2.0 DDL 草案中）。  
2. 可选 **双写**：从 snapshot 同步写入物理表 `odds_asian`（过渡期），使 0.3.x 结算路径零改动。  
3. 回归：现网（或指定副本）上 `CFFXDJ_5_V3` validate/compare 指纹与盈亏数字不变。

## 2. 范围与非目标

| 做 | 不做 |
|---|---|
| asian 市场 open/mid/close → `odds_asian` phase 映射 | **覆盖/改写** 旧 177 场手工 mid/close 的盘口水位语义 |
| 默认通道 `rule`（日用 15:00/22:00 等） | 用新 rule 钟点 **回写** 已标为 `rule_legacy` 的旧快照 |
| 副本先跑通再议现网 | 本包落影子 `strategy_defs` / 改 V3 定义或预测 |
| 文档化 phase↔point、water_src 传递 | 立刻上 euro / jc_hhad / ou 全市场双写（可二期） |
| 保持 macau_close 固定 0.95 结算默认 | 把 actual/t8/t1 设为 V3 默认 settle |

## 3. 建议步骤（执行时按序；现不下刀）

1. **副本演练**  
   - 在 `data/v2d1/app.db`（或新建 `data/v2d2/`）上：从 D1 snapshot（及日后正式 history）填充/校验 `v_odds_asian_rule`。  
   - 脚本：`scripts/sync_odds_asian_from_snapshot.py`（拟）：`channel=rule` → UPSERT `odds_asian`，`source`/`extras` 标明 `dual_write_d2`；**禁止** DELETE 旧手工行 unless 显式 `--replace-probe-only`。  
2. **phase 映射拍板后固化**（见 §5）  
   - 建议草案：`open→open`，`mid→mid`，`close→close`；`t8/t1` **不**写入 `odds_asian`（只留 snapshot / 对照）。  
3. **水位**  
   - snapshot 已是真实/中点水位 → `odds_asian.home_water/away_water` 原样；`water_src`/`water_censored`/`extras_json.water_tier_raw` 能带则带。  
4. **回归**  
   - 指向副本或临时 `DATABASE_URL`：跑 V3 + MIRROR compare；断言哈希与 0.3.8 基线 JSON 一致。  
5. **现网开关（仅分析师批准后）**  
   - 环境变量或 config：`ODDS_ASIAN_DUAL_WRITE=1`；默认关。  
   - 或「只建 VIEW、结算改读 VIEW」作为更干净的替代（需改 `_load_strategy_bets` SQL，回归同左）。

## 4. 风险

| 风险 | 缓解 |
|---|---|
| 双写覆盖旧手工 mid/close | 默认 **INSERT OR IGNORE** / 仅补空 phase；现网 177 场禁止 UPDATE 水位 |
| rule vs rule_legacy 混用 | 双写只读 `channel=rule`；legacy 仅对照列；旧数据导入标 `rule_legacy` 不进日用 VIEW |
| 探针场污染正式 177 | 探针 `match_uid=probe:*` / `extras.source=probe_d1` 永不 sync 进现网 |
| VIEW 与物理表并存导致读错库 | 文档写清「过渡读物理表；稳定后切 VIEW」；指纹含 `data_rev` |
| euro/jc 半套同步 | D2 首期 **只 asian**；其它市场另开里程碑 |
| 结算版本误 bump | D2 **不改** `settlement_version`；仅数据供给路径变化且数字不变才算过 |

## 5. 需要分析师拍板

1. **立刻双写现网 vs 先只副本？**（建议：先副本 + VIEW 验收，现网默认关。）  
2. **phase 映射**：`t8/t1` 是否映射为 mid/close 别名写入 `odds_asian`，还是仅 snapshot？  
3. **空缺策略**：某 book 缺 rule.mid 时，是否回落 actual.t8 / 旧手工 / 跳过？  
4. **是否同步 euro_home / jc_*？**（建议 D2 不做。）  
5. **过渡读路径**：继续写+读物理 `odds_asian`，还是结算改读 `v_odds_asian_rule`？  
6. **旧 177 场**：是否批量打标 `channel=rule_legacy` 进 snapshot（只读迁移），与双写分开？

### 5.1 拍板结论（分析师，2026-10-06）

| # | 结论 |
|---|---|
| 1 | **先副本 + VIEW 验收**；现网双写默认 **关**，过验收再开。 |
| 2 | **t8/t1 只留 snapshot**，不映射进 `odds_asian`；VIEW 对照也只投影 `open/mid/close`。 |
| 3 | 缺 `rule.mid`（或缺任一名点）：**跳过该 phase**，不回落 actual/旧手工；缺口记日志。 |
| 4 | D2 **只做 asian**；euro/jc 不做。 |
| 5 | 过渡期 **继续写+读物理 `odds_asian`**；VIEW 建好对照；结算改读 VIEW **另议**。 |
| 6 | 旧 177：**只读**迁移进 `odds_snapshot`（`channel=rule_legacy`），与双写分开；**禁止 UPDATE** 旧 mid/close 水位/盘口。 |

同步策略（执行口径）：对物理表 **仅 INSERT 缺失的 (match_id,book,phase)**；已存在旧行一律 SKIP，永不 UPDATE。新行 `extras_json.source=v2_dual_write`。

演练笔记：`v2_0-d2-rehearsal-notes.md`；脚本：`scripts/sync_odds_asian_from_snapshot.py`。

## 6. 回退

- 关双写开关；VIEW 可留。  
- 若误写现网：用 `backups/` 中 D2 执行前的 `app.db` 恢复；或按 `extras_json`/`import_batches` 回滚双写批次。  
- 代码回退：还原 sync 脚本与 SQL 读路径；API 版本不必为 D2 单独 bump 除非改了读路径。

## 7. 验收清单（执行阶段）

- [ ] V3 哈希 `<指纹已移除>`  
- [ ] compare 默认：V3 …，MIRROR …，stack … / MDD≈…  
- [ ] 现网 `odds_asian` 行数/指纹与执行前基线一致（若未批准写现网）或仅新增探针/空 phase  
- [ ] `rule_legacy` 行未覆盖正式 mid/close  

---

**结论**：§5.1 已拍板。副本演练见 `v2_0-d2-rehearsal-notes.md`；现网 `DUAL_WRITE_ODDS_ASIAN` 默认 0。
