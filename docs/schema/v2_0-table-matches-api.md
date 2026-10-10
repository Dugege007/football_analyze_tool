# v2.0 · 数据表页批量平铺接口 `GET /table/matches`（API 0.3.19）

> **用户 2026-10-10 更正（优先于本文其他内容）**：「初盘」只有一个定义，就是各家公司开盘时的数据；竞彩日 11:10 只是我们去取数据的时间，不是一种盘口。本文中把 11:10 快照写成「即时（11:10）」、`rule_1110` 阶段、`include_live=rule_1110` 或 `live_rule_1110_*` 字段的内容，从 v0.1.9 起全部作废，只作历史记录保留。数据库里已有的 11:10 快照不删除，库结构不改。完整定义见 `v2_0-odds-phase-terminology.md` 开头一节。


> **0.3.19（2026-10-08 UTC+8）**：推迟场（目标时刻只用当时已公布的开赛时间）、`kickoff_jc_conflict`、S2-V2 / N4-V2（真实水位口径，旧 S2/N4 冻结 + 备注）、探针 209/210 人工复核（`v2_0-0316-followup-decisions.md`「0.3.18 之后」节 + 推迟场补充）；分析师追加：即时（11:10）自采优先 + `alt` + `instant_src_diff`、补数让路、N5 每日 ρ 异质性字段、高亮 hl_v0.3（`config_version=hl_v0.3`）。见 §12.2 / §13「0.3.19」。

> **0.3.18（2026-10-08 UTC+8）**：第二批口径 A–F（`v2_0-0316-followup-decisions.md`）+ 例外场按编号（`v2_0-odds-phase-terminology.md` 末节）+ v2d3 只读实例 + N5 ρ 快照。见 §12.1 / §13「0.3.18」。
>
> **0.3.17（2026-10-08 UTC+8）**：0316 follow-up 六条（`v2_0-0316-followup-decisions.md`）+ 前端 §13 对账 + hl_v0.2。要点：legacy_import 初盘 earliest 推定竞彩日 11:10（`ts_inferred=true`），`usable_at_*` 与 api_opening 同规则判（不一刀切 true）；open 对象 `open_basis` 为空时带 `open_basis_reason`；返还率为空则 `baseline_method=null`；行级 `kickoff_source` / `kickoff_placeholder`（5DF 12:00 占位符检查）；`config_version=hl_v0.2`、`config.return_hl_water=real_only`。见 §13「0.3.17」。

> **0.3.16（2026-10-08 17:25 UTC+8）**：§12 五条已按分析师拍板落地（见 §12 开头「已解决」），术语文档末两节为准。

> 日期：2026-10-08（UTC+8）。状态：**已实现、已上现网服务（只读）**。  
> 代码：`api/app/table_matches.py`（路由在 `app/main.py` 末尾 `include_router`）；测试 `tests/test_table_matches.py`。  
> OpenAPI 片段：同目录 [`v2_0-table-matches-openapi.json`](./v2_0-table-matches-openapi.json)（由 FastAPI `/openapi.json` 截取）。  
> 口径依据（本接口不自创口径）：
> - 阶段术语：[`v2_0-odds-phase-terminology.md`](./v2_0-odds-phase-terminology.md)（用户 2026-10-08 定稿）
> - 高亮 / 返还率 / 凯利：[`v2_0-data-table-highlight-rules.md`](./v2_0-data-table-highlight-rules.md)（`hl_v0.2`：返还率上色 / 基准 / ≥20 计数只用真实水位）
> - 防泄漏铁律：[`v2_0-as-of-betting-iron-rules.md`](./v2_0-as-of-betting-iron-rules.md)；赛果入库节奏：[`v2_0-result-ingest-schedule.md`](./v2_0-result-ingest-schedule.md)
> - 规定通道时刻：`app/collection_schedule.py`（唯一来源）
>
> 硬约束：只读（SQLite `mode=ro` 连接）、不写任何表、不改现有表结构、`DUAL_WRITE_ODDS_ASIAN` 仍关、不改 V3 预测与 `produced_at`、不改 settlement 默认。

## 1. 一句话

每场一行，给前端 AG Grid「数据表」页：比赛信息 + 四家亚盘（澳门/皇冠/威廉/**平博**）× 初盘/中盘/临盘（例外场另有 `*_real`）+ 四家欧赔 1X2 + 竞彩胜平负 + V3 预测（冻结只读）+ 赛果与每场结算；后端统一算 **返还率、按公司滚动中位数基准、凯利**（`hl_v0` §4–§6），高亮判定留给前端。缺数据一律 `null` + 来源/可用性标记，不编造。

## 2. 请求

`GET /table/matches`

| 参数 | 默认 | 说明 |
|---|---|---|
| `date_from` | `date_to − 6 天` | 竞彩日起（含），`YYYY-MM-DD` |
| `date_to` | 库内 ≤ as_of 当日的最大竞彩日 | 竞彩日止（含）。现网数据止于 2026-07-05，所以不传日期时返回 06-29～07-05 |
| `scope` | `jc` | `jc`（= `matches.scope='jingcai'`）｜`ext`（= `extra`）｜`all`；旧写法 `jingcai`/`extra` 也接受。沿用 `/matches` 的 `scope` 列约定（现网 177 场全为 jingcai 且都有竞彩编号） |
| `strategy` | `CFFXDJ_5_V3` | `predictions.strategy` |
| `channel` | `rule` | 目前只接受 `rule`（`*_real` 自动取 `actual` 通道 t8/t1）。旧的 `close_mode` 参数已按协作中决定去掉，由 `*_real` 取代 |
| `include_live` | `none` | `none`=`live` 为空数组｜`rule_1110`=只给竞彩日 11:10 的 as-of 即时盘｜`all`=全部即时盘（含 rule_1110 条目） |
| `settlement_version` | 现网默认 `ah_v4_water_midpoint` | 可选 `ah_v4_macau_actual_or_095`；非法值 400（复用 `_resolve_settlement_version`） |
| `as_of` | now | 截止时刻（ISO；无时区按 +08:00）。传未来时刻一律钳到 now |
| `baseline_window_days` | 空（=本场竞彩日之前全部） | 返还率基准滚动窗口；**hl_v0 未写窗口长度，待拍板** |
| `baseline_min_n` | 20 | 基准最少样本场数，不足返回 null；**hl_v0 未写，待拍板** |
| `format` | `nested` | `nested`｜`flat`（扁平键，AG Grid 直接用；见 §9） |
| `limit` / `offset` | 500 / 0 | 上限 2000；响应带 `total` |

排序沿用 `/matches`：`jingcai_date, kickoff_hour, jc_no, id`。

示例：

```bash
curl -s 'http://127.0.0.1:8787/table/matches?date_from=2026-06-01&date_to=2026-06-07&scope=jc&strategy=CFFXDJ_5_V3&channel=rule'
curl -s 'http://127.0.0.1:8787/table/matches?date_from=2026-06-01&date_to=2026-06-07&format=flat&include_live=rule_1110'
```

## 3. 响应顶层

| 字段 | 说明 |
|---|---|
| `api_version` | `0.3.15` |
| `config_version` | **`hl_v0.2`**（0.3.17；0.3.16 为 hl_v0.1） |
| `as_of` / `as_of_requested` | 实际截止时刻（+08:00）/ 请求原值 |
| `date_from` `date_to` `scope` `strategy` `channel` `include_live` `settlement_version` `format` | 生效参数回显 |
| `books` | `["macau","crown","william","pinnacle"]` |
| `config` | 生效口径：`result_visible_after_kickoff_hours=3`、`last_prematch_fresh_minutes=15`、`live_rule_1110="11:10"`、`baseline{method,group_by,sample,window_days,min_n,excludes}`、`kelly{...}`、`ah_line_convention`、`real_phases_calc` |
| `data_sources` | `odds_snapshot` / `odds_timeline_seg` 表是否存在（现网都为 `false`）、旧表清单 |
| `total` `limit` `offset` `count` | 分页 |
| `items[]` | 每场一行（§4） |

## 4. 行结构（`format=nested`）

```text
item
├─ match_id                       match_uid，如 "2026-06-17|三204"
├─ match{match_id, match_pk, jc_id(竞彩编号), jc_no, jingcai_date(竞彩日), kickoff_at(开赛, +08:00),
│        kickoff_minute_known, scope, league(联赛原名), league_canonical, competition_type,
│        home_team, away_team, home_team_canonical, away_team_canonical}
├─ phase_exception                true = 例外场（collection_schedule.is_rule_fixed_clock：开赛 ≥23:00 或早场带 [00:00,11:30]）
├─ schedule{phase_exception, open_target_time(null，初盘按各家最早一条), mid_target_time, close_target_time,
│           mid_real_target_time, close_real_target_time(仅例外场), live_rule_1110_target_time, source}
├─ ah.{macau|crown|william|pinnacle}
│     ├─ open | mid | close       AhCell（规则值；返还率按这三个算）
│     ├─ mid_real | close_real    AhCell 去掉返还率字段；非例外场为 null
│     └─ last_prematch            LiveEntry + minutes_before_kickoff + stale；无记录 null
├─ x1x2.{macau|crown|william|pinnacle}
│     ├─ open | mid | close       X1x2Cell（含返还率、基准、凯利）
│     ├─ mid_real | close_real    X1x2Cell 去掉返还率/凯利字段；非例外场为 null
│     └─ last_prematch
├─ x1x2_base.{open|mid|close}{pinnacle{home,draw,away} | null, multi_avg{home,draw,away,n_books,books[]} | null}
├─ multi_avg_prob | null           = x1x2_base.close.multi_avg（前端名，规则临盘的多家平均去水概率）
├─ jc_1x2.{open|mid|close}        JcCell（竞彩胜平负）
├─ live[]                          LiveEntry；include_live=none 时为 []
├─ prediction | null               与 /matches/{id}/prediction 完全同字段 + produced_before_kickoff
├─ prediction_hidden_reason       no_prediction | produced_after_as_of | null
├─ produced_at                     = prediction.produced_at（冻结只读）
├─ as_of                           本次截止时刻
├─ result | null                   {home_goals, away_goals, total_goals, score:"1-0", wdl}
├─ result_hidden_reason           before_kickoff | not_visible_yet(开赛后 3h 内) | no_result | kickoff_unknown | null
├─ hidden_reason                   = result_hidden_reason（前端名）
├─ settlement | null               §7
└─ settlement_hidden_reason       no_prediction | before_kickoff | not_visible_yet | no_result | kickoff_unknown | produced_after_as_of | no_settle_line | bad_direction | null
```

### 4.1 AhCell（亚盘一格）

| 字段 | 说明 |
|---|---|
| `line` | 盘口，**主队视角、正数=主让**（与 `odds_asian.handicap`、结算同记法）。`odds_snapshot`/`odds_timeline_seg` 里 5DF API 记法是「负数=主让」，本接口已取反统一 |
| `home_water` / `away_water` | 港盘水位；无则 null |
| `water_source` | `actual`（API 真实水位）｜`tier_midpoint`（旧手工档位 t 按 0.70+0.05t 换算的中点）｜`null`（无水位）。「0.95 回落」只出现在结算 `settlement.juice_source`，不出现在展示水位里，不拿 0.95 冒充展示值 |
| `water_censored` | 档位截断值（t=0/10）；true 时不算返还率、不进基准 |
| `recorded_at` | 实际报价/抓取时刻（+08:00）；旧手工导入为 null（时刻未知） |
| `target_at` | 该阶段目标时刻：快照行取快照自带 `target_at`；旧手工导入为 null（其时钟是旧采集口径≈`rule_legacy`，不冒充规则时刻） |
| `basis` | `first_record`（初盘=该家最早一条）｜`asof_rule`（规则时刻及之前最后一条）｜`asof_real`（赛前 8h/1h 及之前最后一条）｜`legacy_import`（现网旧手工月度包）｜null |
| `source` | `odds_snapshot/<channel>/<point>`｜`odds_asian`｜null |
| `available` / `hidden_reason` | 是否有值；`hidden_reason`：`after_as_of`（该格时刻晚于 as_of，被隐藏）｜`no_data`（库里没有，如平博） |
| `minutes_since_open` | 本格 `recorded_at` − 同家 `open.recorded_at`（分钟）：跨公司比较变化时按各家自己的开盘时间（术语文档「初盘口径」） |
| `return_rate` | `1 ÷ (1/(1+主水) + 1/(1+客水))`（hl_v0 §4），保留 6 位 |
| `return_rate_baseline` / `_n` | 该公司同阶段、**竞彩日严格早于本场**的滚动中位数及样本数；不足 `min_n` → null |
| `return_rate_dev` | `return_rate − baseline` |

### 4.2 X1x2Cell（欧赔一格）

`home/draw/away` 三项赔率，`complete`（三项齐全），`recorded_at`（报价时刻，未知为 null）、`fetched_at`（仅 `api_opening/api_closing` 类：抓取时刻）、`target_at`、`basis`（另有 `api_opening` / `api_closing` = 5DF `/odds` 接口给的开盘/收盘价，**报价时刻未知**）、`source`（`odds_snapshot/...`｜`odds_euro_home(home_only)`）、`available`、`hidden_reason`、`minutes_since_open`、`return_rate`（`1 ÷ (1/胜+1/平+1/负)`，三项齐才算）、`return_rate_baseline/_n/_dev`，以及凯利：

| 字段 | 说明 |
|---|---|
| `kelly{home,draw,away}` | 主列凯利 = 本家赔率 × 基准概率 |
| `kelly_base` | `pinnacle`（平博去水概率）｜`multi_avg`（多家平均）。平博本家、或缺平博三项时 → `multi_avg` |
| `kelly_base_n_books` | 基准家数（pinnacle=1；multi_avg=参与平均的家数） |
| `kelly_multi_avg{home,draw,away}` | 「多家平均」参考列（每家都给） |
| `multi_avg_n_books` | 参考列家数 |

去水：`p_i = (1/odds_i) / Σ(1/odds)`；多家平均（`multi_avg`）= 当期（同阶段）有完整三项的各家（澳门/皇冠/威廉/平博中可用的）各自去水后取算术平均，返回 `n_books` 与 `books[]`（hl_v0 没写缺家时怎么办 → 按可用家）。

### 4.3 JcCell（竞彩胜平负）

同 X1x2Cell 的取值/来源字段（无返还率、凯利）。现网只有 `odds_jc_home` 主胜一项（`complete=false`）。

### 4.4 LiveEntry（即时盘口 / last_prematch）

`book, market(asian|euro_1x2|ou), label(rule_1110|null), target_at, recorded_at(变化点起点=该报价首次记录), valid_until(变化点终点), line(亚盘已统一为正=主让), home_water, away_water, home, draw, away, over_water, under_water, water_source, is_inplay, source`；`last_prematch` 另带 `minutes_before_kickoff`、`stale`。

## 5. 阶段取值规则（术语文档 + collection_schedule）

| 阶段 | 规则 | 取数（优先级） |
|---|---|---|
| `open` 初盘 | **各家公司各自最早一条**，`recorded_at` = 该家实际开盘时间（不统一时刻） | `odds_snapshot(rule/open)` → `(actual/open)` → 旧表 `odds_asian` / `odds_euro_home` / `odds_jc_home` 的 `open`（`legacy_import`，时间未知） |
| `mid` 中盘（规则） | 非例外场 = 赛前 8h；例外场 = 竞彩日 15:00 | `odds_snapshot(rule/mid)`（≤目标时刻的最后一条，导入时已按 as-of 取）→ 旧表 `mid` |
| `close` 临盘（规则） | 非例外场 = 赛前 1h；例外场 = 竞彩日 22:00（只对例外场，不是每场都 22:00） | `odds_snapshot(rule/close)` → 旧表 `close` |
| `mid_real` 中盘（真实） | 仅例外场：赛前 8h，as-of | `odds_snapshot(actual/t8)`；无旧表回退 |
| `close_real` 临盘（真实） | 仅例外场：赛前 1h，as-of | `odds_snapshot(actual/t1)`；无旧表回退 |
| `live[label=rule_1110]` 即时（11:10） | 竞彩日 11:10，≤11:10 的最后一条 | `odds_timeline_seg`（每家每玩法一条） |
| `live[label=null]` 即时 | 比赛结束前任一时刻的 API 盘口（含赛中，`is_inplay`） | `odds_timeline_seg` 全部变化点（`include_live=all`） |
| `last_prematch` | 开赛前最后一条即时快照（开赛后的不算） | `odds_timeline_seg`（非 in-play）∪ 有真实报价时刻的 `odds_snapshot`，取 `recorded_at` 最大且 ≤ min(开赛, as_of) |

- **例外场判定（0.3.16）**：`collection_schedule.is_phase_exception(kickoff_hour, minute)` = 开赛 ∈ **[23:00, 次日 11:30]，两端都含**（与竞彩日 cutoff 一致；分钟用 `kickoff_minute_known=1` 时的 `kickoff_at` 分钟，分钟不明按整点 0–11 / 23）。场次级 `phase_exception: true|false`。现网 177 场中 **143** 场为例外场。
- **规则时刻（0.3.16）**：`collection_schedule.rule_targets` —— 例外场 = 竞彩日 15:00 / 22:00；其它 = `kickoff_at − 8h / − 1h` **精确到分钟**（`kickoff_minute_known=0` → 整点）。`schedule.phase_target = "exact_minute"`。真实时刻：`collection_schedule.actual_targets`（`schedule.*_real_target_time`，只在例外场给）。
- **结算 / 返还率 / 凯利默认用规则值**（`open/mid/close`）；`*_real` 只给原始盘口、水位、赔率（术语文档没写 `*_real` 是否也要算返还率/凯利 → **待定**）。
- **即时（11:10）与 V3**：按协作中决定，V3 方案的阈值和旧手工数据对应的是**竞彩日 11:10 前后的盘**，所以拿 V3 做对照时请用 `live[label=rule_1110]`，不要用 `open`（`open` 已改为各家最早一条，可能早好几天）。现网没有时间线表，`rule_1110` 在现网为空数组；v2d3 副本 175/177 场有。
- **last_prematch 新鲜度**（2026-10-08 定、配置 `LAST_PREMATCH_FRESH_MINUTES = 15`）：`minutes_before_kickoff ∈ [0,15]` → `stale=false`；`> 15` → `stale=true`（**仍返回该条**）；开赛后的快照不参与。门槛为分析师 v0，**待按平博快照密度复核**（见术语文档 CLV 节）。注意：时间线是「变化点」压缩，`recorded_at` 是该报价**首次**出现的时刻；盘口长时间不变时会被判 stale，`valid_until` 可辅助判断。
- **CLV 三列不在后端算**：本接口提供 `close` / `close_real` / `last_prematch`（含 `stale`）三组原始值，CLV 计算方式术语文档未给公式，前端或后续版本再定。

## 6. 返还率基准与凯利（hl_v0 §4–§6）

- 基准 = 按 `(market, book, phase)` 分组的**滚动中位数**；样本 = 库内全部场（所有 scope，不受本次日期/scope/分页影响）中**竞彩日严格早于本场竞彩日**的同组返还率（`bisect_left` 实现；本日及之后不进基准 —— `test_baseline_uses_only_strictly_earlier_days` 把本日及之后的水位改成极端值，基准不变）。
- 排除：亚盘 `water_censored=true`、**旧手工档位中点水位（`water_source=tier_midpoint`，0.3.16 起不计入）**、欧赔三项不全、**欧赔 `api_closing`（0.3.16 起不进任何计算）**。
- **`baseline_method`（0.3.16，每个返还率格子）**：`empirical` = 该公司×阶段历史中位数（真实水位样本 ≥ `min_n`=20）；`fixed_fallback` = 样本不足 → `return_rate_baseline=null`，前端套 `config.return_rate_fallback`（= hl_v0.1 §4/§5 兜底绝对值：亚盘澳/皇/威 <0.93 轻、<0.91 中、>0.97 轻；平博 <0.955 轻、<0.94 中；欧赔澳/皇/威 <0.90 轻、<0.88 中；平博 <0.95 轻、<0.93 中）。现网皇冠/威廉只有档位中点水位 → 现网全部 `fixed_fallback`。
- **凯利多家平均（0.3.16）**：某家的 `kelly_multi_avg` 以及 `kelly_base=multi_avg` 时（平博自身 / 缺平博）一律用**其他几家**的平均（不含本家），格子 `n_avg` = 参与平均的家数；只有本家一家时凯利留空。`x1x2_base.*.multi_avg`（「多家平均（参考）」列）仍含全部机构（`includes_self=true`，`n_avg=n_books`）。
- **欧赔 `api_closing`（0.3.16）**：不进 `close`、返还率、基准、凯利、多家平均、`n_avg`；仅在赛果可见后以 `x1x2[book].api_closing`（`label=api_closing`、`display_name=收盘（时间未知）`、`reference_only=true`）单独返回，赛前嵌套里没有这个键（flat 列恒在、赛前全 null）。`api_opening` 见 §13「初盘 open_basis」。
- 窗口：缺省「本场竞彩日之前全部」（扩张窗口；分析师 2026-10-08 确认），`baseline_window_days` 可设滚动天数；`min_n` 缺省 20（确认）。
- 兜底绝对阈值由前端按 `baseline_method=fixed_fallback` 套用 `config.return_rate_fallback`，后端不做高亮。
- 凯利见 §4.2。凯利只按同一阶段（open/mid/close）、同一时间点的各家报价算。

## 7. 每场结算（复用现有逻辑，不改默认）

- 取数与 `/strategies/{id}/validate`（`_load_strategy_bets`）同源：`odds_asian` 的 `settle_book`（V3 = `macau_close`）盘口与水位；**不读快照**。
- 计算：`backtest.resolve_juice` + `backtest.settle_pnl`；`settlement_version` 缺省 = 现网默认 **`ah_v4_water_midpoint`**（澳门固定 …，`juice_source=fixed_macau`）；可选 `ah_v4_macau_actual_or_0…`（现网澳门无水位 → 全部 `fallback` …，`juice_reason=water_missing`）。
- 份数：`predictions.stake` 有值用之（`stake_units_source=prediction`），否则按 `DEFAULT_STAKE_RULE.default_units=…`（`default_units`）。现网 V3 … 场 `stake` 全空 → 全部按 … 份。
- `pnl_units` = 份数 × (赢权重 × juice − 输权重)，**单位是「份」**，与资金曲线无关（金额依赖逐日资金曲线，属于 validate/compare，不在逐行表里给）。
- `code`：`win | win_half | push | lose_half | lose | no_bet`（与 `backtest.settle_code` 同值、与前端 `SettleCode` 同名；方向「不下注」= `no_bet`，pnl …）；`source="backend"`。前端估算结算可弃用。

## 8. 防泄漏

1. 赛果：`as_of ≥ 开赛 + 3h`（对齐赛果入库「开赛 +3 小时主拉」）**且** `results` 比分齐全才返回 `result` 和 `settlement`；否则 `null` + `result_hidden_reason`/`hidden_reason`：`before_kickoff`（未开赛）→ `not_visible_yet`（开赛后 3h 内）→ `no_result`（无比分）；开赛时刻未知 → `kickoff_unknown`。即使赛果行提前写入了比分，未到时点也不返回（测试 `test_unfinished_match_has_no_result_or_settlement`）。
2. 预测：显式传 `as_of` 且早于 `produced_at` → `prediction=null`、`prediction_hidden_reason=produced_after_as_of`。缺省 as_of=now 时不影响。`produced_before_kickoff` 如实标注（现网 V3 177 场全部是 2026-10-05/06 回补产出 → 全为 false，属已拍板的 splice 回补，不是本接口问题）。
3. 盘口：格子的 `recorded_at`（旧表用规则目标时刻；`api_closing` 用开赛时刻）晚于 `as_of` → 隐藏（`hidden_reason=after_as_of`）。
4. 返还率基准：只用竞彩日严格早于本场的数据（见 §6）。
5. 只读：连接 `file:...app.db?mode=ro`；测试 `test_connection_is_read_only` 证明写会报错，`test_prod_db_untouched` 比对现网文件 sha256 前后一致。

## 9. 扁平列映射（`format=flat`，AG Grid columnDefs 用）

- 扁平键 = 嵌套路径用 `_` 连接（`match.*` 去前缀直接放顶层）；为 null 的子对象（非例外场 `*_real`、无记录 `last_prematch`、无结算等）也展开成全 null 列，**每行列集合一致**（测试 `test_flat_columns_stable_*`）。
- 也可以不用 flat：nested 下 AG Grid `field` 支持点号路径，如 `field: 'ah.macau.close.line'`，与下表一一对应（把 `_` 换回 `.` 的层级）。
- 数组字段原样保留：`live`、`prediction_rationale`、`x1x2_base_{phase}_multi_avg_books`、`multi_avg_prob_books`。
- 占位：`{book}` ∈ macau / crown / william / pinnacle；`{phase}` ∈ open / mid / close；`{real_phase}` ∈ mid_real / close_real。每行共 958 列（含 4 家展开）。

| 扁平键模式 | 中文列头建议 / 说明 |
|---|---|
| `match_id` | 比赛 ID（match_uid） |
| `match_pk` | 内部数字 id |
| `jc_id` | 竞彩编号 |
| `jc_no` | 竞彩序号 |
| `jingcai_date` | 竞彩日 |
| `kickoff_at` | 开赛时间(+08:00) |
| `kickoff_hour` | 开赛小时 |
| `kickoff_minute_known` | 开赛分钟已知 |
| `scope` | jingcai/extra |
| `league` | 联赛 |
| `league_canonical` | 联赛规范名 |
| `competition_type` | 赛事类型 |
| `home_team` | 主队 |
| `away_team` | 客队 |
| `home_team_canonical` | 主队规范名 |
| `away_team_canonical` | 客队规范名 |
| `phase_exception` | 例外场 |
| `schedule_phase_exception` | 阶段目标时刻（schedule） |
| `schedule_open_target_time` | 阶段目标时刻（schedule） |
| `schedule_mid_target_time` | 阶段目标时刻（schedule） |
| `schedule_close_target_time` | 阶段目标时刻（schedule） |
| `schedule_mid_real_target_time` | 阶段目标时刻（schedule） |
| `schedule_close_real_target_time` | 阶段目标时刻（schedule） |
| `schedule_live_rule_1110_target_time` | 阶段目标时刻（schedule） |
| `schedule_source` | 阶段目标时刻（schedule） |
| `ah_{book}_{phase}_line` | 亚盘：line |
| `ah_{book}_{phase}_home_water` | 亚盘：home_water |
| `ah_{book}_{phase}_away_water` | 亚盘：away_water |
| `ah_{book}_{phase}_water_source` | 亚盘：water_source |
| `ah_{book}_{phase}_water_censored` | 亚盘：water_censored |
| `ah_{book}_{phase}_recorded_at` | 亚盘：recorded_at |
| `ah_{book}_{phase}_target_at` | 亚盘：target_at |
| `ah_{book}_{phase}_basis` | 亚盘：basis |
| `ah_{book}_{phase}_source` | 亚盘：source |
| `ah_{book}_{phase}_available` | 亚盘：available |
| `ah_{book}_{phase}_hidden_reason` | 亚盘：hidden_reason |
| `ah_{book}_{phase}_minutes_since_open` | 亚盘：minutes_since_open |
| `ah_{book}_{phase}_return_rate` | 亚盘：return_rate |
| `ah_{book}_{phase}_return_rate_baseline` | 亚盘：return_rate_baseline |
| `ah_{book}_{phase}_return_rate_baseline_n` | 亚盘：return_rate_baseline_n |
| `ah_{book}_{phase}_return_rate_dev` | 亚盘：return_rate_dev |
| `ah_{book}_{real_phase}_line` | 亚盘（真实，例外场）：line |
| `ah_{book}_{real_phase}_home_water` | 亚盘（真实，例外场）：home_water |
| `ah_{book}_{real_phase}_away_water` | 亚盘（真实，例外场）：away_water |
| `ah_{book}_{real_phase}_water_source` | 亚盘（真实，例外场）：water_source |
| `ah_{book}_{real_phase}_water_censored` | 亚盘（真实，例外场）：water_censored |
| `ah_{book}_{real_phase}_recorded_at` | 亚盘（真实，例外场）：recorded_at |
| `ah_{book}_{real_phase}_target_at` | 亚盘（真实，例外场）：target_at |
| `ah_{book}_{real_phase}_basis` | 亚盘（真实，例外场）：basis |
| `ah_{book}_{real_phase}_source` | 亚盘（真实，例外场）：source |
| `ah_{book}_{real_phase}_available` | 亚盘（真实，例外场）：available |
| `ah_{book}_{real_phase}_hidden_reason` | 亚盘（真实，例外场）：hidden_reason |
| `ah_{book}_{real_phase}_minutes_since_open` | 亚盘（真实，例外场）：minutes_since_open |
| `ah_{book}_last_prematch_book` | 开赛前最后一条即时快照：book |
| `ah_{book}_last_prematch_market` | 开赛前最后一条即时快照：market |
| `ah_{book}_last_prematch_label` | 开赛前最后一条即时快照：label |
| `ah_{book}_last_prematch_target_at` | 开赛前最后一条即时快照：target_at |
| `ah_{book}_last_prematch_recorded_at` | 开赛前最后一条即时快照：recorded_at |
| `ah_{book}_last_prematch_valid_until` | 开赛前最后一条即时快照：valid_until |
| `ah_{book}_last_prematch_line` | 开赛前最后一条即时快照：line |
| `ah_{book}_last_prematch_home_water` | 开赛前最后一条即时快照：home_water |
| `ah_{book}_last_prematch_away_water` | 开赛前最后一条即时快照：away_water |
| `ah_{book}_last_prematch_home` | 开赛前最后一条即时快照：home |
| `ah_{book}_last_prematch_draw` | 开赛前最后一条即时快照：draw |
| `ah_{book}_last_prematch_away` | 开赛前最后一条即时快照：away |
| `ah_{book}_last_prematch_over_water` | 开赛前最后一条即时快照：over_water |
| `ah_{book}_last_prematch_under_water` | 开赛前最后一条即时快照：under_water |
| `ah_{book}_last_prematch_water_source` | 开赛前最后一条即时快照：water_source |
| `ah_{book}_last_prematch_is_inplay` | 开赛前最后一条即时快照：is_inplay |
| `ah_{book}_last_prematch_source` | 开赛前最后一条即时快照：source |
| `ah_{book}_last_prematch_minutes_before_kickoff` | 开赛前最后一条即时快照：minutes_before_kickoff |
| `ah_{book}_last_prematch_stale` | 开赛前最后一条即时快照：stale |
| `x1x2_{book}_{phase}_home` | 欧赔：home |
| `x1x2_{book}_{phase}_draw` | 欧赔：draw |
| `x1x2_{book}_{phase}_away` | 欧赔：away |
| `x1x2_{book}_{phase}_complete` | 欧赔：complete |
| `x1x2_{book}_{phase}_recorded_at` | 欧赔：recorded_at |
| `x1x2_{book}_{phase}_fetched_at` | 欧赔：fetched_at |
| `x1x2_{book}_{phase}_target_at` | 欧赔：target_at |
| `x1x2_{book}_{phase}_basis` | 欧赔：basis |
| `x1x2_{book}_{phase}_source` | 欧赔：source |
| `x1x2_{book}_{phase}_available` | 欧赔：available |
| `x1x2_{book}_{phase}_hidden_reason` | 欧赔：hidden_reason |
| `x1x2_{book}_{phase}_minutes_since_open` | 欧赔：minutes_since_open |
| `x1x2_{book}_{phase}_return_rate` | 欧赔：return_rate |
| `x1x2_{book}_{phase}_return_rate_baseline` | 欧赔：return_rate_baseline |
| `x1x2_{book}_{phase}_return_rate_baseline_n` | 欧赔：return_rate_baseline_n |
| `x1x2_{book}_{phase}_return_rate_dev` | 欧赔：return_rate_dev |
| `x1x2_{book}_{phase}_kelly_home` | 欧赔：kelly_home |
| `x1x2_{book}_{phase}_kelly_draw` | 欧赔：kelly_draw |
| `x1x2_{book}_{phase}_kelly_away` | 欧赔：kelly_away |
| `x1x2_{book}_{phase}_kelly_base` | 欧赔：kelly_base |
| `x1x2_{book}_{phase}_kelly_base_n_books` | 欧赔：kelly_base_n_books |
| `x1x2_{book}_{phase}_kelly_multi_avg_home` | 欧赔：kelly_multi_avg_home |
| `x1x2_{book}_{phase}_kelly_multi_avg_draw` | 欧赔：kelly_multi_avg_draw |
| `x1x2_{book}_{phase}_kelly_multi_avg_away` | 欧赔：kelly_multi_avg_away |
| `x1x2_{book}_{phase}_multi_avg_n_books` | 欧赔：multi_avg_n_books |
| `x1x2_{book}_{real_phase}_home` | 欧赔（真实，例外场）：home |
| `x1x2_{book}_{real_phase}_draw` | 欧赔（真实，例外场）：draw |
| `x1x2_{book}_{real_phase}_away` | 欧赔（真实，例外场）：away |
| `x1x2_{book}_{real_phase}_complete` | 欧赔（真实，例外场）：complete |
| `x1x2_{book}_{real_phase}_recorded_at` | 欧赔（真实，例外场）：recorded_at |
| `x1x2_{book}_{real_phase}_fetched_at` | 欧赔（真实，例外场）：fetched_at |
| `x1x2_{book}_{real_phase}_target_at` | 欧赔（真实，例外场）：target_at |
| `x1x2_{book}_{real_phase}_basis` | 欧赔（真实，例外场）：basis |
| `x1x2_{book}_{real_phase}_source` | 欧赔（真实，例外场）：source |
| `x1x2_{book}_{real_phase}_available` | 欧赔（真实，例外场）：available |
| `x1x2_{book}_{real_phase}_hidden_reason` | 欧赔（真实，例外场）：hidden_reason |
| `x1x2_{book}_{real_phase}_minutes_since_open` | 欧赔（真实，例外场）：minutes_since_open |
| `x1x2_{book}_last_prematch_book` | 开赛前最后一条即时快照：book |
| `x1x2_{book}_last_prematch_market` | 开赛前最后一条即时快照：market |
| `x1x2_{book}_last_prematch_label` | 开赛前最后一条即时快照：label |
| `x1x2_{book}_last_prematch_target_at` | 开赛前最后一条即时快照：target_at |
| `x1x2_{book}_last_prematch_recorded_at` | 开赛前最后一条即时快照：recorded_at |
| `x1x2_{book}_last_prematch_valid_until` | 开赛前最后一条即时快照：valid_until |
| `x1x2_{book}_last_prematch_line` | 开赛前最后一条即时快照：line |
| `x1x2_{book}_last_prematch_home_water` | 开赛前最后一条即时快照：home_water |
| `x1x2_{book}_last_prematch_away_water` | 开赛前最后一条即时快照：away_water |
| `x1x2_{book}_last_prematch_home` | 开赛前最后一条即时快照：home |
| `x1x2_{book}_last_prematch_draw` | 开赛前最后一条即时快照：draw |
| `x1x2_{book}_last_prematch_away` | 开赛前最后一条即时快照：away |
| `x1x2_{book}_last_prematch_over_water` | 开赛前最后一条即时快照：over_water |
| `x1x2_{book}_last_prematch_under_water` | 开赛前最后一条即时快照：under_water |
| `x1x2_{book}_last_prematch_water_source` | 开赛前最后一条即时快照：water_source |
| `x1x2_{book}_last_prematch_is_inplay` | 开赛前最后一条即时快照：is_inplay |
| `x1x2_{book}_last_prematch_source` | 开赛前最后一条即时快照：source |
| `x1x2_{book}_last_prematch_minutes_before_kickoff` | 开赛前最后一条即时快照：minutes_before_kickoff |
| `x1x2_{book}_last_prematch_stale` | 开赛前最后一条即时快照：stale |
| `x1x2_base_{phase}_pinnacle_home` | 凯利基准概率：pinnacle_home |
| `x1x2_base_{phase}_pinnacle_draw` | 凯利基准概率：pinnacle_draw |
| `x1x2_base_{phase}_pinnacle_away` | 凯利基准概率：pinnacle_away |
| `x1x2_base_{phase}_multi_avg_home` | 凯利基准概率：multi_avg_home |
| `x1x2_base_{phase}_multi_avg_draw` | 凯利基准概率：multi_avg_draw |
| `x1x2_base_{phase}_multi_avg_away` | 凯利基准概率：multi_avg_away |
| `x1x2_base_{phase}_multi_avg_n_books` | 凯利基准概率：multi_avg_n_books |
| `x1x2_base_{phase}_multi_avg_books` | 凯利基准概率：multi_avg_books |
| `multi_avg_prob_home` | 多家平均去水概率（规则临盘）：home |
| `multi_avg_prob_draw` | 多家平均去水概率（规则临盘）：draw |
| `multi_avg_prob_away` | 多家平均去水概率（规则临盘）：away |
| `multi_avg_prob_n_books` | 多家平均去水概率（规则临盘）：n_books |
| `multi_avg_prob_books` | 多家平均去水概率（规则临盘）：books |
| `jc_1x2_{phase}_home` | 竞彩胜平负：home |
| `jc_1x2_{phase}_draw` | 竞彩胜平负：draw |
| `jc_1x2_{phase}_away` | 竞彩胜平负：away |
| `jc_1x2_{phase}_complete` | 竞彩胜平负：complete |
| `jc_1x2_{phase}_recorded_at` | 竞彩胜平负：recorded_at |
| `jc_1x2_{phase}_fetched_at` | 竞彩胜平负：fetched_at |
| `jc_1x2_{phase}_target_at` | 竞彩胜平负：target_at |
| `jc_1x2_{phase}_basis` | 竞彩胜平负：basis |
| `jc_1x2_{phase}_source` | 竞彩胜平负：source |
| `jc_1x2_{phase}_available` | 竞彩胜平负：available |
| `jc_1x2_{phase}_hidden_reason` | 竞彩胜平负：hidden_reason |
| `prediction_direction` | V3 预测：direction |
| `prediction_strategy` | V3 预测：strategy |
| `prediction_settle_book` | V3 预测：settle_book |
| `prediction_confidence` | V3 预测：confidence |
| `prediction_stake` | V3 预测：stake |
| `prediction_stake_rule` | V3 预测：stake_rule |
| `prediction_produced_at` | V3 预测：produced_at |
| `prediction_updated_at` | V3 预测：updated_at |
| `prediction_message_sent_at` | V3 预测：message_sent_at |
| `prediction_produced_before_kickoff` | V3 预测：produced_before_kickoff |
| `prediction_hidden_reason` | V3 预测：hidden_reason |
| `produced_at` | 预测产出时间 |
| `as_of` | 接口截止时间 |
| `result_home_goals` | 赛果：home_goals |
| `result_away_goals` | 赛果：away_goals |
| `result_total_goals` | 赛果：total_goals |
| `result_score` | 赛果：score |
| `result_wdl` | 赛果：wdl |
| `result_hidden_reason` | 赛果：hidden_reason |
| `hidden_reason` | 赛果/结算为空原因 |
| `settlement_settlement_version` | 结算：settlement_version |
| `settlement_settle_book` | 结算：settle_book |
| `settlement_line` | 结算：line |
| `settlement_side` | 结算：side |
| `settlement_juice` | 结算：juice |
| `settlement_juice_source` | 结算：juice_source |
| `settlement_juice_reason` | 结算：juice_reason |
| `settlement_stake_units` | 结算：stake_units |
| `settlement_stake_units_source` | 结算：stake_units_source |
| `settlement_code` | 结算：code |
| `settlement_pnl_units` | 结算：pnl_units |
| `settlement_source` | 结算：source |
| `settlement_hidden_reason` | 结算：hidden_reason |
| `prediction_rationale` | V3 理由（数组） |
| `live` | 即时盘口（数组，不展开） |

## 10. 现网字段覆盖率（`data/app.db`，2026-06-01～07-05，177 场；对照 v2d3 副本）

> 由本接口实际返回逐格统计（`as_of`=now、默认参数）。「v2d3」列是副本 `data/v2d3/app.db` 用同一代码跑出来的结果，**不是现网**，用来说明「v2d3 赔率同步进现网后会有哪些值」。现网 `odds_snapshot` / `odds_timeline_seg` 表不存在。

例外场（`phase_exception=true`）：现网 **143/177**（开赛 ≥23:00 或早场带 [00:00,11:30]）。

### 10.1 亚盘

| 公司.阶段 | 盘口 现网 | 水位 现网 | 水位来源 现网 | recorded_at 现网 | 返还率 现网 | 基准非空 现网 | 盘口/水位/返还率 v2d3 |
|---|---|---|---|---|---|---|---|
| macau.open | 177 | 0 | null:177 | 0 | 0 | 0 | 177/175/175 |
| macau.mid | 0 | 0 | null:177 | 0 | 0 | 0 | 175/175/175 |
| macau.close | 177 | 0 | null:177 | 0 | 0 | 0 | 177/175/175 |
| macau.mid_real（仅例外场 143） | 0 | — | — | — | 不算 | — | 141 |
| macau.close_real（仅例外场 143） | 0 | — | — | — | 不算 | — | 141 |
| macau.last_prematch | 0（stale=false: 0） | | | | | | 175（stale=false: 70） |
| crown.open | 177 | 177 | tier_midpoint:177 | 0 | 177 | 155 | 177/177/177 |
| crown.mid | 177 | 177 | tier_midpoint:177 | 0 | 176 | 155 | 177/177/176 |
| crown.close | 177 | 177 | tier_midpoint:177 | 0 | 177 | 155 | 177/177/177 |
| crown.mid_real（仅例外场 143） | 0 | — | — | — | 不算 | — | 0 |
| crown.close_real（仅例外场 143） | 0 | — | — | — | 不算 | — | 0 |
| crown.last_prematch | 0（stale=false: 0） | | | | | | 0（stale=false: 0） |
| william.open | 176 | 176 | tier_midpoint:176, null:1 | 0 | 129 | 138 | 176/176/129 |
| william.mid | 176 | 176 | tier_midpoint:176, null:1 | 0 | 137 | 138 | 176/176/137 |
| william.close | 176 | 176 | tier_midpoint:176, null:1 | 0 | 138 | 138 | 176/176/138 |
| william.mid_real（仅例外场 143） | 0 | — | — | — | 不算 | — | 0 |
| william.close_real（仅例外场 143） | 0 | — | — | — | 不算 | — | 0 |
| william.last_prematch | 0（stale=false: 0） | | | | | | 0（stale=false: 0） |
| pinnacle.open | 0 | 0 | null:177 | 0 | 0 | 0 | 0/0/0 |
| pinnacle.mid | 0 | 0 | null:177 | 0 | 0 | 0 | 0/0/0 |
| pinnacle.close | 0 | 0 | null:177 | 0 | 0 | 0 | 0/0/0 |
| pinnacle.mid_real（仅例外场 143） | 0 | — | — | — | 不算 | — | 0 |
| pinnacle.close_real（仅例外场 143） | 0 | — | — | — | 不算 | — | 0 |
| pinnacle.last_prematch | 0（stale=false: 0） | | | | | | 0（stale=false: 0） |

要点：
- **澳门亚盘：现网盘口有（open/close 177），水位 0/177，没有 mid**（三时点里只有初/临）。v2d3 副本有真实水位 175/177、mid 175/177、例外场真实时点 141/143。
- **皇冠/威廉亚盘：现网有三时点盘口与水位**，但水位全部是旧手工档位换算的中点（`tier_midpoint`），无 `recorded_at`；威廉 176/177（缺 1 场）。威廉截断档位多（open 47 / mid 39 / close 38 格 `water_censored`），这些格不算返还率。v2d3 里皇冠/威廉仍是同一批旧数据（快照只有 1 场探针）。
- **平博：现网与 v2d3 都没有任何数据** → 全部 `available=false`、`hidden_reason=no_data`，响应顶层 `data_sources.books_without_data=["pinnacle"]`。
- 返还率基准非空数 < 返还率数：首几天历史不足 `min_n=20`。

### 10.2 欧赔 1X2 / 竞彩

| 公司.阶段 | 主胜有值 现网 | 三项齐 现网 | 凯利 现网 | 三项齐/凯利 v2d3 | v2d3 basis |
|---|---|---|---|---|---|
| macau.open | 177 | 0 | 0 | 175/175 | api_opening, legacy_import |
| macau.mid | 0 | 0 | 0 | 0/0 | — |
| macau.close | 177 | 0 | 0 | 175/175 | api_closing, legacy_import |
| crown.open | 0 | 0 | 0 | 175/175 | api_opening |
| crown.mid | 0 | 0 | 0 | 0/0 | — |
| crown.close | 0 | 0 | 0 | 175/175 | api_closing |
| william.open | 177 | 0 | 0 | 175/175 | api_opening, legacy_import |
| william.mid | 0 | 0 | 0 | 0/0 | — |
| william.close | 177 | 0 | 0 | 175/175 | api_closing, legacy_import |
| pinnacle.open | 0 | 0 | 0 | 0/0 | — |
| pinnacle.mid | 0 | 0 | 0 | 0/0 | — |
| pinnacle.close | 0 | 0 | 0 | 0/0 | — |
| 竞彩 jc.open | 176 | 0 | — | 0/— | |
| 竞彩 jc.mid | 0 | 0 | — | 0/— | |
| 竞彩 jc.close | 176 | 0 | — | 0/— | |

要点：
- **现网欧赔只有主胜一项**（澳门、威廉的 open/close，`odds_euro_home`），平/负为空 → 返还率、凯利、多家平均全部 null。皇冠、平博欧赔现网没有。竞彩只有主胜（`odds_jc_home` 176/177），让球胜平负表 0 行。
- v2d3 副本有澳门/皇冠/威廉完整 1X2 175/177，但来自 5DF `/odds` 的开盘/收盘价（`basis=api_opening/api_closing`），**报价时刻未知**；其中 `close` 是 API 收盘价，并非「赛前 1h」临盘 —— 同步进现网前需要拍板（见 §12）。没有 1X2 中盘。

### 10.3 预测 / 赛果 / 结算

| 项 | 现网 |
|---|---|
| V3 预测 | 177/177（方向：{'不下注': 165, '主': 8, '客': 4}） |
| 份数 `stake` | 0/177（全空 → 结算按默认 1 份） |
| 置信度 | 12/177 |
| `produced_before_kickoff=true` | 0/177（全部为 2026-10-05/06 回补） |
| 赛果可见 | 168/177（`no_result` 9 场：06-28 日073、07-05 日091/092/201–206） |
| 结算 | {'no_bet': …, 'win': …, 'lose': …, 'win_half': …}；`pnl_units` 合计 … 份（默认 `ah_v4_water_midpoint`，澳门固定 …） |
| 即时（11:10） | 现网 0（无时间线表）；v2d3 175/177 |


## 11. 哪些字段要等 v2d3 赔率同步进现网才会有值

（现网 `DUAL_WRITE_ODDS_ASIAN` 关，`odds_snapshot` / `odds_timeline_seg` 未进现网；本接口代码已能读这两张表，表一出现就自动优先用快照、旧表兜底，**不需要再改接口**。）

| 字段 | 现网 | v2d3 同步后 | 备注 |
|---|---|---|---|
| `ah.macau.{open,mid,close}.home_water/away_water/water_source/recorded_at/return_rate(_baseline)` | 空（mid 连盘口都没有） | 175/177 | 澳门真实水位来自 5DF macauslot 历史 |
| `ah.macau.{mid_real,close_real}` | 空 | 141/143 例外场 | |
| `ah.*.last_prematch`、`x1x2.*.last_prematch` | 空 | 澳门亚盘 175（新鲜 70）；欧赔 0 | 需时间线 |
| `live[]`（含 `rule_1110`） | `[]` | 175/177（仅澳门亚盘有完整时间线） | |
| `x1x2.{macau,crown,william}.{open,close}` 平/负 + 返还率 + 凯利 + `multi_avg` | 空 | 175/177 | **收盘价报价时刻未知**（`api_closing`），见 §12-9 |
| `x1x2.*.mid`、`x1x2.*.{mid_real,close_real}` | 空 | 空 | v2d3 也没有 1X2 时间线 |
| `jc_1x2` 平/负 | 空 | 仅 1 场探针 | 竞彩 SP 需另找源（sporttery） |
| 皇冠/威廉真实水位、`recorded_at`、`*_real` | 旧档位中点、无时间 | 同现网（仅 1 场探针） | 需另外补采 |
| **平博（ah 与 x1x2 全部）** | 无 | **无** | 两库都没有，须新开采集；在此之前凯利主基准恒退回 `multi_avg` |

## 12. 差异与待拍板

**已解决（0.3.16，2026-10-08，依据术语文档「例外场范围」「目标时刻与几处默认值」「补充」三节）**

| 原条目 | 决议 | 0.3.16 实现 |
|---|---|---|
| 1 例外场范围 | [23:00, 次日 11:30] 两端都含 | `is_phase_exception`；与 0.3.15 行为一致，现网例外场 143/177，**变化 0 场** |
| 2 带分钟开赛丢分钟 | T−8h/T−1h 精确到分钟；分钟不明按整点 | `rule_targets` / `calc_collection_times`；现网 mid/close 目标变化 13 场（10 场带分钟 + 3 场 day_skew，见 ACCEPTANCE） |
| 3 v2d3 快照 t8/t1 按整点 | v2d3 用补分钟后的开赛时间重导，指纹 `phase_target=exact_minute`，旧导出 `hour_floor` 留对照 | `scripts/reexport_phase_snapshots_exact_minute.py`；700 行重导 |
| 7 基准参数 | 本场之前全部、≥20、公司×阶段；**档位中点水位不计入**；不足 20 用固定兜底阈值 | `baseline_method`、`config.return_rate_fallback` |
| 8 多家平均含本家 | 参考列含全部；「某家 vs 平均」一律不含本家，存 `n_avg` | `kelly_multi_avg` / `kelly(multi_avg)` leave-one-out |
| 9 v2d3 1X2 `api_closing` | 不当临盘、不进返还率/凯利/高亮；完赛后单独「收盘（时间未知）」 | `x1x2[book].api_closing` |
| 10 `*_real` 算不算 | 只给原始值（同意） | 不变 |
| 13 澳门 mid 正负号 | v2d3 源头统一正数=主让 + 入库前校验（单场 + 来源×阶段 20% 整批） | v2d3 163 行取反（12 行平手不变）；`scripts/validate_ah_sign.py`；同步/补数脚本入库前调用 |

验收：`$ODDS_DATA_DIR/backfill/table-12-decisions-2026-10-08/ACCEPTANCE.md`。下面是 0.3.15 时的原文（留档）。


**A. 本接口按现有代码 / 协作中结论实现、但与文字口径有出入（不自创，列出待拍板）**

1. **例外场范围**：按 `collection_schedule.is_rule_fixed_clock` = 开赛 ≥23:00 **或早场特殊带 [00:00,11:30]**（有分钟时；仅整点则 0–11 点）。术语文档/hl_v0 文字是「≥23:00（含次日凌晨）」，没提到上午到 11:30 的场 —— 例如次日 10:00 开赛的场，规则中盘=竞彩日 15:00、规则临盘=竞彩日 22:00（距开赛约 12h）。现网 143/177 被判为例外场。
2. **非例外场带分钟的开赛**：`calc_collection_times` 写的是 `f"{h-8:02d}:00"` / `f"{h-1:02d}:00"`，**丢掉分钟**。如 18:30 开赛 → 中盘目标 10:00（=赛前 8.5h）、临盘 17:00（=赛前 1.5h），文字口径是赛前 8h/1h。
3. **v2d3 快照的 t8/t1 目标也按整点算**：如 2026-06-01|一004 开赛 02:45，快照 `actual/t1.target_at`=01:00（应为 01:45）。本接口格子里的 `target_at` 显示快照实际用的目标，`schedule.close_real_target_time` 显示按现 `kickoff_at` 精确算的 01:45，两者会对不上；需要用补齐分钟后的开赛时间重导快照（不在本次范围）。同理时间线 `valid_until` 截在旧整点开赛时刻（该场 02:00）。
4. **初盘**：已按用户拍板 = 各家最早一条（`basis=first_record`），**不是** 11:10；11:10 改为 `live[label=rule_1110]`。现网旧手工 `open` 的采集时刻未知（`legacy_import`）。
5. **现网旧数据的时钟**：现网 `odds_asian`/`odds_euro_home` 的 `mid/close` 是旧手工采集口径（D2 笔记：语义≈`rule_legacy`，早场 16:00/23:00），不是现规则 15:00/22:00。接口照样放在 `mid/close`，但标 `basis=legacy_import`、`target_at=null`、`recorded_at=null`，不冒充规则时刻。
6. **last_prematch 边界**：术语文档写 `0 < minutes_before_kickoff ≤ 15`，协作中最新指令是 `[0,15]` → 已按 `[0,15]` 实现（恰好开赛时刻那条算新鲜）。另外时间线是变化点压缩，盘口长时间不变时最后一个变化点会很早，`stale` 会偏多；门槛待分析师按平博快照密度复核。

**B. hl_v0 / 术语文档没写、需要拍板的参数（当前缺省值）**

7. 返还率基准**窗口长度**（缺省：本场竞彩日之前全部）与**最少样本**（缺省 20）；分组按「公司 × 阶段」还是只按公司（缺省按公司 × 阶段）；旧档位中点水位是否计入（缺省计入，精度 0.05 一档）。
8. **多家平均**是否包含被评估公司本身：hl_v0 原文「四家各自去水再平均」→ 缺省包含；所以平博自己的凯利（用多家平均）里也含平博自己。是否改为「去掉本家」待定。
9. **v2d3 的 1X2 收盘价**是 5DF `/odds` 的 closing（报价时刻未知、不是赛前 1h），接口放在 `close` 但标 `basis=api_closing`、`recorded_at=null`、`fetched_at`=抓取时刻，并参与返还率/凯利。是否接受，还是要求 1X2 也走时间线 as-of 后再算。
10. `*_real` 是否也算返还率/凯利（术语文档没写）→ 当前**只给原始盘口/水位/赔率**。
11. 赛果可见时点 = 开赛 + 3h（对齐赛果入库主拉）。
12. CLV 三列（规则/真实/收盘，平博为准）后端未计算，只给原始值；公式与是否后端算待定。平博无数据前 CLV 也算不出。

**C. 发现的数据问题（只读发现，未改任何库）**

13. **亚盘盘口正负号两套记法**：现网 `odds_asian.handicap` 正数=主让（结算也按此）；`odds_snapshot`/`odds_timeline_seg`（5DF API）负数=主让（295 格取反后一致；另 30 格因时点不同数值不同）。本接口已统一为正=主让。**风险**：v2d3 副本的 `odds_asian` 里澳门 `mid` 177 行（来源 `5df_macauslot_history`）是 API 记法，而同场 `open/close` 是旧记法 —— 同一张表混了两套正负号。若把 v2d3 的 `odds_asian` 原样同步进现网，中盘盘口方向会反；同步脚本需要先取反。
14. V3 177 场 `stake` 全空、`produced_at` 全在开赛之后（回补）→ 结算全按 1 份；显式传早于 2026-10-05 的 `as_of` 时 V3 预测会被隐藏（`produced_after_as_of`），符合铁律但回放旧日期时会看到空预测。

## 12.1 0.3.18 第二批口径（已实现）

| 项 | 决议 | 实现 |
|---|---|---|
| A 例外场 / 竞彩日归属 | 按编号判；有编号：属 D 且开赛 ≥ D 23:00（无上限）→ 例外；无编号：[D 23:00, D+1 11:30] | `exception_rule=jc_code_ge_2300`（旧 `time_window_2300_1130`）；`schedule.exception_rule`、`config.exception_rule`；探针导入按编号星期推竞彩日；现网例外 143→146，v2d3 144→147（六008/二020/六036，v2d3 重导 rule mid/close） |
| B 五201/四201/日092 | 澳门场中首笔 + 公开赛程均支持 5DF → 保留 5DF；另存 `kickoff_jc` | v2d3 `matches.kickoff_jc`；API `match.kickoff_jc`（现网无列 → null） |
| C N1 威廉收盘全是 api_closing | hist 不可评估 `close_unusable`；validate 按 `close_basis` 分账 | `by_close_basis{rule_tick,api_closing,unknown}` + `close_basis_split`；api_closing 组 `ledger_note`「用到了时间未知的收盘价，只作历史参考」；冻结 N1 不动 |
| D odds_asian ≠ 快照 | 以 exact_minute 快照为准 | `scripts/resync_odds_asian_from_snapshot.py`：close … / open … / mid …；S1-V2 重跑 n=… ROI …（不变） |
| E 欧赔 water_source | 直接报价 actual（含已核实旧手工）、档位换算 tier_midpoint、未知 null；基准 real_only | `x1x2.{book}.{phase}.water_source`、`jc_1x2.{phase}.water_source`；`config.x1x2_return_hl_water=real_only` |
| F §2 水位异动 | 只认 actual；档位格不上色，跨档悬停 | `ah.{book}.{mid,close}.water_move_eligible`、`tier_cross{from,to,sides}`；方案计算默认排除 tier_midpoint |

**v2d3 只读实例（0.3.18）**：`app/db.py` 读环境变量——`APP_DB_PATH`（不设 = `data/app.db`）、`APP_DB_LABEL`（`live`｜`v2d3`，不设 = live）、`APP_READONLY=1`。只读模式下 sqlite 连接一律 `mode=ro`；**所有 POST / PUT / PATCH / DELETE 一律 403**（`{"detail": "read-only instance ...", "meta": {...}}`），含会写缓存的 `POST /strategies/{id}/validate`。选 403 而不是「不写缓存的纯计算」：不依赖逐个接口改造，以后新加的写接口也自动挡住；连接层 `mode=ro` 再兜底。`APP_DB_LABEL=v2d3` 必须同时 `APP_READONLY=1`，且不能指向现网库，否则启动即报错。`/health`、`/table/matches` 响应带 `meta={db, promoted, readonly}`：现网 `db=live, promoted=true`，副本 `db=v2d3, promoted=false`。启动（现网 8787 命令不变）：
`APP_DB_PATH=data/v2d3/app.db APP_DB_LABEL=v2d3 APP_READONLY=1 nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8788 > logs/uvicorn-v2d3-0.3.18.log 2>&1 &`

N5 快照强制 `rho_global` / `rho_league` / `rho_league_se` + 指纹 `rho=global_pooled_ivw`、`rho_se_method=profile_kish`（见 `v2_0-n5-poisson-spec-v1.md（未公开）` 末节）。

## 12.2 0.3.19 推迟场 / S2-V2·N4-V2 / 人工复核（已实现）

| 项 | 决议 | 实现 |
|---|---|---|
| 推迟场字段 | 存 `kickoff_original` / `kickoff_actual` / `postponed_announced_at` | v2d3 `matches` 加列（另 `postpone_ts_unknown`、`postpone_void_check`、`postpone_evidence`）；API match 级同名 + `postponed`、`postpone_delay_minutes`。`kickoff_at` 仍是实际开赛（赛果 / 结算 / 时间线照实际）。现网无列 → 全 null / false |
| 目标时刻 | 只用到该时刻为止已公布的开赛时间 | `collection_schedule.known_kickoff_target`：t = 原定 − Δ；公告时刻 ≤ t → 新开赛 − Δ（`announced_new`）；公告晚于 t → t（`original`）；查不到 → t + `postpone_ts_unknown=true`（`original_ts_unknown`，进日核对）。`schedule.postpone_target_basis{mid,close}` |
| 例外场 | 按编号判，用 `kickoff_original` | `schedule.kickoff_for_exception`；例外场规则列 D 15:00/22:00 不受推迟影响，（真实）列走上面的规则；非例外场规则列也走上面的规则 |
| 日092 | 原定 08:00、实际 09:00、07:10 公布 | 规则列 07-05 15:00 / 22:00；（真实）列 07-06 00:00 / 07:00（basis 都是 original）。v2d3 快照重导 actual t8/t1 两行（01:00→00:00、08:00→07:00；t1 盘值 0.76/1.08 → 0.74/1.10），rule 两行不变；结算照实际赛果（推迟恰 1h，不 pending） |
| 推迟 > 1h | `postpone_void_check=pending`，暂不结算 | `/table/matches` 行 `settlement=null`、`settlement_hidden_reason=postpone_void_pending`；validate 不进 bets / n_eligible / hits，单独计 `n_postpone_pending` + `postpone_items[]`（不算 n_not_evaluable） |
| void_postponed 钩子 | 澳门时限核实前不写死 | `collection_schedule.POSTPONE_VOID_HOURS=None`（不生效）；summary 恒给 `postpone_void_hours`；写死后：超时场 validate 计 `n_void_postponed`、表格 `settlement.code=void_postponed`、`pnl_units=null`，且 `postpone_void_hours` 进验证指纹（null 时不进，旧指纹不变） |
| kickoff_jc_conflict | `kickoff_jc` 与 `kickoff_at` 差 ≥ 90 分钟 → true | 读时计算；v2d3 1 场（五201，150 分钟）；现网无 `kickoff_jc` → null |
| 日核对 | ts 未知 / pending / 冲突 / 人工复核 | match 级 `daily_check[]` ∈ `postpone_ts_unknown`｜`postpone_void_pending`｜`kickoff_jc_conflict`｜`manual_review` |
| S2-V2 / N4-V2 | 新 key，只认真实水位，hist | `scripts/generate_shadow_s2n4_v2.py`（不走 seed）；v2d3 S2-V2 19 行、N4-V2 0 行（只挂定义）；指纹 extras `water_rule=real_water_only`、`odds_source=hist`、`postpone_void_hours=null` |
| 旧 S2 / N4 | 冻结，旧 key 不再追加；旧条目备注 | `app/ledger_registry.py`：生成器 / import_lib 写 SHADOW_S2 / SHADOW_N4 一律 `FrozenLedgerError`（`generate_shadow_predictions.py` 默认 `--only S8`，S2/N4 只能 `--dry-run`）。备注读时加：`prediction.ledger_note="触发依据是换算水位"`、`ledger_note_reason=tier_water_trigger`（N4 全部 13 条；S2 中 b 分支 2 条：五030、一044）；`/table/matches`、`/matches/{id}/prediction(s)` 都返回；不改库、不改预测 |
| 探针 209/210 | 人工复核，不进特征和结算 | v2d3 `matches.manual_review=1`、`manual_review_reason=ah_sign_mismatch_probe`；生成器 `match_ids` 排除；表格不进返还率基准，有预测时 `settlement_hidden_reason=manual_review`；validate 不可评估原因 `manual_review`（子原因 `ah_sign_mismatch_probe`） |
| 即时（11:10）合并（追加 1，18:15 口径） | 时间线表之外，把 odds_snapshot 里 `capture=own`、`odds_source=live` 的 11:10 行并进来；两边都有以自采为准 | `live[]` 里 `label=rule_1110` 的条目（`merge_rule=own_capture_first;alt=timeline`）：同一场、同一 (book, market)，自采行和时间线表（开始 ≤ 竞彩日 11:10 的最后一段）都有 → 用自采值，时间线值放 `alt={line, water, tick_at}`（`water` 亚盘 `{home,away}`、大小球 `{over,under}`、1X2 `{home,draw,away}`；`line` 为 API 记法）；只有时间线 → 照旧用时间线，`alt=null`；只有自采 → 用自采，`alt=null`。每格带 `origin`（timeline｜own_capture）、`odds_source`（hist｜live）、`capture`（own｜null）、`captured_at`（实际抓取 / 报价时刻）、`fetch_lag_min`（自采：captured_at − 11:10，自采晚到几分钟也照用）。两边盘口不同或任一边水位差 > 0.03 → 该格 `instant_src_diff=true`，`match.instant_src_diff=true` 并进 `match.daily_check`（不论 `include_live`），响应 `daily_check_summary` 计数；全量清单 `scripts/daily_check_report.py`。两边都保留、谁也不覆盖。`alt` 只对照，不参与升降盘判断、不进策略计算。as-of：11:10 ≤ as_of 才给；自采 captured_at > as_of 不用。现网无 odds_snapshot、v2d3 目前 0 行自采 11:10，所以眼下数值不变 |
| N5 每竞彩日 ρ 异质性（追加，18:15） | 快照新增 `rho_Q`、`rho_df`、`rho_I2`、`rho_Q_p`，指纹 `rho_pool=fe｜re_dl`（Q 的 p<0.05 用 DerSimonian–Laird） | 与 0.3.18 `rho_global` 同处强制（`_check_n5_rho_day`）：新快照必须带；`rho_I2` 按小数 0–1；`rho_Q_p<0.05 ⇔ re_dl`；不可评估行可 null；旧快照不改。见 `v2_0-n5-poisson-spec-v1.md（未公开）`「API 0.3.19」节 |
| 高亮 hl_v0.3（追加，18:19；只管上色） | 凯利三档 +0.02；返还率兜底按公司真实水位 P25（as-of，n<100 不上色）；水位异动统一比初→临，初临不同盘 = tier_cross，同盘但中盘换过 = tier_cross_mid | `config_version=hl_v0.3`；`config.kelly_highlight`、`config.return_rate_fallback_hl_v03`；ah/x1x2 格 `fallback_p25`/`fallback_n`/`fallback_hl_eligible`；ah close 格 `water_move_eligible`（初→临）/`tier_cross{kind:line｜water_tier, from{phase,line,home,away}, to, sides}`/`tier_cross_mid`，open/mid 格恒 null。见高亮规则文档「hl_v0.3 后端字段」 |
| 补数让路（追加 2） | 北京 11:05–11:20、14:55–15:15、21:55–22:15 让路；共享 （账户上限），分析师采集最多 24 | `app/shared_api_yield.py`（标准库）：时段内 `before_request()` 睡到时段结束；补数合计 ≤ 16 次/分钟（跨进程 flock 账本 `odds-data/5dollar/backfill_yield_ledger.json`）；响应头剩余 ≤ 24 停到 Reset（缺省 61 秒）；事件记 `odds-data/5dollar/backfill_yield.jsonl`。已接入 7 个补数脚本（见 ACCEPTANCE），分析师的 `live_capture.py` 不接（它是被让的一方） |
| 缓存 | 状态变了要重算 | v2d3 有人工复核 / 推迟场时 `data_rev` 末尾追加 `sp:<hash>` 段（`data_rev_source` 带 `+match_special`）；现网不变 |

## 13. 前端名 → 后端名对照（前端 `src/sheet/types.ts` / `adapter.ts`）

- **路由前缀**：前端请求 `/api/table/matches`，Vite 代理把 `/api` 去掉（`vite.config` `rewrite: path.replace(/^\/api/, '')`）→ 后端实际路由是 **`/table/matches`**，后端不加 `/api`，和现有全部路由一致。直连 8787 时用 `/table/matches`。
- 已按前端名对齐（与本文档早先草稿相比的**破坏性改名**，前端 `Be*` 类型与 `fromBackendRow` 需同步）：

| 前端名 | 后端（0.3.15 最终） | 早先草稿名 | 说明 |
|---|---|---|---|
| `target_at`（格子 / live） | `target_at` | `target_time` | `schedule.*_target_time` 保留原名（前端 `ApiSchedule` 用的就是这个） |
| `water_source` | `water_source` ∈ `actual｜tier_midpoint｜null` | 值曾为 `real` | `fallback_095` 不会出现在盘口格；回落只体现在 `settlement.juice_source=fallback`（默认口径为 `fixed_macau`） |
| `label` | `live[].label`（`rule_1110`｜null）、`last_prematch.label`(null) | 同 | 格子（open/mid/close）不带 label |
| `mid_real` / `close_real` | 同名；非例外场为 null | 同 | |
| `phase_exception` | 同名（行级，`schedule` 里也有一份） | 同 | |
| `hidden_reason` | 格子级 `hidden_reason`（`after_as_of`｜`no_data`）；行级 `hidden_reason` = `result_hidden_reason` | 行级原无 | 代码：`before_kickoff`｜`not_visible_yet`｜`no_result`｜`kickoff_unknown`（前端 `hiddenReasonCn` 已能识别前三个） |
| `settlement.code` | `settlement.code` ∈ `win｜win_half｜push｜lose_half｜lose｜no_bet` | `result_code`（`half_win/half_loss/loss`） | 与前端 `SettleCode` 同值，adapter 的 `SETTLE_CODE` 映射可删 |
| `settlement.pnl_units` | 同名（单位：份） | 同 | |
| `settlement.source` | `"backend"` | 无 | 前端估算结算可弃用 |
| `settlement_version`（行级） | `settlement.settlement_version`（行内）；响应顶层也有 | 同 | |
| `x1x2.kelly` / `kelly_base` | 同名；`kelly_base` ∈ `pinnacle｜multi_avg` | `consensus` | |
| `multi_avg_prob` | 行级 `multi_avg_prob{home,draw,away,n_books,books}` = `x1x2_base.close.multi_avg` | 无 | 各阶段在 `x1x2_base.{phase}.multi_avg`；参考列凯利 `kelly_multi_avg`、家数 `multi_avg_n_books` |
| `last_prematch.minutes_before_kickoff` / `stale` | 同名，在 `ah.{book}.last_prematch` / `x1x2.{book}.last_prematch` | 同 | |
| `ApiAhBook.live` | 行级 `live[]`（带 `book`、`market`） | 同 | **保留不同**：一个数组同时装亚盘/欧赔/大小球，flat 时只占一列；前端 adapter 已按 book 归位 |
| `match.id/date/home/away/jc_no(字符串)` | `match_id`、`match.jingcai_date`、`home_team`、`away_team`、`jc_id`（字符串）/`jc_no`（整数） | 同 | **保留不同**：与库表 / `/matches` 列名一致，且 `jc_no` 在库里是整数序号，改名会和前端的字符串 `jc_no` 冲突；adapter 已映射。另补了 `match.kickoff_hour` |
| `result.wdl` | 同名；另有 `score`（`"1-0"`）、`total_goals` | 同 | |
| （0.3.16 新）`baseline_method` | 每个 open/mid/close 返还率格子：`empirical`｜`fixed_fallback` | 无 | `fixed_fallback` 时 `return_rate_baseline=null`，阈值读 `config.return_rate_fallback`（hl_v0.1 §4/§5） |
| （0.3.16 新）`n_avg` | 欧赔格子：`kelly_multi_avg`（及 `kelly_base=multi_avg` 的 `kelly`）所用「其他几家」的家数 | 无 | 参考列 `x1x2_base.*.multi_avg` 含全部，另有 `n_avg`=`n_books`、`includes_self=true` |
| （0.3.16 新）`x1x2.{book}.api_closing` | `{label:"api_closing", display_name:"收盘（时间未知）", home, draw, away, fetched_at, reference_only:true, quote_time_known:false}`；**仅赛果可见后**出现 | 原在 `close`（`basis=api_closing`） | 不进 `close`、返还率、凯利、多家平均、高亮；灰字「参考」 |
| （0.3.16 新）初盘 `open_basis` | `ah/x1x2.{book}.open` 与 `jc_1x2.open`：`first_tick`｜`api_opening`｜`legacy_import` | 无 | 见下 |
| （0.3.16 新）`earliest_ts_quote_at` | 同上 open 对象：本家本玩法最早一笔**带时间戳**报价（含我们自抓的 11:10），无则 null | 无 | |
| （0.3.16 新）`usable_at_mid` / `usable_at_close` / `unusable_reason` | 同上 open 对象 | 无 | 前端只读这两个标记，不自己判断 |
| （0.3.17 新）`open_basis_reason` | 同上 open 对象：`open_basis=null` 时 `no_open_data`（该家该玩法无初盘，如现网平博亚盘、皇冠/平博欧赔、竞彩缺 1 场）｜`after_as_of`；有 open_basis 时为 null | 无 | 每个 open 对象（四家 × 亚盘/欧赔 + 竞彩）都带 `open_basis` + `open_basis_reason` |
| （0.3.17 新）`ts_inferred` | 同上 open 对象：`legacy_import` 的 earliest 为推定的竞彩日 11:10 → true；first_tick / api_opening → false；无初盘 → null | 无 | |
| （0.3.17 新）`match.kickoff_source` / `match.kickoff_placeholder` | 行级（`schedule` 里同值，另有 `schedule.kickoff_check`）：`5df`｜`jingcai`｜`jingcai_hour_synth`；`5df_1200`｜null | 无 | 见下「开赛占位符」 |
| （0.3.17 改）`baseline_method` | 返还率为 null 的格子 → `baseline_method=null` | 0.3.16 恒有值 | |
| （0.3.17 说明）`n_avg` | 只在算了凯利（`kelly` / `kelly_multi_avg`）的欧赔格子有值；现网欧赔只有主胜赔（`complete=false`）→ 无凯利 → `n_avg` 全 null（v2d3 有完整 1X2 的 525 格均有值） | — | 亚盘的偏离是对本家×阶段历史基准（家数看 `return_rate_baseline_n`），没有「多家平均」比较，所以亚盘不设 `n_avg` |

| （0.3.18 新）`schedule.exception_rule` / `config.exception_rule` | `jc_code_ge_2300`（有编号）｜`time_window_2300_1130`（无编号）；config 为服务端规则名 | 旧时间窗 | 例外场：有编号开赛 ≥ D 23:00（无上限）；无编号 [D 23:00, D+1 11:30] |
| （0.3.18 新）`match.kickoff_jc` | 竞彩官方开赛时刻（整点 ISO）；现网无列 → null；v2d3 177/179 有值 | 无 | 对照用；`kickoff_at` 不变 |
| （0.3.18 新）`x1x2.{book}.{phase}.water_source` / `jc_1x2.{phase}.water_source` | `actual`｜`tier_midpoint`｜null | 无 | 直接报价 / 已核实旧手工 = actual；档位换算 = tier_midpoint；不可见 / 未知 = null。欧赔基准只收 actual |
| （0.3.18 新）`ah.{book}.{mid,close}.water_move_eligible` | true（与上一阶段同盘且两格 actual）｜false（任一格非 actual）｜null（盘口变 / 不可见，不判） | 无 | §2 异动上色只在 true 时做；open / `*_real` 恒 null |
| （0.3.18 新）`ah.{book}.{mid,close}.tier_cross` | `{from:{phase,home,away}, to:{phase,home,away}, sides:["home"\|"away"...]}`（档位 t，水位=0.70+0.05t）｜null | 无 | 两格都是 tier_midpoint、同盘、档位变了才给；悬停「跨档：X→Y（档位换算，幅度不精确）」，不上色、不进方案。flat：`ah_{book}_{phase}_tier_cross_from_home` 等，null 时全 null |
| （0.3.18 新）`config.x1x2_return_hl_water` / `config.water_move_rule` | `real_only` / 规则文案 | 无 | |
| （0.3.18 新）`meta.db` / `meta.promoted` | `live`｜`v2d3`；现网 `promoted=true`，副本 `false` | 无 | `/health`、`/table/matches` 必带 |
| （0.3.19 新）`match.postponed` | bool；`kickoff_actual ≠ kickoff_original` | 无 | flat `postponed` |
| （0.3.19 新）`match.kickoff_original` / `kickoff_actual` / `postponed_announced_at` | ISO +08:00；非推迟场 / 现网 null | 无 | `kickoff_at` = 实际开赛（不变） |
| （0.3.19 新）`match.postpone_ts_unknown` | 查不到公告时刻 → true（目标按原定算，进日核对）；非推迟场 null | 无 | |
| （0.3.19 新）`match.postpone_void_check` | `pending`（推迟 > 1h，暂不结算）｜null | 无 | 行 `settlement_hidden_reason=postpone_void_pending` |
| （0.3.19 新）`match.postpone_delay_minutes` | 实际 − 原定（分钟）；非推迟场 null | 无 | |
| （0.3.19 新）`schedule.kickoff_for_exception` / `schedule.postpone_target_basis{mid,close}` | 推迟场：原定开赛；`original`｜`announced_new`｜`original_ts_unknown`；其它场 null | 无 | flat `schedule_postpone_target_basis_mid/close`（null 时也有列） |
| （0.3.19 新）`match.kickoff_jc_conflict` | `kickoff_jc` 与 `kickoff_at` 相差 ≥ 90 分钟 → true；无 kickoff_jc → null | 无 | 悬停「竞彩时刻与开赛时间不一致，待日核对」 |
| （0.3.19 新）`match.manual_review` / `manual_review_reason` | bool / `ah_sign_mismatch_probe`｜null | 无 | 不进特征、结算、基准 |
| （0.3.19 新）`match.daily_check` | `[]` 或 `postpone_ts_unknown`｜`postpone_void_pending`｜`kickoff_jc_conflict`｜`manual_review`｜`instant_src_diff` | 无 | 日核对清单；flat 为数组 |
| （0.3.19 新）`prediction.ledger_note` / `ledger_note_reason` | 旧冻结 S2/N4「触发依据是换算水位」/ `tier_water_trigger`；其它 null | 无 | 悬停用；flat `prediction_ledger_note` |
| （….1… 新）`settlement_hidden_reason` 新值 | `manual_review`｜`postpone_void_pending` | 无 | `settlement.code` 新值 `void_postponed`（钩子，时限写死后才出现，`pnl_units=null`） |
| （0.3.19 新）`live[].origin` / `odds_source` / `capture` / `captured_at` / `fetch_lag_min` / `merge_rule` | 只在 `label=rule_1110` 条目上有值：`timeline`｜`own_capture`；`hist`｜`live`；`own`｜null；ISO +08:00；分钟；`own_capture_first;alt=timeline` | 无 | 前端按 `origin`/`odds_source` 标来源 |
| （0.3.19 新）`live[].alt` | `{line, water, tick_at}`｜null：自采和时间线都有时的时间线值（`water` 亚盘 `{home,away}`、大小球 `{over,under}`、1X2 `{home,draw,away}`） | 无 | 只做对照（悬停），不参与升降盘、不进策略 |
| （0.3.19 新）`live[].instant_src_diff` / `match.instant_src_diff` | 盘口不同或水位差 > 0.03 → true；两路都有且一致 → false；没有两路同时有数 → null | 无 | 进 `match.daily_check`；flat `instant_src_diff` |
| （0.3.19 新）响应顶层 `daily_check_summary` | `{scope:"page", n_matches, by_reason{...}, instant_src_diff_cells_in_live}`（本页） | 无 | 全量用 `scripts/daily_check_report.py` |
| （0.3.19 新）validate summary | `n_postpone_pending`、`n_void_postponed`、`postpone_items[]`、`postpone_void_hours`；`n_not_evaluable_by_reason.manual_review` | 无 | 推迟场不算不可评估，单独计数 |
| （0.3.19 新，hl_v0.3）`ah/x1x2.{book}.{open|mid|close}.fallback_p25` / `fallback_n` / `fallback_hl_eligible` | 本公司真实水位返还率 P25（亚盘/欧赔分开、三阶段合并、竞彩日严格早于本场）/ 样本数 / n≥100 | 无 | `fallback_hl_eligible=false` → 兜底不上色、灰字「样本不足，暂不判断」 |
| （0.3.19 改，hl_v0.3）`ah.{book}.close.water_move_eligible` | 初→临同盘：两格 actual → true，否则 false；不同盘 / 不可见 → null。**mid 格恒 null**（不再比中→临） | 无 | |
| （0.3.19 改，hl_v0.3）`ah.{book}.close.tier_cross` | `{kind: line｜water_tier, from{phase,line,home,away}, to{…}, sides}`；line = 初/临盘口不同（sides=["line"]）；water_tier = 同盘档位跨档 | 无 | 只悬停，不上色；flat 增 `_kind`、`_from_line`、`_to_line` 列 |
| （0.3.19 新，hl_v0.3）`ah.{book}.close.tier_cross_mid` | 初/临同盘但中盘换过盘 → true；同盘中盘没换 / 不可见 → false；其它 null | 无 | 照常比、照常上色；悬停「中盘曾换盘，已回到初盘盘口」 |
| （0.3.19 新，hl_v0.3）`config.kelly_highlight` / `config.return_rate_fallback_hl_v03` / `config.hl` | 凯利 margin 0.02（轻 kelly−返还率≥0.02、中 kelly≥1.02、重 null）；兜底规则说明；`v0.3` | 无 | hl_v0.4 分位数阈值未上 |

### 初盘 `open_basis`（0.3.16，术语文档「api_opening 的处理」）

- **取值**：本家有 ≤ `as_of` 的带时间戳赛前报价（API 历史 tick：时间线变化点 ∪ 有真实报价时刻的快照）→ 取最早一笔当初盘，`open_basis=first_tick`、`basis=first_record`、`recorded_at`=该笔时刻；只有 5DF `/odds` 的 opening → `open_basis=api_opening`（`recorded_at=null`，`fetched_at` 只是抓取时刻）；现网旧手工 open → `open_basis=legacy_import`。我们自抓的「即时（11:10）」只算进 `earliest_ts_quote_at`，**不当初盘**。
- **可用性**：`first_tick` → `usable_at_mid = usable_at_close = true`。`api_opening` 的开盘时刻**未知，不保证早于决策点** → `usable_at_X = (earliest_ts_quote_at ≤ schedule.X_target_time)`（含端），否则 `false` 且 `unusable_reason="open_time_unknown_after_decision_possible"`。`legacy_import`（0.3.17，分析师决策 4 + 用户更正，分析师已确认）→ `earliest_ts_quote_at` 推定为该竞彩日 11:10、`ts_inferred=true`（本家若有更早的真实带时间戳报价则用真实值、`ts_inferred=false`），`usable_at_X` 与 api_opening **同一规则**（earliest ≤ 该阶段 target_at，含端），**不一刀切 true**。结果：非例外场开赛 ∈ [11:30, 19:10) → `usable_at_mid=false`；其中 [11:30, 12:10) 另 `usable_at_close=false`；例外场（15:00/22:00）两段都 true。现网 2026-06 全部 177 场：亚盘 open 530 格 mid true 488 / false 42、close 全 true；欧赔 open 354 格 mid 326 / 28、close 全 true；竞彩 open 176 格 mid 162 / 14、close 全 true（mid=false 的是 14 场开赛早于 19:10 的非例外场；现网无 [11:30,12:10) 同日开赛场）。
- **时长类字段**：`api_opening` 的开盘时刻未知，`minutes_since_open` 等一律 null，不拿抓取时间顶替，也不用默认跨度估。
- **展示 vs 特征**：本接口里 `api_opening` 的初盘仍参与**展示**用的返还率、凯利、多家平均和高亮输入（只读页面）。但**任何特征 / 影子策略 / 验证**要用初盘或由初盘派生的值（升降盘、初→中/临变化等），**必须先过 `usable_at_mid` / `usable_at_close`**（按决策阶段选一个），为 false 的场按初盘不可用处理；结果按 `open_basis` 分组另出对照。
- 前端悬停文案（任一为 false）：「初盘时间未知，可能晚于决策时点，不参与特征计算」；`api_opening` 开盘时间显示「时间未知」+ 灰色小字「API」。

### 开赛占位符 `kickoff_source` / `kickoff_placeholder`（0.3.17，0316 follow-up 决策 1）

- 只在 5DF 开赛**恰为 12:00** 时检查，对竞彩官方时刻（优先级：`match_meta.extras.jingcai_kickoff_at` 完整时刻 > `extras.jingcai_kickoff_hour` > `matches.kickoff_hour` 整点）：
  - 不一致 → 用竞彩时刻：`kickoff_source=jingcai`、`kickoff_placeholder=5df_1200`，mid/close 目标按竞彩时刻重算；竞彩只有整点时分钟未知 → `schedule.phase_target=hour_floor`、`kickoff_minute_known=false`。
  - 没有竞彩时刻 → 保留 5DF 日期与 12 点，分钟按未知（`hour_floor`），`kickoff_placeholder=5df_1200`。
  - 竞彩整点 = 12 → 一致（竞彩整点不带自然日，不按合成日期判冲突），`kickoff_source=5df`。
- `schedule.kickoff_check` ∈ `not_applicable`｜`agrees_jingcai`｜`agrees_jingcai_hour`｜`placeholder_use_jingcai`｜`placeholder_no_jingcai`。读时生效（现网不写库）；导入前同规则校验：`scripts/validate_kickoff_placeholder.py`。
- 现网 / v2d3：0 场触发。3 场 day_skew（06-13 六008、06-16 二020、06-20 六036）竞彩官方为「12 点」（`imports/2026/2606.json`），与 5DF 次日 12:00 一致；澳门 in-play tick 从次日 12:00–12:03 开始、公开赛程（如澳大利亚–土耳其北京时间 6/14 12:00）均印证 5DF 正确 → 目标不变，exact_minute 快照无需重导。

## 14. JSON 示例

### 14.1 现网真实一行（`data/app.db`，`2026-06-17|三204`，例外场、V3 主、赢）

`GET /table/matches?date_from=2026-06-17&date_to=2026-06-17`，取 `items` 中该场（`as_of` 为生成文档时刻）：

```json
{
 "match_id": "2026-06-17|三204",
 "match": {
  "match_id": "2026-06-17|三204",
  "match_pk": 85,
  "jc_id": "三204",
  "jc_no": 204,
  "jingcai_date": "2026-06-17",
  "kickoff_at": "2026-06-18T02:00:00+08:00",
  "kickoff_hour": 2,
  "kickoff_minute_known": true,
  "scope": "jingcai",
  "league": "芬超",
  "league_canonical": "芬超",
  "competition_type": "联赛",
  "home_team": "格尼斯坦",
  "away_team": "拉赫蒂",
  "home_team_canonical": "格尼斯坦",
  "away_team_canonical": "拉赫蒂"
 },
 "phase_exception": true,
 "schedule": {
  "phase_exception": true,
  "open_target_time": null,
  "mid_target_time": "2026-06-17T15:00:00+08:00",
  "close_target_time": "2026-06-17T22:00:00+08:00",
  "mid_real_target_time": "2026-06-17T18:00:00+08:00",
  "close_real_target_time": "2026-06-18T01:00:00+08:00",
  "live_rule_1110_target_time": "2026-06-17T11:10:00+08:00",
  "source": "app.collection_schedule (rule) + actual_targets (real)"
 },
 "ah": {
  "macau": {
   "open": {
    "line": 0.0,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_asian",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null
   },
   "mid": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null
   },
   "close": {
    "line": 0.0,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_asian",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null
   },
   "mid_real": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": "2026-06-17T18:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "close_real": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": "2026-06-18T01:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "last_prematch": null
  },
  "crown": {
   "open": {
    "line": 0.0,
    "home_water": 0.825,
    "away_water": 1.05,
    "water_source": "tier_midpoint",
    "water_censored": false,
    "recorded_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_asian",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": 0.965484,
    "return_rate_baseline": 0.954248,
    "return_rate_baseline_n": 77,
    "return_rate_dev": 0.011236
   },
   "mid": {
    "line": 0.0,
    "home_water": 0.8,
    "away_water": 1.05,
    "water_source": "tier_midpoint",
    "water_censored": false,
    "recorded_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_asian",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": 0.958442,
    "return_rate_baseline": 0.955882,
    "return_rate_baseline_n": 77,
    "return_rate_dev": 0.00256
   },
   "close": {
    "line": 0.0,
    "home_water": 0.825,
    "away_water": 1.05,
    "water_source": "tier_midpoint",
    "water_censored": false,
    "recorded_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_asian",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": 0.965484,
    "return_rate_baseline": 0.961039,
    "return_rate_baseline_n": 77,
    "return_rate_dev": 0.004445
   },
   "mid_real": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": "2026-06-17T18:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "close_real": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": "2026-06-18T01:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "last_prematch": null
  },
  "william": {
   "open": {
    "line": 0.0,
    "home_water": 0.9,
    "away_water": 0.75,
    "water_source": "tier_midpoint",
    "water_censored": false,
    "recorded_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_asian",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": 0.910959,
    "return_rate_baseline": 0.924324,
    "return_rate_baseline_n": 61,
    "return_rate_dev": -0.013365
   },
   "mid": {
    "line": 0.0,
    "home_water": 0.7,
    "away_water": 0.95,
    "water_source": "tier_midpoint",
    "water_censored": true,
    "recorded_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_asian",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": 0.910959,
    "return_rate_baseline_n": 65,
    "return_rate_dev": null
   },
   "close": {
    "line": 0.0,
    "home_water": 0.7,
    "away_water": 0.925,
    "water_source": "tier_midpoint",
    "water_censored": true,
    "recorded_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_asian",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": 0.910959,
    "return_rate_baseline_n": 61,
    "return_rate_dev": null
   },
   "mid_real": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": "2026-06-17T18:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "close_real": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": "2026-06-18T01:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "last_prematch": null
  },
  "pinnacle": {
   "open": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null
   },
   "mid": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null
   },
   "close": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null
   },
   "mid_real": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": "2026-06-17T18:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "close_real": {
    "line": null,
    "home_water": null,
    "away_water": null,
    "water_source": null,
    "water_censored": null,
    "recorded_at": null,
    "target_at": "2026-06-18T01:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "last_prematch": null
  }
 },
 "x1x2": {
  "macau": {
   "open": {
    "home": 2.3,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_euro_home(home_only)",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "mid": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "close": {
    "home": 2.3,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_euro_home(home_only)",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "mid_real": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": "2026-06-17T18:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "close_real": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": "2026-06-18T01:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "last_prematch": null
  },
  "crown": {
   "open": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "mid": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "close": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "mid_real": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": "2026-06-17T18:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "close_real": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": "2026-06-18T01:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "last_prematch": null
  },
  "william": {
   "open": {
    "home": 2.7,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_euro_home(home_only)",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "mid": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "close": {
    "home": 2.4,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": "legacy_import",
    "source": "odds_euro_home(home_only)",
    "available": true,
    "hidden_reason": null,
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "mid_real": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": "2026-06-17T18:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "close_real": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": "2026-06-18T01:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "last_prematch": null
  },
  "pinnacle": {
   "open": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "mid": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "close": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": null,
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null,
    "return_rate": null,
    "return_rate_baseline": null,
    "return_rate_baseline_n": 0,
    "return_rate_dev": null,
    "kelly": null,
    "kelly_base": null,
    "kelly_base_n_books": null,
    "kelly_multi_avg": null,
    "multi_avg_n_books": null
   },
   "mid_real": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": "2026-06-17T18:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "close_real": {
    "home": null,
    "draw": null,
    "away": null,
    "complete": false,
    "recorded_at": null,
    "fetched_at": null,
    "target_at": "2026-06-18T01:00:00+08:00",
    "basis": null,
    "source": null,
    "available": false,
    "hidden_reason": "no_data",
    "minutes_since_open": null
   },
   "last_prematch": null
  }
 },
 "x1x2_base": {
  "open": {
   "pinnacle": null,
   "multi_avg": null
  },
  "mid": {
   "pinnacle": null,
   "multi_avg": null
  },
  "close": {
   "pinnacle": null,
   "multi_avg": null
  }
 },
 "multi_avg_prob": null,
 "jc_1x2": {
  "open": {
   "home": 2.25,
   "draw": null,
   "away": null,
   "complete": false,
   "recorded_at": null,
   "fetched_at": null,
   "target_at": null,
   "basis": "legacy_import",
   "source": "odds_jc_home(home_only)",
   "available": true,
   "hidden_reason": null
  },
  "mid": {
   "home": null,
   "draw": null,
   "away": null,
   "complete": false,
   "recorded_at": null,
   "fetched_at": null,
   "target_at": null,
   "basis": null,
   "source": null,
   "available": false,
   "hidden_reason": "no_data"
  },
  "close": {
   "home": 2.31,
   "draw": null,
   "away": null,
   "complete": false,
   "recorded_at": null,
   "fetched_at": null,
   "target_at": null,
   "basis": "legacy_import",
   "source": "odds_jc_home(home_only)",
   "available": true,
   "hidden_reason": null
  }
 },
 "live": [],
 "prediction": {
  "direction": "主",
  "strategy": "CFFXDJ_5_V3",
  "settle_book": "macau_close",
  "rationale": [
   "bucket=SUM=+3",
   "dir_sum=3",
   "FMAAH_V2=-1",
   "FSLREG_V2_2=1",
   "FAWLE_V1=1",
   "FWAHB_V2=1",
   "FCAHB_V2=1"
  ],
  "confidence": 0.6,
  "stake": null,
  "stake_rule": null,
  "produced_at": "2026-10-06 18:43:55",
  "updated_at": null,
  "message_sent_at": null,
  "produced_before_kickoff": false
 },
 "prediction_hidden_reason": null,
 "produced_at": "2026-10-06 18:43:55",
 "as_of": "2026-10-08T17:12:14.827899+08:00",
 "result": {
  "home_goals": 1,
  "away_goals": 0,
  "total_goals": 1,
  "score": "1-0",
  "wdl": "胜"
 },
 "result_hidden_reason": null,
 "hidden_reason": null,
 "settlement": {
  "settlement_version": "ah_v4_water_midpoint",
  "settle_book": "macau_close",
  "line": 0.0,
  "side": "主",
  "juice": 0.95,
  "juice_source": "fixed_macau",
  "juice_reason": null,
  "stake_units": …,
  "stake_units_source": "default_units",
  "code": "win",
  "pnl_units": …,
  "source": "backend"
 },
 "settlement_hidden_reason": null
}
```

### 14.2 v2d3 副本同一场的片段（**非现网**，演示快照 / 时间线有数据时的形态）

`ah.macau`（含 `mid_real`/`close_real`/`last_prematch`）、`x1x2.macau.close`（`api_closing`、凯利退回 `multi_avg`）、`x1x2_base.close`、`live`（`include_live=rule_1110`）：

```json
{
 "ah.macau": {
  "open": {
   "line": 0.0,
   "home_water": 0.77,
   "away_water": 1.01,
   "water_source": "actual",
   "water_censored": null,
   "recorded_at": "2026-06-15T19:07:52+08:00",
   "target_at": null,
   "basis": "first_record",
   "source": "odds_snapshot/rule/open",
   "available": true,
   "hidden_reason": null,
   "minutes_since_open": null,
   "return_rate": 0.94119,
   "return_rate_baseline": 0.944762,
   "return_rate_baseline_n": 75,
   "return_rate_dev": -0.003572
  },
  "mid": {
   "line": 0.0,
   "home_water": 0.77,
   "away_water": 1.01,
   "water_source": "actual",
   "water_censored": null,
   "recorded_at": "2026-06-15T19:07:52+08:00",
   "target_at": "2026-06-17T15:00:00+08:00",
   "basis": "asof_rule",
   "source": "odds_snapshot/rule/mid",
   "available": true,
   "hidden_reason": null,
   "minutes_since_open": 0.0,
   "return_rate": 0.94119,
   "return_rate_baseline": 0.944894,
   "return_rate_baseline_n": 75,
   "return_rate_dev": -0.003704
  },
  "close": {
   "line": 0.0,
   "home_water": 0.77,
   "away_water": 1.01,
   "water_source": "actual",
   "water_censored": null,
   "recorded_at": "2026-06-15T19:07:52+08:00",
   "target_at": "2026-06-17T22:00:00+08:00",
   "basis": "asof_rule",
   "source": "odds_snapshot/rule/close",
   "available": true,
   "hidden_reason": null,
   "minutes_since_open": 0.0,
   "return_rate": 0.94119,
   "return_rate_baseline": 0.944762,
   "return_rate_baseline_n": 75,
   "return_rate_dev": -0.003572
  },
  "mid_real": {
   "line": 0.0,
   "home_water": 0.77,
   "away_water": 1.01,
   "water_source": "actual",
   "water_censored": null,
   "recorded_at": "2026-06-15T19:07:52+08:00",
   "target_at": "2026-06-17T18:00:00+08:00",
   "basis": "asof_real",
   "source": "odds_snapshot/actual/t8",
   "available": true,
   "hidden_reason": null,
   "minutes_since_open": 0.0
  },
  "close_real": {
   "line": 0.0,
   "home_water": 0.77,
   "away_water": 1.01,
   "water_source": "actual",
   "water_censored": null,
   "recorded_at": "2026-06-15T19:07:52+08:00",
   "target_at": "2026-06-18T01:00:00+08:00",
   "basis": "asof_real",
   "source": "odds_snapshot/actual/t1",
   "available": true,
   "hidden_reason": null,
   "minutes_since_open": 0.0
  },
  "last_prematch": {
   "book": "macau",
   "market": "asian",
   "label": null,
   "target_at": null,
   "recorded_at": "2026-06-15T19:07:52+08:00",
   "valid_until": "2026-06-18T02:00:00+08:00",
   "line": 0.0,
   "home_water": 0.77,
   "away_water": 1.01,
   "home": null,
   "draw": null,
   "away": null,
   "over_water": null,
   "under_water": null,
   "water_source": "actual",
   "is_inplay": false,
   "source": "odds_timeline_seg/5df_macauslot_history",
   "minutes_before_kickoff": 3292.1,
   "stale": true
  }
 },
 "x1x2.macau.close": {
  "home": 2.3,
  "draw": 3.31,
  "away": 2.58,
  "complete": true,
  "recorded_at": null,
  "fetched_at": "2026-10-07T02:10:27.723517+08:00",
  "target_at": null,
  "basis": "api_closing",
  "source": "odds_snapshot/rule/close",
  "available": true,
  "hidden_reason": null,
  "minutes_since_open": null,
  "return_rate": 0.889289,
  "return_rate_baseline": 0.88999,
  "return_rate_baseline_n": 75,
  "return_rate_dev": -0.000701,
  "kelly": {
   "home": 0.8764,
   "draw": 0.8988,
   "away": 0.8964
  },
  "kelly_base": "multi_avg",
  "kelly_base_n_books": 3,
  "kelly_multi_avg": {
   "home": 0.8764,
   "draw": 0.8988,
   "away": 0.8964
  },
  "multi_avg_n_books": 3
 },
 "x1x2_base.close": {
  "pinnacle": null,
  "multi_avg": {
   "home": 0.381027,
   "draw": 0.27153,
   "away": 0.347443,
   "n_books": 3,
   "books": [
    "crown",
    "macau",
    "william"
   ]
  }
 },
 "live": [
  {
   "book": "macau",
   "market": "asian",
   "label": "rule_1110",
   "target_at": "2026-06-17T11:10:00+08:00",
   "recorded_at": "2026-06-15T19:07:52+08:00",
   "valid_until": "2026-06-18T02:00:00+08:00",
   "line": 0.0,
   "home_water": 0.77,
   "away_water": 1.01,
   "home": null,
   "draw": null,
   "away": null,
   "over_water": null,
   "under_water": null,
   "water_source": "actual",
   "is_inplay": false,
   "source": "odds_timeline_seg/5df_macauslot_history"
  }
 ]
}
```

## 15. 测试与冒烟

- `tests/test_table_matches.py`（… 个用例，全部在现网库的临时副本上跑；另有 … 个用例只读打开现网并比对 sha2… 前后一致）：未开赛/开赛 3h 内/无比分 → 无 result/settlement；未来 `as_of` 钳到 now；V3 字段与 `/matches/{id}/prediction` 逐场完全一致（… 场）；结算与 `backtest.settle_pnl` 一致；非法 `settlement_version` …；`actual_or_0…` 回落；缺数据为 null + `no_data`；scope jc/ext/all 与旧写法；分页与日期缺省；`mode=ro` 写入报错；返还率公式；**基准只用严格早于本日的数据**（把本日及之后改成极端值，基准与样本数不变）；窗口/最少样本；凯利平博基准 / 平博自身用多家平均 / 缺平博退回；快照盘口取反、例外场 `*_real`；非例外场 `*_real` 为 null；`include_live` 三档与 `rule_1…` as-of；`last_prematch` … 分钟 stale=false、… 分钟 stale=true、开赛后不算；flat 列集合稳定；OpenAPI 含路由与参数；nested 响应通过 Pydantic 模型校验。
- 全套 `pytest`：**42 passed**（原有 15 + 新增 27）。
- 冒烟（现网服务 127.0.0.1:8787 重启后，2026-10-08 17:14 UTC+8）：`/health` → `0.3.15`、`dual_write_odds_asian=false`；`/table/matches?date_from=2026-06-01&date_to=2026-06-07&scope=jc&strategy=CFFXDJ_5_V3&channel=rule` → 200、43 场、赛果 43、结算 win 1 / no_bet 42、例外场 35、`books_without_data=["pinnacle"]`；全区间 flat + `include_live=rule_1110` → 200、177 场 × 958 列、live 全空（现网无时间线）；非法 `settlement_version` → 400；不传日期 → 06-29～07-05 共 29 场；旧 `/matches` 200；现网 `app.db` sha256 前后一致。

## 16. 改动文件

**0.3.18**：`app/collection_schedule.py`（`EXCEPTION_RULE` / `phase_exception` / `jingcai_date_from_code` / `rule_targets`·`channel_targets` 的 `has_jc_code`）、`app/table_matches.py`（例外场、`kickoff_jc`、欧赔/竞彩 `water_source`、`water_move_eligible` / `tier_cross`、`meta`）、`app/shadow_evaluable.py`（N1 `close_unusable`、`close_basis` / `by_close_basis`、N5 ρ）、`app/db.py`（`APP_DB_PATH` / `APP_DB_LABEL` / `APP_READONLY`）、`app/main.py`（只读中间件、`meta`、`/collection/pending` 例外口径、`API_VERSION`）；**新增** `tests/test_second_batch_0318.py`、`tests/test_readonly_v2d3_0318.py`、`tests/test_n5_rho_0318.py`；脚本 `scripts/add_kickoff_jc.py`、`scripts/resync_odds_asian_from_snapshot.py`（新增），`reexport_phase_snapshots_exact_minute.py`（`--match-ids`）、`generate_shadow_predictions.py`（close_unusable、默认排除档位水位）、`generate_shadow_s1_v2.py`（exception_rule 指纹）、`import_odds_timeline_probe.py`（竞彩日按编号）、`fill_macau_mid_water.py`。备份：`api/backups/second-batch-0.3.18-20261008T174723/`。验收：`$ODDS_DATA_DIR/backfill/second-batch-2026-10-08/ACCEPTANCE.md`。

**0.3.17**：`app/collection_schedule.py`（`check_kickoff_placeholder`、`synth_jingcai_kickoff`）、`app/table_matches.py`（legacy_import usable、`ts_inferred`、`open_basis_reason`、baseline_method 随返还率、开赛占位符、hl_v0.2）、`app/shadow_evaluable.py`（N1 `open_unusable`、N5 修订）、`app/main.py`（仅 `API_VERSION`）、`requirements.txt`（numpy/scipy）、**新增** `tests/test_table_matches_0317.py`、`tests/test_kickoff_placeholder_0317.py`、`tests/test_shadow_evaluable_0317.py`，改 `tests/test_table_matches_0316.py` / `tests/test_table_matches.py` 期望；脚本 `scripts/validate_kickoff_placeholder.py`、`scripts/resync_odds_asian_mid_from_snapshot.py`、`scripts/generate_shadow_s1_v2.py`（新增），`scripts/generate_shadow_predictions.py`（N1 只追加 + open_unusable）、`scripts/fill_kickoff_minutes.py` / `scripts/import_kickoff_minutes_prod.py`（导入前占位符校验）。备份：`api/backups/0316-followups-0.3.17-20261008T173443/`。验收：`$ODDS_DATA_DIR/backfill/0316-followups-2026-10-08/ACCEPTANCE.md`。

**0.3.16**：`app/collection_schedule.py`（`is_phase_exception`、`rule_targets`、分钟精确）、`app/table_matches.py`、`app/main.py`（仅 `API_VERSION`）、`tests/test_table_matches.py`（4 个用例按新口径改期望）、`tests/test_early_kickoff_band.py`（11:31 期望改 03:31/10:31）、**新增** `tests/test_table_matches_0316.py`（13 个）、`scripts/validate_ah_sign.py`、`scripts/normalize_macau_mid_sign_v2d3.py`、`scripts/reexport_phase_snapshots_exact_minute.py`、`scripts/sync_odds_asian_from_snapshot.py` / `scripts/fill_macau_mid_water.py`（写 odds_asian 前取反 + 入库前校验）。备份：`match-analysis-api/backups/table-12-decisions-0.3.16-20261008T172218/`。

**0.3.15**：

| 文件 | 改动 |
|---|---|
| `match-analysis-api/app/table_matches.py` | **新增**：路由、取数、返还率/基准/凯利、Pydantic 响应模型 |
| `match-analysis-api/app/main.py` | `API_VERSION` 0.3.14 → **0.3.15**；文件末尾 `include_router` |
| `match-analysis-api/tests/test_table_matches.py` | **新增** 27 个用例 |
| `match-analysis-api/requirements.txt` | 追加 `httpx`（仅测试 TestClient 用；已装进 `.venv`） |
| `match-analysis-api/README.md` | 接口速查 + 0.3.15 记录 |
| `match-analysis-api/CHANGELOG.md` | **新增**（0.3.15 起；更早版本见 README） |
| `odds-data/schema/v2_0-table-matches-api.md` | 本文 |
| `odds-data/schema/v2_0-table-matches-openapi.json` | OpenAPI 片段 |

修改前备份：`match-analysis-api/backups/table-matches-0.3.15-*/`（main.py、README.md、requirements.txt）。
