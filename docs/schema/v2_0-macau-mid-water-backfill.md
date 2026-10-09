# 澳门亚盘中盘水位回填（2026-10-07）

> 目标：解开 `SHADOW_S1` 的数据阻塞（澳门无 mid、水位全 NULL）。  
> **只写副本** `data/macau_water_fill/app.db`；不碰现网 / v2d1 / v2d2；`DUAL_WRITE_ODDS_ASIAN` 仍关。

## 1. 缺口（回填前 · 现网拷贝）

| 库 | macau open | macau mid | macau close | open/close 水位 |
|---|---|---|---|---|
| `app.db` / 副本起点 | 177 行有盘口 | **0** | 177 行有盘口 | **全 NULL** |
| crown / william | open/mid/close 齐 | 有 | 有 | `water_src=tier_midpoint` |

S1 规则卡（`prediction-landing-cards.md（未公开）`）：严格口径要澳门 AH **三段**真实水位；此前状态 = **blocked**。

## 2. 源与映射

- API：5DollarFootballAPI ****（`FIVEDOLLAR_FOOTBALL_API_KEY`）
- 竞彩映射：`GET /v1/chinasportslottery?types=jingcailottery&lang=zh-cn`
  - 时间窗：**恰 24h**（竞彩日 12:00 BJT → +1 日 12:00 BJT；超窗 → `invalid_time_window`）
  - `lottery.jingcailottery.number`（如 `周六201`）↔ 库内 `jc_id`（`六201`）：去掉「周」字归一
- 水位历史：`GET /v1/fixtures/{id}/odds/history?bookmaker=macauslot&market=asian&per_page=500`
- 水位换算：欧式小数 → 港盘 `hk = decimal − 1`；`water_src=actual`；`extras.source=5df_macauslot_history`
- InferSports：feed 已 stale（2026-10-02+），**不用于**本回填
- API-Football：已停用，不用

## 3. 中盘 / 初盘 / 临盘取点

沿用 `app.collection_schedule.channel_targets` 的 **rule** 通道：

| phase | 目标时刻 | tick 选取 |
|---|---|---|
| open | 首条赛前 tick | `minute IS null` 且 `recorded_at < kickoff` 的第一条 |
| mid | 开赛 hour∈{0–10,23} → **竞彩日 15:00**；否则 **T−8h** | last tick ≤ target |
| close | 对应 rule close（15:00 批 → 22:00；否则 T−1h） | last tick ≤ target（无则末条赛前） |

- tick 早于 target **>30min** → `extras.approx=true` + `tick_age_hours`（澳门历史常稀疏，预期大量 approx）。
- **只 UPDATE 水位为 NULL 的 open/close**；永不覆盖非空。
- **INSERT** `phase=mid`（已有且水位非空则跳过）。
- 盘口 `handicap`：mid 新行写 tick.line；open/close **不改**既有 handicap（只补水）。

## 4. 脚本 / 产物

| 路径 | 说明 |
|---|---|
| `scripts/backfill/backfill_macau_mid_water.py` | 回填脚本（默认写 `macau_water_fill`） |
| `api/data/macau_water_fill/app.db` | 副本库 |
| `$ODDS_DATA_DIR/5dollar/macau-mid-backfill-2026-10-07/raw/csl/` | CSL 日缓存 |
| `$ODDS_DATA_DIR/5dollar/macau-mid-backfill-2026-10-07/raw/hist/` | macauslot asian history |
| `$ODDS_DATA_DIR/5dollar/macau-mid-backfill-2026-10-07/logs/fill_report.json` | 计数报告 |
| `$ODDS_DATA_DIR/5dollar/macau-mid-backfill-2026-10-07/logs/call_log.tsv` | API 调用日志 |

```bash
# 副本全量
python3 scripts/backfill/backfill_macau_mid_water.py

# 演练
python3 scripts/backfill/backfill_macau_mid_water.py --dry-run --limit 5

# 仅用已缓存 raw（不打 API）
python3 scripts/backfill/backfill_macau_mid_water.py --skip-fetch

# 现网（默认拒绝；需双写开 + 显式旗）
# DUAL_WRITE_ODDS_ASIAN=1 python3 ... --db data/app.db --i-know-this-is-production
```

速率：默认 gap 2.1s（≤30/min）；额度 （账户上限）；`remaining≤5` 停。

## 5. S1 解锁条件

数据侧：澳门 open/mid/close 均有真实水位后，才可按规则卡挂 `SHADOW_S1`（fingerprint 切回 `feature_water_book=macau`）。  
规则侧仍要：三段盘口不动 ∩ 上盘水位始终 ∈[…]；样本门槛见 mutex L0/L1。  
**本回填只解数据阻塞，不自动 seed predictions。**

## 6. 与后端并行约定

后端若同时在 v2d2 补水：本脚本**只**写 `macau_water_fill`，避免互踩。合并进现网前对拍 `fill_report.json` 与抽检行。
