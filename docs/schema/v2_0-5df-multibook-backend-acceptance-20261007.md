# v2.0 · 5DF 多庄历史回补 · 后端只读入库验收（2026-10-07）

> **角色**：后端工程师 executor · **模式**：只读验收（`DUAL_WRITE.off`，未写现网 `app.db`，未跑写库 import）。  
> **原料**：`$ODDS_DATA_DIR/backfill/5df-multibook-history-queue/`  
> **铁律**：`schema/v2_0-as-of-betting-iron-rules.md` + `research/shadow-ledger/as-of-betting-iron-rules.md`；泄漏清单 `acceptance-leakage-checklist.md` §2.2。  
> **对照设计**：`v2_0-odds-timeline-storage-design.md`、`v2_0_odds_timeline.sql`、探针 `import_odds_timeline_probe.py`、D2 `sync_odds_asian_from_snapshot.py`。  
> **分析师 QA 风险文档**：`schema/v2_0-backfill-qa-risks-20261007.md` + `schema/v2_0-backfill-qa-summary.json（未公开）`（审计时刻 2026-10-07T14:38:23+08:00）——见 §6 交叉；本验收仍只读、未写库。

---

## 0. 结论摘要

| 项 | 判定 |
|---|---|
| **总评** | **CONDITIONAL** |
| 结构 → timeline / snapshot | **PASS（有条件）**：raw hist 字段与 `odds_timeline_seg` / `odds_snapshot` grain 对齐；须经探针同款 parser，禁止用 `/odds` snap 的 closing/inplay 填 mid |
| mid 仅 as-of history | **CONDITIONAL**：探针路径正确；原料含大量赛后 tick + odds snap 含 closing/inplay——导入器若走错源即泄漏 |
| fixture↔竞彩映射 | **CONDITIONAL**：`match_uid` 在 map 内唯一；**`fixture_id` 一对多 1078**（±1 邻日 join 双胞胎）；入库主键须以竞彩 `match_uid` 为准并闸掉脏邻日 |
| 副本幂等 / 跳过 / 重试 | **PASS（设计级）**：现有 UNIQUE + `odds_fetch_*` 可挂；须明确「只 INSERT OR IGNORE、永不 UPDATE 旧手工 / 旧 snapshot」与失败状态机 |
| 现网 / DUAL_WRITE | **PASS**：目录 `DUAL_WRITE.off`；本验收未触现网 |

**一句话**：原料够进副本时间线，但 **mid 必须 `last_tick_at_or_before(pre_match_hist, target_at)`**；**唯一业务键是 `match_uid`（jingcai_date|jc_id），不是 fixture_id**；副本导入只 splice / skip-existing，失败进 `odds_fetch_queue` 重试，禁止盖手工盘。

---

## 1. 结构能否稳进 odds 时间线表

### 1.1 抽样现状（2026-10-07 约 14:36 UTC+8）

| 对象 | 规模 / 形态 |
|---|---|
| `queue/done.jsonl` | **~2391** 行（目标 universe ~5034；另 pending ~69、unmapped ~2580） |
| `logs/fill_report.jsonl` | ~2393；`ok=true` 几乎全过（1 条 `rate_reserve`） |
| `raw/odds/` | ~2389 文件：`{fixture_id}_macauslot_pinnacle.json` |
| `raw/hist/` | ~4776 文件：`{fixture_id}_{macauslot\|pinnacle}_asian.json`；done 覆盖 **缺 hist=0** |
| `csl_fixture_map.json` | `n=3516` items；`window_note`: 竞彩日 12:00 BJ → +24h |

**done 行字段**：`fixture_id, match_uid, jingcai_date, jc_id, home_team, away_team, books, market, phase, status, finished_at, calls[]`。  
**fill_report**：`fixture_id, match_uid, jingcai_date, at, dry_run, calls[], ok, error, fatal`。

**hist tick（实测）**：

```text
{ minute, line, home, away, score{home,away}, recorded_at, [suspended] }
```

- `recorded_at`：带偏移 ISO（多为 `+00:00`）
- `home`/`away`：欧式小数含本金（~1.7–2.1）→ 港盘水位 = `price − 1`（与探针 `_hk_water` 一致）
- 抽样 30 场：约 **19/30** 含 `minute!=null` 或 `recorded_at ≥ kickoff` 的**赛后/滚球** tick；必须滤掉

**odds snap（实测）**：`bookmakers[].odds.asian_handicap.{opening,closing,inplay}` —— 仅适合 blob 归档与 `api_opening`/`api_closing` 对照点；**禁止**当 rule/actual mid。

### 1.2 可直接映射

| 原料 | → 表 / 列 | 备注 |
|---|---|---|
| hist tick（赛前） | `odds_timeline_seg`：`line, price_home/away, water_*=price-1, seg_start_at=recorded_at` | 变化点 RLE；`book`: `macauslot→macau`, `pinnacle→pinnacle`；`market=asian`；`source=5dollar_history`；`water_src=actual` |
| hist + `channel_targets` | `odds_snapshot`：`open / rule.mid|close / rule_legacy.* / actual.t8|t1|close` | 与 `import_odds_timeline_probe.insert_channel_snapshots` 同路径 |
| raw 文件 path+sha256 | `odds_fetch_blob` | `kind=odds|history`；正文仍库外 |
| done / pending | `odds_fetch_queue` 种子 | `task=history|odds_snap|backfill` |
| `match_uid` + 队名 + `kickoff_at`（map） | `matches`（副本） | 正式竞彩 uid，勿用 `probe:` 前缀 |

### 1.3 缺口

| 缺口 | 影响 |
|---|---|
| 无现成「批量 5DF→timeline」脚本（仅探针 2 场） | 需新 `import_5df_multibook_history.py`（副本 only） |
| kickoff 在 map，不在 done 行 | 导入须 join `csl_fixture_map`（或 matches）；缺 kickoff → 无法滤赛后、无法算 target |
| 稀疏 hist（抽样 500：empty≈6，&lt;3 tick≈40） | mid 可能 `stale_gap=1` 或空 snapshot；须记 gaps，不得用 closing 顶替 |
| 仅 macauslot+pinnacle / asian | 与 6 庄全量设计差 4 庄；本期范围可接受，schema 已预留 |
| book slug vs 库内 book | 必须映射表；漏映射会双写两套 book 键 |
| done.jsonl 3 场双行（同 uid 毫秒级重复 append） | 导入前按 `fixture_id`/`match_uid` 去重 |

### 1.4 类型 / 语义风险

| 风险 | 说明 |
|---|---|
| 时区 | tick UTC、竞彩日/BJ cutoff → 统一解析带 tz，target 用 `collection_schedule`（UTC+8） |
| `line` REAL | 半球/四分之一盘 OK；NULL（滚球 suspended）不得进赛前 seg |
| 价格 vs 水位 | 库同时存 price 与 water；结算默认仍固定 0.95，真水仅对照 |
| `UNIQUE(match_id,book,market,channel,point)` | 同点重导跳过；改算法须新 `source`/`channel` 或显式删副本行，禁止静默 UPDATE |

---

## 2. mid 只能从 history tick 在 as-of 时刻推导（写透）

### 2.1 铁律对照

- 模拟下注钉在 cutoff / 规定 mid 时刻（铁律 §1）。
- 泄漏清单 §2.2：**临盘／中盘取决策前快照，非收盘／赛后盘**。
- 设计 §1.2 / 探针实现：

```text
pre = pre_match_ticks(hist)          # 丢 minute!=null、recorded_at≥kickoff、suspended
mid_tick = last_tick_at_or_before(pre, target_at_rule_mid)
# target_at 来自 channel_targets(jingcai_date, kickoff_hour) —— 非「随便一条 close」
```

**定义（验收口径）**：`odds_snapshot` 上任意 `point=mid`（及 rule `close`、actual `t8`/`t1`）的 `(line, water_*)` **必须**来自满足 `recorded_at ≤ target_at` 的**最后一条赛前 hist tick**；`recorded_at`/`target_at`/`lag_hours`/`stale_gap` 一并写入。

### 2.2 已观察到的危险导入路径（原料侧存在，脚本侧须闸死）

| 危险路径 | 为何泄漏 | 闸门 |
|---|---|---|
| **用 `/odds` `asian_handicap.closing` 当 mid** | closing ≈ 临场末盘，晚于规定 mid；早场甚至贴近开赛 | **禁止** snap→`point=mid`；snap 最多 `api_opening`/`api_closing` |
| **用 snap `inplay` 或 hist 滚球 tick** | 含赛果信息（score/minute） | `pre_match_ticks`：`minute is None` 且 `recorded_at < kickoff` |
| **用 hist 最后一条（含赛后）当 close/mid** | 抽样多数文件含 post-ko | 先滤赛前再取 last / at-or-before |
| **无 cutoff：整表用同一「最新盘」回填** | 违反 as-of | 每场独立 `target_at`；禁止全局最新 tick |
| **upsert 覆盖旧手工 `odds_asian` mid/close** | 破坏冻结 V3 / 177 手工 | 副本可建 snapshot；同步旧表 **仅 INSERT 缺失键**（D2 已写死）；现网 DUAL_WRITE 关 |
| **缺 hist 时用 closing 顶 mid** | 静默换源 | 缺 tick → **跳过该 point + gaps 日志**，禁止 fallback closing |

### 2.3 安全参考实现（已存在，应复用）

- `import_odds_timeline_probe.py`：`pre_match_ticks` + `last_tick_at_or_before` + `insert_channel_snapshots`。
- `sync_odds_asian_from_snapshot.py`：`sync-rule` **永不 UPDATE**；缺 `rule.mid` 记 gaps。
- 队列 README 已写明：「mid 推导必须用 `recorded_at ≤` 目标时刻的 tick；脚手架只落 raw，不发明水位」——**入库脚本须继承，不可在 import 层发明**。

### 2.4 建议自动化 guard（副本导入 CI / dry-run）

1. 抽 ≥20 场：断言 `snapshot.recorded_at ≤ snapshot.target_at`。  
2. 断言 mid 行 `source` ∈ `{5dollar_history}`，且 raw 侧能定位到同一 tick。  
3. 断言无 mid 行的 line/water 等于该场 odds snap `closing`（相等则 FAIL——高度疑似错源）。  
4. 断言 mid 所用 tick `minute is null`。

---

## 3. fixture_id ↔ 竞彩映射与唯一约束（写透）

### 3.1 映射事实

- **主键候选（竞彩侧）**：`match_uid = "{jingcai_date}|{jc_id}"` —— map 内 **0 重复**。
- **fixture_id**：同一 5DF 场可对应 **两个** sporttery `match_uid`（±1 日邻日 join）。
  - **1078** 个 fixture_id 出现 2 条 map 行；**全部同主客队**（非错队，是**日历/竞彩日归属双胞胎**）。
  - `join_offset_days`：`-1` ×1077，`+1` ×1；`join_method=neighbor_jc`。
  - 例：`1769150633` → `2025-10-25|六031`（CSL 原文）与 `2025-10-26|六031`（neighbor，`csl_file_date=2025-10-25`）。
- done 覆盖：双胞胎 **无一双双进 done**；约 1044 对中有一侧在 done，其中 **~834 为 neighbor 侧**——即拉取键可能挂在「偏移后的 jingcai_date」上。

### 3.2 推荐唯一约束

| 层 | 约束 | 说明 |
|---|---|---|
| 竞彩场 | `matches.match_uid` **UNIQUE** | 业务真源 |
| 5DF 链 | `matches.fixture_id` **UNIQUE WHERE NOT NULL**（或独立 `fixture_map(fixture_id) UNIQUE → match_id`） | **入库前必须消歧**：一 fixture 只绑一个 canonical `match_uid` |
| 时间线 | 已有 `UNIQUE(match_id, book, market, seg_start_at, compression)` | |
| 快照 | 已有 `UNIQUE(match_id, book, market, channel, point)` | |
| 原始包 | `odds_fetch_blob.path UNIQUE`；建议另加 `(fixture_id, kind, book, market)` 业务唯一 | |
| 队列 | `(fixture_id, task, book, market)` 或 `(match_uid, task, …)` UNIQUE | 防重复拉取/导入 |

**不要**用 `(fixture_id, book, line_type)` 当竞彩主键——`line` 随时间变，且 fixture 有一对多。

### 3.3 碰撞 / 一对多处理建议

1. **Canonical 规则（建议）**：优先 `source=5df_chinasportslottery` 且 `join_method` 空（文件日=竞彩日）；neighbor 行标 `ambiguous_neighbor=1`，**默认不建 matches 行**，仅当 sporttery universe 键只能靠 neighbor 命中时才启用，并写 `extras_json.join_*`。  
2. 若 neighbor 已进 done：导入时仍以 **done.match_uid（sporttery 键）** 建场，fixture_id 作属性；若随后 CSL 原文日出现同一 fixture，**禁止**再插第二 `matches` 行——改挂映射审计表。  
3. 队名 soft-score 并列 → map 已计 `ambiguous` 跳过；导入侧再抽查 team 一致性。  
4. ±1 日是竞彩日 vs CSL 文件日口径差，不是随意模糊匹配——文档化，勿再扩到 ±2。

### 3.4 映射约束一句话

**`match_uid` 唯一决定竞彩场；`fixture_id` 最多绑一场，邻日双胞胎先消歧再入库。**

---

## 4. 副本导入钩子：幂等 / 跳过已有 / 失败重试（写透）

> 以下为**接口与状态机建议**，本轮**不实施写库**。

### 4.1 建议脚本

```text
# 仅副本，例如 data/v2d_5df/app.db；拒绝现网除非双开关（默认关）
.venv/bin/python scripts/import_5df_multibook_history.py \
  --db data/v2d_5df/app.db \
  --queue-dir $ODDS_DATA_DIR/backfill/5df-multibook-history-queue \
  --map csl_fixture_map.json \
  --books macau,pinnacle \
  --markets asian \
  --skip-existing \
  --dry-run          # 先报告 inserts/skips/gaps
```

复用：`pre_match_ticks` / `ticks_to_segments` / `last_tick_at_or_before` / `channel_targets` / `BOOK_MAP`（自探针抽出 shared module 更佳）。

### 4.2 幂等与跳过

| 步骤 | 行为 |
|---|---|
| matches | `INSERT OR IGNORE` by `match_uid`；已存在则复用 `id`，**不改**已冻 kickoff/队名（冲突进 report） |
| timeline_seg | 依赖 UNIQUE → IntegrityError 跳过；`--skip-existing` 先 SELECT 计数 |
| snapshot | 同上；**禁止** `ON CONFLICT DO UPDATE` 盖 `rule_legacy` / 手工源 |
| blob | path UNIQUE；sha 变则新 path 或报 `sha_mismatch` |
| → odds_asian | 仅副本演练：D2 `sync-rule` 风格 **INSERT 缺失 phase**；缺 mid 不补 closing |

报告字段建议：`inserted_seg, skipped_seg, inserted_snap, skipped_snap, gaps[](), collisions[], fatal[]`。

### 4.3 失败重试队列（挂现有表）

沿用 `odds_fetch_queue`：

| 字段 | 用法 |
|---|---|
| `task` | `import_timeline` / `import_snapshot` / `fetch_hist`（若 raw 缺） |
| `status` | `pending → running → done \| retry \| dead` |
| `attempts` | +1；`not_before` 指数退避（如 2^n 分钟，封顶） |
| `last_error` | 短码：`empty_hist` / `no_kickoff` / `fixture_collision` / `parse_error` / `stale_only` |
| `checkpoint_json` | `{match_uid, fixture_id, book, market, stage}` |

**状态机**：

```text
pending --claim--> running --ok--> done
                      |--retryable err--> retry (attempts<max, set not_before)
                      |--fatal / attempts≥max--> dead
retry --not_before due--> pending
```

与 jsonl 队列关系：`done.jsonl` = 拉取完成；**导入另册**（`logs/import_report.jsonl` + DB queue），避免「raw 有了等于库有了」。

### 4.4 硬拒绝条件（脚本启动）

1. 缺 `DUAL_WRITE.off` 或 env `DUAL_WRITE_ODDS_ASIAN=1` → exit。  
2. `--db` resolve 等于现网 `data/app.db` → exit（除非显式双开关，默认不提供）。  
3. map 中同一 `fixture_id` 两条且均无 canonical 标记 → 该 fid **不导入**，进 `dead`/`collisions`。

---

## 5. 建议闸门清单（入库前）

1. **As-of mid**：仅 hist `recorded_at ≤ target_at`；自动化 guard §2.4。  
2. **禁 closing/inplay→mid**；禁无赛后 tick。  
3. **`match_uid` UNIQUE；fixture 一对一消歧**后再 INSERT matches。  
4. **INSERT OR IGNORE / skip-existing**；禁止 UPDATE 旧 snapshot / 旧 `odds_asian`。  
5. **DUAL_WRITE 关**；只写副本路径。  
6. **gaps 显式**：空/稀疏 hist → 跳过 point，不写假水位。  
7. **book 映射** `macauslot→macau` 单测。  
8. **时区**：kickoff / target / tick 统一可比较。  
9. **预测冻结**：本管道不碰 `predictions` / V3 主列。  
10. **邻日 join**：导入报告列出 neighbor 占比与抽检队名。

---

## 6. 与分析师 QA 交叉

> 对照：`v2_0-backfill-qa-risks-20261007.md` + `v2_0-backfill-qa-summary.json（未公开）`（审计 **2026-10-07T14:38:23+08:00**）。  
> 本轮仍 **只读交叉**：**不写库、不开双写、不进副本**。总评维持 **CONDITIONAL**；**fixture 邻日双胞胎消歧完成前禁止副本 ingest**。

### 6.1 对齐点（BE ↔ QA 一致）

| 主题 | 后端验收 | 分析师 QA / summary | 对齐结论 |
|---|---|---|---|
| **mid 样本无赛后泄漏** | §2：须 `pre_match_ticks` + `last_tick_at_or_before`；禁 closing/inplay→mid | 全量 mid 泄漏 **0**；抽样 20/20 双庄末条赛前 `recorded_at ≤ kickoff`；双庄 mid 可推导 **~97.5%** | **对齐**：as-of 闸门有效；入库须继承探针路径，不得换源 |
| **双胞胎 / neighbor** | §3：`fixture_id` 一对多 **1078**（−1×1077，+1×1）；业务键 `match_uid` | map 内 neighbor **1078**；队伍冲突 **0**；宇宙内 neighbor_joined 仅 19 | **对齐**：规模一致；消歧规则（canonical 竞彩日）入库前必做 |
| **禁止入库（现状）** | 未写现网/副本；`DUAL_WRITE.off`；硬拒绝现网路径 | `research_db_multibook_ingested=false`；现网 matches=177、macau mid=0；只落 raw | **对齐**：原料可用 ≠ 已 ingest；本轮继续禁写 |
| 泄漏清单 / splice | §5 闸门：INSERT OR IGNORE、禁 UPDATE 手工 | checklist B/C/E：as-of、权威 uid、禁止年窗「完成」宣称 | **对齐** |

### 6.2 分析师侧新增 / 加细风险（BE 须吃进闸门）

| 风险 | QA 证据 | 对后端副本导入的影响 |
|---|---|---|
| **26 hist `has_more` 未翻页** | 多为 pinnacle、`count=500` 截断 | mid（较早 target）影响较小；**close / actual T−1h** 可能缺临盘 tick → 标 `hist_truncated` 或补翻页后再写 close |
| **澳门 `tick_age>2h`** | 有 mid 中 **~89.23%**（2096/2349）；37 场无赛前 tick | snapshot 须写 `approx` + `tick_age_hours`；缺赛前 → **跳过 mid + gaps**，禁止 closing 顶替 |
| **双 worker 重复写** | done 内 **3** 个 `fixture_id` 各 2 行（同秒 cache_hit / exit 143） | 导入前按 `match_uid`/`fixture_id` 去重；队列 UNIQUE；单 worker 锁（运维） |
| （背景）年窗未完成 | unmapped **2580**（CSL 期仍 **2275**） | 禁止宣称年窗完成；仅对 done 且三文件齐全子集做副本演练 |

### 6.3 交叉后仍维持的判定

| 项 | 维持 |
|---|---|
| **总评** | **CONDITIONAL**（结构可进时间线；mid/映射/幂等均有条件） |
| **入库** | **消歧前不进副本**；`DUAL_WRITE` 保持关；现网零触碰 |
| **唯一键** | `match_uid` 权威；`fixture_id` 一对一消歧后再 `INSERT matches` |
| **mid** | 仅 hist as-of；QA 泄漏=0 不降低脚本侧闸门强度 |

**交叉一句话**：QA 证实 mid 无赛后泄漏且双胞胎规模与 BE 一致，并新增分页截断 / 澳门 approx / 双 worker 重复写三条入库前闸门；总评仍 CONDITIONAL，消歧完成前不进副本。

### 6.4 其它对照文档

| 文档 | 状态 | 交叉 |
|---|---|---|
| `acceptance-leakage-checklist.md` §2.2 / §2.6 / §4 | 已读 | 盘口时间线泄漏、队名/赛事错配、splice/skip-existing |
| `v2_0-backfill-csl-map-status.md` 等 | 存在 | 邻日 join 与 §3 / QA neighbor=1078 同向 |

---

## 7. 本轮未做（遵守禁止项）

- 未写现网 / 副本 DB，未开 DUAL_WRITE，未改 predictions，未 merge PR，未跑写库 import。  
- 仅文件系统与脚本只读抽样。

---

*生成：2026-10-07（UTC+8）· executor 只读验收*
