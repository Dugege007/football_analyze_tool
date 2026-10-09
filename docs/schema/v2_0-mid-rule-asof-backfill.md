# 规则中盘/临盘漏采 → hist as-of 补写

**拍板**：2026-10-09（用户）。同日再拍：approx 中盘120/临盘60；晚补分层 recommend_live_ok；far_open 默认可标采纳。  
**范围**：只写 v2d3 研究副本 `$V2D3_DB_PATH`；`DUAL_WRITE_ODDS_ASIAN` 关；不改现网 / V3 手工。

## 口径

1. 实时窗口仍是 `[T−2min, T+10min]`；超时记 `missed`。
2. **漏采后允许补**：对目标 T，调 5DF `GET /v1/fixtures/{id}/odds/history?bookmaker=&market=asian`，取  
   **赛前**（`minute IS NULL`、非 `suspended`、有 line/水位）且 **`recorded_at ≤ T`** 的**最近一次**变化。
3. 写入 `odds_snapshot`：`channel=rule`，`point=mid` 或 `close`（本批仅 mid）。  
   **禁止**用 T 之后的 `/odds` 即时盘冒充规则中盘/临盘。
4. 某书在 T 前无任何开盘 → `no_odds`，不编造。
5. `source` 列 = `5df_hist_asof`（与 `5df_live` 区分；后续 live ingest 不会覆盖该键上的 asof 行，反之 asof 也不覆盖已有 live）。

### extras（必写）

| 字段 | 值 |
|---|---|
| `source` | `5df_hist_asof` |
| `asof_backfill` | `true` |
| `planned_target_at` | 原规则目标 T |
| `observed_change_at` | 实际采用的 tick.`recorded_at` |
| `phase_assign_late` | `true`（写入发生在窗口之后） |
| `features_ok` | `true`（as-of≤T 准确则可进特征/模拟；与 catchup 不同） |
| `sim_ok` | `true`（同 features_ok：允许模拟下注与参数调整） |
| `recommend_live_ok` | 开赛后≤3h 为 `true`；>3h 晚补为 `false`（禁即时推荐消息） |
| `approx` | 中盘 age>**120**min / 临盘 age>**60**min 时 `true` |
| `approx_threshold_min` | 实际使用的阈值（120 或 60） |
| `far_open` | 过远首开可标记（常与 approx 同触发）；**默认仍采纳**，方案侧可降权 |
| `tick_age_rule` | `hist_recorded_at` |

`recorded_at` 列 = `observed_change_at`；`target_at` = 原 T；`lag_hours` = (observed−T)/3600 ≤ 0。

## 工具

- 独立脚本：`scripts/live/asof_backfill_mid_rule.py`
- 入口：`live_capture.py asof-backfill --date D (--target-key K … \| --missed-mid-rule)`
- raw 缓存：`5dollar/live/asof_backfill/raw/`
- 运行报告：`5dollar/live/asof_backfill/logs/asof_backfill_*.json`
- 庄家：CORE_BOOKS = macau / crown / william / pinnacle（hist slug：macauslot / crown / williamhill / pinnacle）
- 让路：走 `shared_api_yield`（≤16/min 补数份额；11:05–11:20 / 14:55–15:15 / 21:55–22:15 不发请求）

## 本批补写结果（竞彩日 2026-10-09）

触发：daemon 约 04:45–10:27 挂死，错过两场规则中盘。

| 目标 | fixture | T (UTC+8) | 开赛 | 状态 |
|---|---|---|---|---|
| `2026-10-09\|五001\|mid\|rule` | 1496805316 仁川联–浦项制铁 | 07:30 | 15:30 | **asof_backfilled**（4/4 书） |
| `2026-10-09\|五002\|mid\|rule` | 1833339547 柏雷索尔–神户胜利船 | 10:00 | 18:00 | **asof_backfilled**（4/4 书） |

### 各书 observed_change_at / 盘口（副本库视角，负数=主让）

**五001 mid|rule（T=07:30）**

| book | observed_change_at | tick_age_min | line | 主水 | 客水 | approx | result |
|---|---|---|---|---|---|---|---|
| macau | 2026-10-06T16:12:03+08:00 | 3797.95 | −0.25 | 0.96 | 0.82 | true | inserted |
| crown | 2026-10-09T05:40:05+08:00 | 109.92 | −0.25 | 1.00 | 0.88 | true | inserted |
| william | 2026-10-09T05:56:47+08:00 | 93.22 | −0.25 | 0.91 | 0.75 | true | inserted |
| pinnacle | 2026-10-09T06:23:26+08:00 | 66.57 | −0.25 | 1.01 | 0.89 | true | inserted |

**五002 mid|rule（T=10:00）**

| book | observed_change_at | tick_age_min | line | 主水 | 客水 | approx | result |
|---|---|---|---|---|---|---|---|
| macau | 2026-10-07T14:11:15+08:00 | 2628.75 | −0.25 | 0.90 | 0.94 | true | inserted |
| crown | 2026-10-09T01:29:58+08:00 | 510.03 | −0.25 | 0.95 | 0.93 | true | inserted |
| william | 2026-10-07T20:51:12+08:00 | 2228.80 | −0.25 | 0.80 | 0.91 | true | inserted |
| pinnacle | 2026-10-09T06:05:19+08:00 | 234.68 | −0.25 | 0.93 | 0.97 | true | inserted |

说明：当时补写用的是旧统一 30min approx 阈值。**2026-10-09 再拍后**中盘改为 120min、临盘 60min；上表五001/五002 各书 age 仍多数会标 approx（且可标 `far_open`），默认采纳。五002 平博在 T 后另有 1 条 tick，已正确排除。

### 库 / 台账 / 备份

- 副本：`matches.id` 217 / 218；各 4 行 `odds_snapshot`（asian / rule / mid / `5df_hist_asof`）
- state：`captured_2026-10-09.json` 两目标 `status=asof_backfilled`，`asof_backfill=true`
- 写前备份：`5dollar/live/backups/v2d3_app_20261009T104140_asof.db`
- 运行报告：`5dollar/live/asof_backfill/logs/asof_backfill_2026-10-09_20261009T104206.json`
- API：8 次 hist，remaining≈38（未撞让路窗）

### features / 推荐分层

- `features_ok=true` + `sim_ok=true`，`phase_assign_late=true`：准确 as-of 可进模拟与调参；台账仍标 late。
- `recommend_live_ok`：开赛后 >3h 晚补为 false（不发即时预测/下注推荐）；非「features 全禁」。
- 与 catchup（开赛提前空档、发现时刻即时盘）不同，勿混用。

## daemon 还要不要改？

- **热路径**：`mark_missed` 记状态，并 **R-C 幂等入队** `5dollar/live/rescue_queue/pending/`（不调 API）。
- **已改**：
  - `scripts/asof_backfill_mid_rule.py`（Strict as-of 执行器 = 规则卡 R-A）
  - `scripts/rescue_queue.py` + `mark_missed` 入队
  - `scripts/live_capture.py`：`asof-backfill` 子命令；missed→rescue_queue
  - 权威多规则卡：`schema/v2_0-live-miss-rescue-rules.md`
  - `5dollar/live/README.md` 同步
- **已做（2026-10-09）**：`rescue_gap_scan.py`（多日缺口）+ `rescue_worker.py`（空闲窗消费 asof/shadow）；见 `v2_0-live-miss-rescue-rules.md` §7。

## 与旧路径对照

| 路径 | 数据时刻 | 入副本？ |
|---|---|---|
| 窗口内 tick | ≈T | 是（`5df_live`） |
| `capture --allow-late` | 现在 | 否（staging only） |
| **asof-backfill** | hist ≤T 最近变化 | 是（`5df_hist_asof`） |
