# 开赛分钟回填（v2d3 · 2026-10-07）

## 范围

- 映射：`$ODDS_DATA_DIR/backfill/macau_mid_water_fixture_map.json`（177/177 已有 `fixture_id` + `fixture_ko`）
- 目标库：**仅副本** `match-analysis-api/data/v2d3/app.db`
- 现网 `data/app.db`：**未改**（脚本硬拒绝；`DUAL_WRITE` 仍关）
- 本包**未再打 5DF API**（映射内 `fixture_ko` 已够用）

## 口径

| 项 | 决策 |
|---|---|
| 源 | `fixture_ko`（5DF fixture 开赛，已转北京 +08:00） |
| 写入 | `matches.kickoff_at` 截到分钟；`kickoff_minute_known=1`；`kickoff_hour` 同步为新小时 |
| known 规则 | 源有明确分钟（**含确认 :00**）才标 1；本映射 177 条均有 `fixture_ko` → 全标 |
| 秒 | 非零秒截断到分钟（3 场：`:03:46`→`:03`、`:00:15`→`:00`、`:00:09`→`:00`） |
| **副本冲突** | \|新−旧\| ≥ 5min → **仍写入新值** + CSV/JSONL 标 conflict（影子底座要准 T−8h） |
| **现网冲突** | **永不覆盖** |
| predictions | 不动 |

冲突分级：`minute_skew`（5–59min）/ `hour_skew`（1–23h）/ `day_skew`（≥1d，多为竞彩日整点 vs 5DF 次日同时刻）。

## 结果

| 指标 | 值 |
|---|---|
| known=1（非 probe） | **177 / 177** |
| 仍 known=0（底座） | **0** |
| 冲突（写入并记日志） | **39**（minute 29 + hour 2 + day 8） |
| 分钟分布 | :00×145 · :30×16 · :45×11 · :10×2 · :35/:03/:59 各 1 |
| probe 行 | id 209/210 未动（本不在 177 映射） |
| 现网 | known 全 0、全整点；sha256 不变 |

## 怪字符同包扫

- 可疑乱码 / 半全角 / 异常空白：**0**
- 待审（拉丁+中文 / 品牌词 FC·SK·HD 等）：**25 行 / 12 个唯一值**（如 `TPS图尔库`、`济州SK FC`、`韩K联`）
- **未批量改名**；清单：
  - `backfill/kickoff-minute-fill-2026-10-07/reports/weird_chars_inventory.csv`
  - `.../weird_chars_inventory.md`

## 脚本 / 产物

- 脚本：`match-analysis-api/scripts/fill_kickoff_minutes.py`
- 计划日志：`backfill/kickoff-minute-fill-2026-10-07/logs/kickoff_plan_*.jsonl`
- 冲突表：`.../reports/kickoff_conflicts.csv`
- ACCEPTANCE：`.../ACCEPTANCE.json`

## 验收命令

```bash
cd api
.venv/bin/python scripts/fill_kickoff_minutes.py report --db data/v2d3/app.db
sqlite3 data/v2d3/app.db "SELECT kickoff_minute_known,COUNT(*) FROM matches WHERE match_uid NOT LIKE 'probe:%' GROUP BY 1;"
# expect: 1|177
sqlite3 data/app.db "SELECT kickoff_minute_known,COUNT(*) FROM matches GROUP BY 1;"
# expect: 0|177
sha256sum data/app.db
# expect: <sha256 已移除>
```


## day_skew 复核（2026-10-07 · 足球分析师拍板）

**不改竞彩日规则。** … 场整日 … 已逐场复核 `fixture_id`（JC 号唯一 + 队名 + 旧整点±…2h 无同队替代场）。

| 结论 | 动作 |
|---|---|
| **8/8 映射正确** | 保留 v2d3 的 5DF `kickoff_at`（known=1）；映射 JSON / `match_meta.extras_json` 标 `day_skew_confirmed=true` |
| 回滚 | **无**（非错轮） |
| `exclude_from_precise_kickoff_channels` | 确认后为 **false**（可进精确开赛通道） |
| fixture_id 变更 | **无** |
| 怪字符改名 | **不开**（清单留档） |
| minute/hour_skew 31 | 副本保留 5DF，已验收 |
| 现网 | **未动**；`DUAL_WRITE` 关；sha256=`<sha256 已移除>` |

逐场表与证据：

- `backfill/kickoff-minute-fill-2026-10-07/reports/day_skew_review.json`
- `.../day_skew_review.csv`
- `.../day_skew_review_raw.json`

原因说明：竞彩**编号日**（如 `周六008` 挂在 `jingcai_date=06-13`）与 5DF **日历开赛日**（06-14 12:00+08）差整日；JC 缓存按开赛日分文件，但 `jc` 标签仍是编号日——属口径差，不是同队不同轮错映。

## 现网导入（kickoff only · 2026-10-07）

**拍板**：一次性导入 v2d3 → 现网 `kickoff_at` + `kickoff_minute_known=1`（并同步 `kickoff_hour`）；**不开** DUAL_WRITE；**不同步**澳门水位 / mid / 1X2。

| 项 | 值 |
|---|---|
| 更新行 | **177** / 177 |
| 现网 known=1 | **177** / 177 |
| V3 哈希 | `<指纹已移除>`（期望 `<指纹已移除>`，OK） |
| OA 指纹 | `<指纹已移除>` count=1413（与导入前一致=OK） |
| predictions | V3=39 / all=263（不变） |
| odds_asian | 1413（不变） |
| DUAL_WRITE_ODDS_ASIAN | `0` |
| kickoff_hour 变更 | **3** 场（ids=[19, 91, 200]）；仅为与新 `kickoff_at` 北京小时一致，不影响 V3 |
| bak | `api/backups/kickoff-prod-import-20261007T022507+0800` |
| ACCEPTANCE | `$ODDS_DATA_DIR/backfill/kickoff-minute-fill-2026-10-07/reports/ACCEPTANCE_prod_kickoff_import.json` |

### 回滚

```bash
cd api
# 仅恢复 kickoff 三字段（推荐）
.venv/bin/python scripts/import_kickoff_minutes_prod.py rollback --bak api/backups/kickoff-prod-import-20261007T022507+0800 --mode fields
# 或整库替换
.venv/bin/python scripts/import_kickoff_minutes_prod.py rollback --bak api/backups/kickoff-prod-import-20261007T022507+0800 --mode full
```

### 验收

```bash
cd api
.venv/bin/python scripts/import_kickoff_minutes_prod.py verify
.venv/bin/python scripts/v3_hash.py data/app.db   # expect <指纹已移除>
```
