# 历史数据核验对照表（2026-10-05）

目标：补数为主、核验为辅；不编造竞彩/支持率；发现冲突记差异不自动覆盖主源。

## 1. 字段对照

| 字段组 | 主源（落库） | 核验源 | 通过标准 | 失败处理 |
|---|---|---|---|---|
| 赛程/队名/开赛时间 | API-Football（近窗）或 football-data CSV（历史） | football-data.org / 5Dollar fixtures | 同联赛同日队名归一后能唯一匹配；开赛差 ≤15min | 记 `match_ Ambiguity`，人工/下一轮再配 |
| 赛果比分 | football-data CSV / 5Dollar finished | API-Football（仅今天±1） | 全场比分一致 | 以完赛官方源为准；冲突进 `score_conflict` |
| 亚盘（澳门/皇冠） | InferSports batch（有报价时） | BSD macauslot（若非合成）/ 人工 Excel 日志 | 让球线同、水位差 ≤0.05 | Infer stale 时标 `stale`，不算核验通过 |
| 亚盘（Bet365） | 5Dollar odds | OddsPapi 抽样 / football-data B365 列（若有） | 线同、水位差 ≤0.05 | 缺一侧则只存单源 |
| 平博/威廉等欧赔 | The Odds 现盘；历史用 OddsPapi historical 抽样 | football-data WH/PS 列 | 抽样场次方向一致 | 免费无深历史则跳过 |
| 竞彩固定奖金/让球 | sporttery（仅大陆出口当日抓） | — | 当日 had/hhad 非空 | 历史 null；不造数 |
| 支持率 | `support_proxy_odds`（欧赔共识） | 竞彩 vote（常空） | 仅作代理字段，不称真实支持率 | Betfair 已弃用 |

## 2. 队名/联赛归一（最小规则）

- 先 `league_id`/`competition` 对齐，再模糊队名（去 FC/United 后缀、大小写）。
- 一对多匹配 → 不合并，进待审列表。

## 3. 时间窗（与方法论对齐）

- 历史核验：只比「同一场」终盘或 CSV 收盘列，不拿现盘打旧赛。
- 将来赛前调度：初盘~11:10、中盘开赛前 8h、临盘开赛前 1h（另建 routine，不占用补数额度）。

## 4. 输出位置

- 差异日志：`$ODDS_DATA_DIR/backfill/logs/verify-YYYYMMDD.md`
- 合并主表：`$ODDS_DATA_DIR/backfill/merged/`（冲突行保留 `*_src` 与 `conflict` 标记）
