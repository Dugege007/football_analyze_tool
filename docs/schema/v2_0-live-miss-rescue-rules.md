# 实时漏点补救规则（live miss rescue）

**拍板**：2026-10-09（用户：过点后 as-of≤T 最近变化；可多套规则并存）。
**同日再拍**：approx 中盘 120min / 临盘 60min；开赛后 3h 只关即时推荐、准确 as-of 仍进模拟调参；过远首开默认可标采纳。  
**范围**：只写 v2d3 研究副本 `$V2D3_DB_PATH`；`DUAL_WRITE_ODDS_ASIAN` 关；不改现网 / V3 手工。  
**相关**：[`v2_0-mid-rule-asof-backfill.md`](./v2_0-mid-rule-asof-backfill.md)（已实现的 Strict as-of 执行器）、[`../5dollar/live/README.md`](../5dollar/live/README.md)、`shared_api_yield` 让路窗。

---

## 0. 铁律（所有规则共用）

1. **禁止**用目标 T **之后**的 `/odds` 即时盘冒充规则 mid/close（或真实 mid/close）主值。
2. 补写取值必须满足：赛前（`minute IS NULL`）、非 `suspended`、有 line/水位、且 **`recorded_at ≤ T`**（Strict）；Near-window 仅在 Strict 无 tick 时按条件放宽，且不得越过 T。
3. 某书在可用窗口内无任何开盘 → `no_odds`，不编造。
4. 已有 `source=5df_live` 的同键行 → **不覆盖**；已有非 live/非 asof 手工行 → **不覆盖**。
5. 补数走 `shared_api_yield`（≤16/min）；**让路窗内不发 hist**：11:05–11:20、14:55–15:15、21:55–22:15（北京时间）。
6. 与 **catchup**（开赛提前空档、发现时刻即时盘、`features_ok` 常 false）严格区分：catchup 不是 as-of 补救。

---

## 1. 意外场景矩阵

| # | 场景 | 典型信号 | 优先规则 | 备注 |
|---|---|---|---|---|
| S1 | 进程挂死 / 心跳停 | `heartbeat.json` 过期、daemon.pid 无进程 | R-C → R-A | ensure-daemon 拉起后扫描 missed 入队 |
| S2 | box 重启 | 冷启动后 ensure-daemon 晚于若干 T+10 | R-C → R-A | 同 S1；积压按优先级排序 |
| S3 | ensure-daemon 晚于 T+10 | 看门狗 :11 才发现 | R-C → R-A | 不在热路径抢抓即时盘 |
| S4 | 5DF 鉴权失败 | 401/403 | R-C 挂起 + 告警 | 不重试刷爆；人工修钥后再消费 |
| S5 | 配额耗尽 / remaining 触底 | remaining≤24 或 429 | R-C 延期 | 避开让路窗与 due 邻近 |
| S6 | 短暂 5xx / 超时 | HTTP 5xx、urlopen timeout | R-C 有限重试 | 指数退避；上限见 §2.3 |
| S7 | `no_replica_match` | 赛程未进副本 | R-E | 赛程入库后再 R-A；超时 → R-D |
| S8 | 占位开赛 12:00 | `kickoff_placeholder_suspect` | 既有 pending 通道 | 不走本补救主路径；确认后归阶段 |
| S9 | 开赛变更 / 提前空档 | `kickoff_rev+1`、`catchup_on_discovery` | **既有 catchup** | 与 as-of 补救并行但不混写 |
| S10 | 目标时刻无任何 tick | hist 空或全在 T 后 | R-A → `no_odds`；可试 R-B | R-B 仍无 → 结案 no_odds |
| S11 | tick 距 T 很远 | age > approx 阈值（**中盘 120min / 临盘 60min**） | R-A 仍写入 | `approx=true` + `far_open` 可标；默认采纳，方案可降权 |
| S12 | 规则通道漏采 | `mid\|rule` / `close\|rule` missed | R-C + R-A | 默认主路径 |
| S13 | 真实通道漏采 | `mid\|real` / `close\|real` missed | R-F（同 as-of，标签分离） | 分析默认仍用规则 |
| S14 | 漏点在开赛后 / 赛后 | `now ≥ kickoff+3h` 才轮到补 | R-D（晚补） | **仍 as-of 写入规则格**；`recommend_live_ok=false`；`sim_ok/features_ok=true`（准确时） |
| S15 | 多场同时 missed 积压 | 宕机数小时 | R-C 排队 | 按 kickoff 近→远、mid 先于 close |
| S16 | 与 11:10 抢配额 | 11:00–11:20 前后 | **禁止消费队列** | 让路窗已覆盖 11:05–11:20；队列额外禁 11:00–11:20 |
| S17 | 心跳停数小时～数天 / daemon 全停 | heartbeat 过期、pid 无进程、多日 plan 无 state | **gap_scan → R-C** | 见 §7；恢复后先扫再消费 |
| S18 | 赛程日后才发现漏采 | 新 plan / 人工核对 | gap_scan（日期范围） | 开赛后 >3h → R-D 晚补（关即时推荐，仍可模拟） |
| S19 | 全日或多日整批缺口 | 回溯 N 天应采未采 | gap_scan 分批入队 + worker 配额 | 默认 N=7；避开 due/让路窗 |

---

## 2. 多套补救规则（可并存）

优先级总览（同一目标只选一条「写入主通道」的执行规则；排队规则可叠加）：

```
入队(R-C) → 等待条件 → 执行:
  no_replica_match              → R-E
  规则 mid/close + 开赛后≤3h    → R-A
  规则 mid/close + 开赛后>3h    → R-D（同 as-of 写入；recommend_live_ok=false）
  真实 mid/close                → R-F（内部仍用 R-A/R-B 算法）
```

### 2.1 R-A Strict as-of（默认执行器）

| 项 | 内容 |
|---|---|
| **触发** | 队列消费或手动 `asof-backfill`；目标为规则 `mid\|rule` / `close\|rule`（或 R-F 委托） |
| **取值** | hist 赛前 tick 中 `recorded_at ≤ T` 的**最近一次** |
| **写入通道** | `odds_snapshot`：`channel=rule`，`point=mid|close`；`source=5df_hist_asof` |
| **extras** | `asof_backfill=true`，`planned_target_at=T`，`observed_change_at`，`phase_assign_late=true`，`features_ok=true`，`sim_ok=true`，`recommend_live_ok=true`，`approx`（中盘 age>120min / 临盘 age>60min），`approx_threshold_min`，`far_open`（可标），`tick_age_rule=hist_recorded_at`，`rescue_rule=R-A` |
| **features / 推荐** | `features_ok=sim_ok=true`（可进模拟与调参）；`recommend_live_ok=true`；台账仍标 late |
| **与 catchup** | 无关；catchup 用发现时刻即时盘且常 `features_ok=false` |
| **禁止** | 采用 `recorded_at > T`；覆盖 `5df_live` / 手工；DUAL_WRITE |
| **无 tick** | `no_odds`（可升级试 R-B） |

实现：`scripts/asof_backfill_mid_rule.py` / `live_capture.py asof-backfill`。

### 2.2 R-B Near-window（Strict 无 tick 时的窄窗放宽）

| 项 | 内容 |
|---|---|
| **触发** | R-A 对该书返回无 tick；且目标仍赛前、未升级 R-D |
| **取值** | 仅当 Strict 为空时：仍要求 `recorded_at ≤ T`。**不把 T 后 tick 拉进来。** 可选策略 B1：若 T 前仅有「首开」且距 T 很远，仍采纳并强制 `approx=true`（与 R-A 对远 tick 一致）。可选策略 B2：要求 `T−W ≤ recorded_at ≤ T`（建议 W=6h）；窗外则失败 → `no_odds` |
| **默认工程拍板** | **启用 B1（远 tick 可写+approx/far_open，默认采纳，方案可降权）**；**不启用 B2 硬拒**（用户 2026-10-09：可标记、默认采纳） |
| **写入** | 同 R-A；`rescue_rule=R-B`（若走了 B2 拒绝则不写） |
| **features / 推荐** | 同 R-A（`features_ok/sim_ok=true` + approx/far_open 可标） |
| **禁止** | 任何 `recorded_at > T`；把「T 后首变」当中盘 |

### 2.3 R-C Deferred queue（排队与让路）

| 项 | 内容 |
|---|---|
| **触发** | `mark_missed` / ensure-daemon 恢复 / 手动扫描到 `status=missed` 的 mid\|rule、close\|rule（及可选 real） |
| **动作** | 写入 `5dollar/live/rescue_queue/pending/*.json`，**本步不调 API** |
| **消费窗** | 非让路窗；额外禁止 11:00–11:20；避开「下一 due 目标前 15min」若会与 live 抢配额 |
| **重试** | 网络/5xx：最多 5 次，退避 1/2/5/10/20 min；鉴权失败：不自动重试，标 `blocked_auth` |
| **过期** | 开赛后 **N=3h** 未消费成功 → 升级 **R-D 晚补**（仍 as-of 写规则格，仅关即时推荐）；赛后 24h 仍失败 → `failed` 结案 |
| **写入** | 不直接写库；成功消费后由 R-A/R-F/R-D 写 |
| **extras / features** | 队列条目带 `enqueued_at`、`reason`、`priority`、`attempts` |
| **禁止** | 在让路窗或 11:10 高峰发 hist；热路径同步 asof |

### 2.4 R-D Late recommend-off（开赛后 >3h 晚补）

> **2026-10-09 语义纠正**：不是「shadow_only → features 全禁」。准确 as-of≤T 数据**仍写规则 mid/close**，可进方案**模拟下注与参数调整**；限制的是**不得据此发即时预测 / 下注推荐消息**。

| 项 | 内容 |
|---|---|
| **触发** | 开赛后 **>3h** 才轮到补；或 R-E 超时后仍有准确 hist；或人工标晚补 |
| **取值** | 与 R-A 相同：as-of≤T；**不得**用赛后盘 / T 后即时盘 |
| **写入通道** | **仍写入** `odds_snapshot` 规则格（`channel=rule`，`point=mid|close`，`source=5df_hist_asof`）；另在 `rescue_queue/shadow/` 落晚补索引报告（对照标签，非「不入库」） |
| **extras** | `rescue_rule=R-D`，`recommend_live_ok=false`，`sim_ok=true`，`features_ok=true`（as-of 准确时），`phase_assign_late=true`，`approx` / `approx_threshold_min` / `far_open` 同 R-A |
| **分层** | `recommend_live_ok=false` → 禁日用即时推荐消息；`sim_ok=true` / `features_ok=true` → 允许模拟与调参；仅数据不可信（无 tick、编造、T 后盘）才 `features_ok=false` |
| **禁止** | 用 T 后即时盘；对 `recommend_live_ok=false` 的场发即时下注推荐 |

### 2.5 R-E Match-wait（等赛程）

| 项 | 内容 |
|---|---|
| **触发** | asof/ingest 报 `no_replica_match` |
| **动作** | 队列状态 `waiting_match`；每轮 plan/ingest 后重试定位副本 `matches` |
| **超时** | 默认开赛前一直等；开赛后 3h 仍无副本 → 升级 R-D 晚补路径（有 hist 则仍可 asof） |
| **成功后** | 转 R-A（或 R-F） |
| **禁止** | 默认自动 `--create-missing-matches`（需用户批准） |

### 2.6 R-F Real-channel（真实 mid/close）

| 项 | 内容 |
|---|---|
| **触发** | `mid\|real` / `close\|real` missed（例外场 actual/t8、t1） |
| **取值** | 与 R-A 相同算法，T = 真实目标（开赛−8h / −1h） |
| **写入** | `channel=actual`，`point=t8\|t1`；`source=5df_hist_asof`；`rescue_rule=R-F` |
| **features** | 可进「真实通道」特征；**分析默认仍用规则通道** |
| **禁止** | 写入 rule 通道；与规则格混键 |

---

## 3. 默认策略（工程拍板）

| 决策 | 默认 | 待用户确认？ |
|---|---|---|
| 规则 mid/close 漏点 | **R-C 入队 + 空闲窗 R-A 执行** | 否（用户已要补救） |
| Strict 无 tick | 先 `no_odds`；远首开按 R-B/B1 写入+标记 | B2 硬拒默认关（用户：可标、默认采纳） |
| approx 阈值 | **中盘 120min / 临盘 60min**；仍写入，`approx=true` + `approx_threshold_min`；`far_open` 可标 | 已拍板 |
| 过远首开 | **默认采纳**；标 `approx`/`far_open`；方案侧可降权或按算法决定是否采纳 | 已拍板 |
| 开赛后 >3h 补 | **R-D 晚补**：仍写规则主值；`recommend_live_ok=false`；`sim_ok/features_ok=true`（准确时） | 已拍板（语义纠正） |
| 真实通道 | 同逻辑独立 R-F；默认分析不用 | 否 |
| 11:10 / 让路窗 | **禁止消费 rescue 队列** | 否 |
| 积压排序 | ① 开赛更近优先 ② 同场 mid 先于 close ③ 规则先于真实 | 否 |
| 自动消费 worker | **已有** `scripts/rescue_worker.py`（空闲窗消费；不堵 live due） | 否 |
| 多日缺口扫描 | **已有** `scripts/rescue_gap_scan.py`；默认回溯 **7 天** | N 天可调（偏好） |
| 积压（多日）排序 | ① 即时推荐仍可用（赛前/开赛后≤3h）② kickoff 近→远 ③ mid→close ④ 竞彩日旧→新 | 否 |
| 每日消费上限 | 默认 **40** 条成功/late/no_odds；单轮默认 **8** | 可调（偏好） |
| catchup 与 rescue | 并存、不互斥；同目标已 catchup 成功则不必再 R-A | 否 |

### 3.1 决策树（简）

```
missed 出现
  ├─ channel 为 pending / 已是 catchup 行 → 不入 rescue（走既有逻辑）
  ├─ 写入 rescue_queue (R-C)
  └─ worker/手动消费时:
       ├─ 无副本场 → R-E
       ├─ phase_variant=real → R-F → (R-A 算法)
       ├─ now ≥ kickoff + 3h → R-D（asof 写入 + recommend_live_ok=false）
       └─ 否则 → R-A → (无 tick 且启用 B2 则 R-B) → no_odds / asof_backfilled
```

---

## 4. 接入草案

### 4.1 队列文件格式

目录：`$ODDS_DATA_DIR/5dollar/live/rescue_queue/`

| 子目录 | 含义 |
|---|---|
| `pending/` | 待消费 |
| `done/` | 已成功（含 asof_backfilled / no_odds 结案） |
| `failed/` | 超重试或永久失败 |
| `shadow/` | R-D 对照产物索引 |
| `logs/` | 消费日志 jsonl |

单条文件名：`{jingcai_date}__{short_code}__{point}__{variant}__{hash8}.json`  
（`variant` = `rule`|`real`；`hash8` = target_key 短哈希防撞。）

```json
{
  "schema": "live_rescue_queue_v1",
  "target_key": "2026-10-09|五001|mid|rule",
  "jingcai_date": "2026-10-09",
  "match_uid": "2026-10-09|五001",
  "fixture_id": 1496805316,
  "channel": "rule",
  "point": "mid",
  "phase_variant": "rule",
  "target_at": "2026-10-09T07:30:00+08:00",
  "kickoff_at": "2026-10-09T15:30:00+08:00",
  "enqueued_at": "2026-10-09T10:30:00+08:00",
  "reason": "missed_after_window",
  "scenario": "S1",
  "priority": 100,
  "attempts": 0,
  "max_attempts": 5,
  "status": "pending",
  "rescue_plan": ["R-C", "R-A"],
  "expires_mode": "kickoff_plus_3h_recommend_live_off",
  "notes": ""
}
```

`priority` 建议：`int(1000 - hours_to_kickoff*10) + (10 if point==mid else 0) + (5 if channel==rule else 0)`（开赛越近越大）。

### 4.2 健康日志字段

在既有 `logs/health.jsonl` / heartbeat 上扩展（消费端写入）：

| event / 字段 | 含义 |
|---|---|
| `rescue_enqueued` | missed → 入队 |
| `rescued` | R-A/R-F 成功写入 |
| `rescue_failed` | 永久失败或 no_odds 结案失败语义 |
| `rescue_deferred` | 让路窗/配额/鉴权 → 延期 |
| `rescue_late` / `rescue_shadow` | 走了 R-D 晚补（已写规则格；关即时推荐） |
| `rescue_waiting_match` | R-E |

heartbeat 可选计数：`rescue_pending`、`rescue_done_today`。

### 4.3 接线状态（本批）

| 项 | 状态 |
|---|---|
| 口径卡本文 | **已落盘**（含 §7 多日缺口） |
| 队列目录 + schema 样例 | **已落盘** `5dollar/live/rescue_queue/` |
| `mark_missed` → 入队（R-C） | **已接线**（只写 pending，不调 API） |
| 多日缺口扫描 gap_scan | **已做** `scripts/rescue_gap_scan.py` → pending |
| 自动 worker 消费 → asof | **已做** `scripts/rescue_worker.py`（让路窗/due 避让/日上限） |
| R-A 执行器 | **已有** `asof_backfill_mid_rule.py` |
| R-D 晚补 | **worker 已接线**（仍 asof 写规则格；`recommend_live_ok=false`；shadow/ 留索引） |
| R-E / R-F 执行器 | R-E：worker 遇 `no_replica_match` 标 waiting_match；R-F：**暂 defer**（真实通道留 pending） |
| 改现网 / DUAL_WRITE | **不做** |

### 4.4 已实现 / 仍待

**已实现（2026-10-09）**

1. `rescue_gap_scan.py`：回溯 N 天（默认 7）扫应采未采 → 幂等入队。  
2. `rescue_worker.py`：空闲窗消费 pending → R-A / R-D 晚补 asof；遵守让路窗、11:00–11:20、due 前 15min、日上限。  
3. `rescue_queue.py`：入队 + 消费辅助（排序、禁区、move）。  
4. 恢复流程：`ensure-daemon` 后先 `gap-scan` 再 `rescue-consume`（见 live README；**不**在 tick 热路径跑）。

**仍待**

1. R-F 真实通道 asof 执行器（worker 现 defer）。  
2. R-E：ingest 建场后自动唤醒 `waiting_match`。  
3. `asof_backfill` 一键 `--missed-close-rule` / 读队列文件（worker 已按 target_key 调）。  
4. 单测补强：入队幂等、让路窗、开赛后 3h → recommend_live_off。

---

## 5. 与现有路径对照

| 路径 | 数据时刻 | 入副本规则格？ | features/sim | recommend_live_ok |
|---|---|---|---|---|
| 窗口内 live tick | ≈T | 是（`5df_live`） | 按 phase_status | 通常 true |
| catchup 发现时刻即时盘 | 发现时 | 是但 late；live 不计命中 | 常 false / late | false |
| `capture --allow-late` | 现在 | **否**（staging） | — | — |
| **R-A asof** | hist≤T | 是（`5df_hist_asof`） | **true** + late | **true**（开赛后≤3h） |
| **R-D 晚补** | hist≤T | **是**（同 asof） | **true**（准确时）+ late | **false** |

---

## 6. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-10-09 | 首版：场景矩阵、R-A…R-F、默认策略、队列格式；`mark_missed` 入队最小接线；自动消费列为下一步 |
| 2026-10-09 | §7 多日/全日缺口（S17–S19）；`rescue_gap_scan.py` + `rescue_worker.py`；默认回溯 7 天；恢复后先扫再消费 |
| 2026-10-09 | 再拍：approx 中盘120/临盘60；R-D 改为晚补分层（关即时推荐、仍进模拟）；过远首开默认可标采纳 |

---

## 7. 多日 / 全日缺口扫描与批量补齐（S17–S19）

用户补充（2026-10-09）：漏点时间过长、一天甚至若干天比赛都漏了，也要能**自动检测并补齐**。

### 7.1 「应采未采」定义

对竞彩日 D、plan 中每个可入队目标（默认 **规则 mid/close**；`--include-real` 时含真实）：

1. 已过窗口：`now > target_at + 10min`（与 live `WIN_AFTER` 一致）；且  
2. 满足任一：
   - `state/captured_D.json` **无该 target_key**；或  
   - `status=missed`（或其它非成功态：非 `captured` / `captured_late` / `asof_backfilled` / `no_odds`）；或  
   - 规则通道下副本 `odds_snapshot` 对应 `(match_uid, channel, point)` **无任一 CORE 书**行（防 state 误标成功）。

11:10 / pending / catchup 目标**不入**本扫描。

### 7.2 扫描窗与升级

| 项 | 默认 |
|---|---|
| 回溯 | **N=7** 个竞彩日（可 `--days` / `--from`–`--to`） |
| 无 plan 文件的日 | 跳过并在报告 `skipped_no_plan` 列出（不自动拉 CSL；需先有 plan） |
| 开赛后 ≤3h | **R-A**（`recommend_live_ok=true`，`sim_ok/features_ok=true`） |
| 开赛后 >3h | 入队标记 → 消费走 **R-D 晚补**（仍写规则主值；`recommend_live_ok=false`） |
| 积压排序 | ① 即时推荐仍可用 ② kickoff 近→远 ③ mid→close ④ 竞彩日旧→新 |

### 7.3 配额与让路

| 项 | 默认 |
|---|---|
| 单轮消费 | ≤ **8** 条（`--max`） |
| 每日成功/late/no_odds | ≤ **40**（`--daily-cap`） |
| 禁消费 | shared 让路窗 + **11:00–11:20**；距下一 live due **≤15min** 不消费 |
| 热路径 | **禁止**在 `tick` 内同步 gap_scan/asof；daemon 只 `mark_missed` 入队 |

### 7.4 工具与恢复顺序

```
# 1) 缺口扫描（可 dry-run）
python scripts/rescue_gap_scan.py --days 7 --dry-run
python scripts/rescue_gap_scan.py --days 7

# 2) 空闲窗消费（可 dry-run）
python scripts/rescue_worker.py --dry-run
python scripts/rescue_worker.py --max 8

# 或经 live_capture 子命令
python scripts/live_capture.py gap-scan --days 7 --dry-run
python scripts/live_capture.py rescue-consume --dry-run
```

**daemon / ensure-daemon 恢复后推荐顺序**（人工或看门狗脚本，**勿塞进 tick**）：

1. `ensure-daemon`（拉起 live）  
2. `gap-scan --days 7`（把停机期间应采未采入队）  
3. 确认非让路窗 / 非 due 邻近后 `rescue-consume`  
4. 大积压：多轮消费，靠日上限与 due 避让自动节流  

报告目录：`5dollar/live/rescue_queue/reports/`（`gap_scan_*.json`、`rescue_worker_*.json`）。

### 7.5 与单场漏点关系

| 来源 | 入队方式 |
|---|---|
| 窗口内未抓到 | `mark_missed` → R-C（热路径只入队） |
| 停机数小时～数天 / 全日漏 | `gap_scan` → 同一 pending 队列（幂等，不重复） |
| 消费 | 同一 `rescue_worker`（R-A / R-D 晚补） |
