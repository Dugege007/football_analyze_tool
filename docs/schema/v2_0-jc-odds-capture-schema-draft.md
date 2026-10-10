# 竞彩胜平负／让球胜平负 · 日常采 + 表结构草案（副本优先）

> **用户 2026-10-10 更正（优先于本文其他内容）**：「初盘」只有一个定义，就是各家公司开盘时的数据；竞彩日 11:10 只是我们去取数据的时间，不是一种盘口。本文中把 11:10 快照写成「即时（11:10）」、`rule_1110` 阶段、`include_live=rule_1110` 或 `live_rule_1110_*` 字段的内容，从 v0.1.9 起全部作废，只作历史记录保留。数据库里已有的 11:10 快照不删除，库结构不改。完整定义见 `v2_0-odds-phase-terminology.md` 开头一节。


> **状态**：方案草案，供后端落地。先落 **v2d3 副本**，`DUAL_WRITE` **关**，不写现网 `app.db`。  
> **日期**：2026-10-08 18:56 UTC+8  
> **口径来源**：[`v2_0-jc-and-fundamentals-status-20261008.md`](./v2_0-jc-and-fundamentals-status-20261008.md)、[`../sporttery/notes.md`](../sporttery/notes.md)、盘口术语 [`v2_0-odds-phase-terminology.md`](./v2_0-odds-phase-terminology.md)

---

## 0. 现状（v2d3）

| 表 | 行数 | 问题 |
|---|---|---|
| `odds_jc_home` | 352（约 177 场 × open/close） | **只有 `home_win`**，缺平/负 |
| `odds_jc_hhad` | 0 | 表已预留，未入库 |
| `odds_snapshot` | 有少量 `book=jc, market=euro_1x2` 探针 | 正式 `market=jc_spf` / `jc_hhad` 日常行尚未建 |
| 官网 | `getMatchCalculatorV1` 可取完整 `had`/`hhad` | **需要能访问竞彩官网的网络环境**；否则可能返回 HTTP 567 |

---

## 1. 设计原则（已钉死）

1. 竞彩与公司盘 **分表、分算**；N5 共识 **只用公司盘真实水位**，竞彩不进 N5（除非日后另开「竞彩校准」并换指纹）。
2. 跟亚盘一样记 **阶段、抓取时间、`usable_at_*`**。
3. 让球胜平负存 **让球数**；让球数变了 = **换盘**，不与旧让球赔率拼进同一条特征。
4. 缺一项（胜/平/负任一空）→ 整场该市场标 `jc_1x2_incomplete=true`；**禁止**用单边 `home_win` 冒充完整市场。
5. 本地采集机 未通：副本允许全空；页面灰字「暂无竞彩官方数据」；**不用别源顶上**。
6. 先副本、双写关；现网开关须维护者另批。

---

## 2. 表／字段草案

### 2.1 推荐：新表 `odds_jc_had`（完整胜平负）+ 冻结旧 `odds_jc_home`

**不扩写覆盖** `odds_jc_home` 旧行（避免 silently 把半套数据当成完整盘）。旧表保留作影子对照；完整市场进新表。

```sql
-- 副本 DDL（建议 migration 名：v2_0_jc_had_hhad_meta）
CREATE TABLE IF NOT EXISTS odds_jc_had (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id        INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  phase           TEXT NOT NULL,          -- open | mid | close | instant_1110
  home_odds       REAL,                  -- 胜 h
  draw_odds       REAL,                  -- 平 d
  away_odds       REAL,                  -- 负 a
  jc_1x2_incomplete INTEGER NOT NULL DEFAULT 0,  -- 1=缺任一项
  source          TEXT NOT NULL,         -- sporttery_local | legacy_home_only | manual
  captured_at     TEXT,                  -- 实际抓取 ISO+08:00；legacy 可 null
  target_at       TEXT,                  -- 目标时刻（11:10 / 中盘目标 / 临盘目标）
  usable_at_mid   INTEGER,               -- 同亚盘 usable 闸门：该快照能否进中盘决策特征
  usable_at_close INTEGER,               -- 能否进临盘决策特征
  sporttery_match_id TEXT,               -- 官网 matchId（如 2041805）
  update_date     TEXT,                  -- 官网 had.updateDate
  update_time     TEXT,                  -- 官网 had.updateTime
  extras_json     TEXT,
  UNIQUE (match_id, phase, source)
);

CREATE INDEX IF NOT EXISTS idx_jc_had_match_phase
  ON odds_jc_had (match_id, phase);
```

**旧表 `odds_jc_home`（只读冻结）**

- 不加 `draw`/`away` 列去「假装完整」。
- 读路径：对比页／调试可显示 `home_win`，但 API 完整竞彩格 **优先读 `odds_jc_had`**；若只有 `odds_jc_home` → 返回 `jc_1x2_incomplete=true`、灰字，不把单边当完整盘。
- 可选一次性影子标注（不改赔率）：在 `match_meta.extras_json` 或独立配置记 `jc_home_legacy_incomplete=true`。

### 2.2 充实 `odds_jc_hhad`（让球胜平负）

现表已有 `goal_line / home_odds / draw_odds / away_odds`。副本 ALTER 加元数据（不破坏旧 UNIQUE）：

```sql
-- 若 SQLite 无列则 ADD（副本迁移脚本按列探测）
ALTER TABLE odds_jc_hhad ADD COLUMN source TEXT;              -- sporttery_local | …
ALTER TABLE odds_jc_hhad ADD COLUMN captured_at TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN target_at TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN usable_at_mid INTEGER;
ALTER TABLE odds_jc_hhad ADD COLUMN usable_at_close INTEGER;
ALTER TABLE odds_jc_hhad ADD COLUMN jc_1x2_incomplete INTEGER NOT NULL DEFAULT 0;
ALTER TABLE odds_jc_hhad ADD COLUMN goal_line_raw TEXT;       -- 官网 goalLine 原文（如 "-1"）
ALTER TABLE odds_jc_hhad ADD COLUMN sporttery_match_id TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN update_date TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN update_time TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN line_rev INTEGER NOT NULL DEFAULT 0;  -- 同场让球线变更次数
ALTER TABLE odds_jc_hhad ADD COLUMN extras_json TEXT;
```

**换盘规则（硬）**

- 同一 `match_id` + 同一 `phase` 目标下，若新抓 `goal_line` ≠ 已存主行 `goal_line`：
  - **不覆盖**旧行赔率；
  - 旧行移入历史（建议副表 `odds_jc_hhad_line_hist` 或写入 `extras_json.prev_lines[]`）；
  - 新行成为该 phase 主值，`line_rev += 1`；
  - 特征只使用「决策时刻可见的当前让球线」那一套 h/d/a，禁止跨线拼接。

```sql
CREATE TABLE IF NOT EXISTS odds_jc_hhad_line_hist (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id    INTEGER NOT NULL,
  phase       TEXT NOT NULL,
  goal_line   REAL,
  home_odds   REAL,
  draw_odds   REAL,
  away_odds   REAL,
  captured_at TEXT,
  superseded_at TEXT NOT NULL,
  source      TEXT,
  extras_json TEXT
);
```

**UNIQUE 调整建议**：主表仍 `UNIQUE (match_id, phase)` 表示「当前有效让球线」；历史进 hist。若后端更想一行一版，可改 `UNIQUE (match_id, phase, goal_line, captured_at)` 并在视图上取最新——**开放问题 #1**（见文末）。

### 2.3 同步投影到 `odds_snapshot`（副本，可选但推荐）

与亚盘时间线对齐，便于 `/table/matches` 统一读：

| 字段 | had | hhad |
|---|---|---|
| `book` | `jc` | `jc` |
| `market` | `jc_spf` | `jc_hhad` |
| `channel` | `rule`（规则中/临）或 `actual`（真实 T−8h/T−1h） | 同左 |
| `point` | `open` / `mid` / `close` / `live`（11:10 用 `live` + extras.label=`rule_1110`） | 同左 |
| `price_home/draw/away` | h/d/a | h/d/a |
| `line` | null | `goal_line` |
| `recorded_at` | `captured_at` | 同 |
| `target_at` | 目标时刻 | 同 |
| `source` | `sporttery_local` | 同 |
| `extras_json` | 含 `jc_1x2_incomplete`、`usable_at_*`、`sporttery_match_id` | 另含 `line_rev`、`goal_line_raw` |

**禁止**：把竞彩行的 `book` 写成 macau/crown 等；禁止 `market=euro_1x2` 冒充竞彩官方（探针残留勿当生产）。

### 2.4 阶段枚举（与亚盘对齐）

| `phase`（旧表风格） | snapshot `point` | 目标时刻 |
|---|---|---|
| `open` | `open` | 该场竞彩官方首次可售报价（有 `updateDate/Time` 则用之；否则第一次成功自采） |
| `instant_1110` | `live` + label `rule_1110` | 所属竞彩日 **11:10** |
| `mid` | `mid`（规则）/ 对照 `t8` | 非例外：开赛 −8h；例外场：竞彩日 15:00 |
| `close` | `close`（规则）/ 对照 `t1` | 非例外：开赛 −1h；例外场：竞彩日 22:00 |

`usable_at_mid` / `usable_at_close`：抓取时刻须 ≤ 对应决策 cutoff；官网 `updateDate+updateTime` 若晚于决策时刻，该快照对该决策 **不可用**（与亚盘 api_opening 闸门同思路）。

### 2.5 官网字段映射

来源：`GET .../getMatchCalculatorV1.qry?client_source=m`（样例 `sporttery/samples/calc_getMatchCalculatorV1.json`）

| 入库 | 官网 |
|---|---|
| 对阵键 | `matchNumStr` + `businessDate` → 对齐 `matches.jc_id` / `jingcai_date` |
| had.h/d/a | `had.h` / `had.d` / `had.a`（字符串转 REAL） |
| hhad | `hhad.h/d/a`，`goalLine`/`goalLineValue` → `goal_line` |
| 官网更新时刻 | `had.updateDate` + `had.updateTime` |
| `sporttery_match_id` | `matchId` |

匹配失败（对不上库内比赛行）：写入 `odds_fetch_queue` / call_log，**不**丢进错误 match_id；日核对列出。

### 2.6 不完整判定

```
jc_1x2_incomplete = 1  iff  home/draw/away 任一为 NULL 或 ≤0
```

- had / hhad **各自**判定。
- API：`complete=false` 时整格灰字；悬停「竞彩胜平负不完整，缺平/负等项」（前端已定）。
- 特征／方案：incomplete 行视为该市场缺失，不进共识、不进校准（校准规则未开之前）。

---

## 3. 日常采节奏（空闲时段）

### 3.1 目标点（与亚盘一致）

| 点 | 目标 | 说明 |
|---|---|---|
| 即时 11:10 | 竞彩日 11:10 | 对照旧手工／V3 |
| 中盘 | T−8h 或例外规则 15:00 | 同术语文档 |
| 临盘 | T−1h 或例外规则 22:00 | 同术语文档 |
| 初盘 | 首次成功拿到的完整 had/hhad | 可晚于开售；记 `captured_at` |

### 3.2 与 5DF 让路窗的关系

5DF 让路窗（补数降速／暂停）：**11:05–11:20、14:55–15:15、21:55–22:15**。  
竞彩走 **Sporttery／本地采集机**，不占 5DF 配额；但仍建议：

1. **优先**：与亚盘同分钟开抓（11:10 / 规则中盘 / 规则临盘），本地采集机 上与 5DF live **并行**（各走各的网）。
2. **若 本地采集机 CPU／带宽告警**：竞彩延后到让路窗结束后 **+2～10 分钟**再抓；`captured_at` 如实写，`fetch_lag_min = captured_at − target_at`；**不**把别源填上。
3. **严禁**为了抢点而在无法访问竞彩官网的网络 直连 Sporttery。

历史补数／空闲回扫：放在上述窗口之外（建议 02:00–06:00、10:00–11:00、12:00–14:00、16:00–21:00 等 本地采集机 空闲段），限速 + call_log。

### 3.3 失败策略（本地采集机）

| 情况 | 行为 |
|---|---|
| 本地采集机 未连接 / ListMachines 无桌面 | **不采**；该日竞彩字段保持空；告警一条（通知维护者或 call_log）；页面灰字 |
| HTTP 567/403 | 确认未误走海外；换 Referer（m/www）有限次；仍失败 → 空 + 告警 |
| `errorCode≠0` / 空 body | 退避重试（如 2–3 次）；记 call_log；不写半截完整标记为 complete |
| 某场缺 had 或缺 hhad | 有的市场照常写；缺的市场行不建或建 incomplete；**不**用公司欧赔顶 |
| 部分场匹配失败 | 已匹配的入库；失败列表进日核对 |

Stub（文档级，本任务不实现爬虫）：

```
# 伪接口（分析师侧 / 本地采集机 侧）
POST /internal/jc/capture_once
  body: { jingcai_date, points: ["instant_1110"|"mid"|"close"|"open"], dry_run? }
  要求：执行主机 = 本地采集机；失败返回 ok=false, reason=collector_unreachable|http_567|…
```

---

## 4. 历史补：`home_win`-only 行

| 策略 | 做法 |
|---|---|
| **不覆盖** | `odds_jc_home` 352 行原样保留 |
| **标 incomplete** | 凡只有 `odds_jc_home`、无对应完整 `odds_jc_had` 的 (match_id, phase) → API/`jc_1x2_incomplete=true` |
| **影子并列** | 日后 本地采集机 补到完整 had 时：**INSERT** `odds_jc_had`（source=`sporttery_local` 或 `sporttery_local_backfill`），**不 UPDATE** `odds_jc_home.home_win` |
| **对照** | 若补数 home 与旧 `home_win` 差 > 0.05 → `extras_json.legacy_home_diff=true` 进日核对 |
| **hhad** | 历史 0 行；有官网历史深度前只采「当前可售」；更深历史另议（notes 已标 ⚠️） |

开放：官网计算器偏当前可售，**历史深度**是否另找接口——见文末开放问题 #2。

---

## 5. 给后端的验收点

1. **副本 only**：迁移与试采只碰 `data/v2d3/app.db`（或指定副本）；`DUAL_WRITE=0`；现网 `data/app.db` sha256 不变。
2. **DDL**：存在 `odds_jc_had`；`odds_jc_hhad` 含 `source/captured_at/target_at/usable_at_*/jc_1x2_incomplete/line_rev`；可选 `odds_jc_hhad_line_hist`。
3. **incomplete**：构造缺 `draw` 的一行 → `jc_1x2_incomplete=1`；`/table/matches` 竞彩格 `complete=false`，不暴露为完整 1X2。
4. **legacy**：旧 352 行仍在；无 `odds_jc_had` 时前端／API 走灰字 incomplete，不读成完整盘。
5. **换盘**：同场同 phase 两次抓让球线不同 → 旧线进 hist（或 prev_lines），主行新线，`line_rev≥1`；特征单测禁止跨线拼接。
6. **本地采集机 失败**：模拟 collector_unreachable → 零竞彩新行、有告警／call_log、无「假成功」。
7. **分表**：竞彩行 `book=jc` 且 `market∈{jc_spf,jc_hhad}`；N5／公司盘共识单测 **不读** 这两类 market。
8. **阶段**：至少能写 `instant_1110` / `mid` / `close`（open 可随后）；`captured_at`、`target_at` 非空（本地采集机 成功时）。
9. **让路**：文档／调度配置写明不与 5DF 抢 本地采集机 到卡死；延后抓带 `fetch_lag_min`。
10. **验收文档**：`backfill/jc-odds-capture-<date>/ACCEPTANCE.md` 含行数、incomplete 计数、本地采集机 状态、现网未写声明。

---

## 6. 开放问题（≤3，需拍板）

1. **hhad 换盘存储**：主表「一 phase 一行当前线 + hist 表」vs「多行按 (phase, goal_line, captured_at) 并存 + 视图取最新」？推荐前者。
2. **历史 had/hhad 深度**：仅「可售窗口内多阶段自采」还是必须另挖历史接口？推荐先自采三阶段 + 旧 home_win 影子，历史接口另立项。
3. **`odds_jc_home` 是否最终 DROP／改名**：推荐长期只读冻结；是否在 API 0.3.x 某版起完全隐藏 home_only，只留 had——等完整覆盖率 ≥ 某阈值再定。

---

## 7. 非目标（本草案）

- 不实现完整爬虫／不在 本地采集机 未连时假装采成功  
- 不写现网、不开 DUAL_WRITE  
- 不把竞彩并进 N5  
- 不做 crs/ttg/hafu 玩法入库（接口有字段，扩展另开）

### 2026-10-08 18:59 补钉（算法顾问三条，已采纳）
见 v2_0-jc-and-fundamentals-status-20261008.md「算法顾问补三条」。

#### hhad as-of 取线（必须）
决策特征禁止默认读主表「当前行」。取线规则：`captured_at ≤ T_decision` 且（`superseded_at` IS NULL OR `superseded_at > T_decision`）；主表无合格行则查 `odds_jc_hhad_line_hist`。hist 行需有 `superseded_at`（被新线替代的时刻）。

#### 竞彩 11:10 窗口（必须）
与亚盘一致：主值仅 `captured_at ∈ [11:00, 11:20]`；超窗自采照存、标 `out_of_window`，主格视为无完整官方即时，不进主值。
