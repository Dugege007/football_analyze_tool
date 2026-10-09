# 比赛分析工具 · 本地 API（Phase 0/1 · v0.2 multi-market）

对齐 作者的私有分析仓 主干：月度 JSON 五段 `match/result/stats/odds/meta` + `prediction`。  
默认主清单 `scope=jingcai`；结算口径 `macau_close`；默认策略 `CFFXDJ_5_V3`。

## 路径

| 项 | 路径 |
|---|---|
| 项目根 | `api/` |
| SQLite | `$APP_DB_PATH` |
| Schema | `docs/schema/v1_sqlite.sql` |
| Migration | `docs/schema/v1_1_prediction_legs.sql`、`v1_2_bankroll_alias_kickoff.sql`、`v1_3_strategy_workshop.sql`（`apply_schema` 会自动补齐） |
| OpenAPI | `docs/schema/openapi-v1.2.yaml`（bankroll）；`openapi-v1.3-strategies-draft.yaml`（M2）；`openapi-v1.4-composer-draft.yaml`（M3）；`openapi-v1.5-compare-draft.yaml`（M4）；`openapi-v1.6-stack-draft.yaml`（M5 stack + v1.7 水位；API 0.3.8） |
| 种子样例 | `docs/backfill-schema/json-v2-sample.json` |

## 安装

```bash
cd api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 建库 + 导入样例

```bash
cd api
source .venv/bin/activate
python scripts/seed.py
```

会删除并重建 `data/app.db`，应用 `v1_sqlite.sql`，写入 JSON v2 样例两场：

- jingcai：`2026-10-06|一003`
- extra：`2026-10-06|extra|非竞彩示例主|非竞彩示例客`

## 启动

```bash
cd api
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8787
```

CORS 已对任意 Origin 开放，Vite 开发口可直连或走 `/api` 代理。

## 停止

前台：`Ctrl+C`  
后台：`kill $(lsof -t -i:8787)` 或记下启动时的 PID 后 `kill <pid>`。

## 接口速查

| Method | Path | 说明 |
|--------|------|------|
| GET | `/health` | `{"ok": true}` |
| GET | `/matches?date=YYYY-MM-DD&scope=jingcai\|extra\|all` | 赛程列表（默认 jingcai）；0.3.2 起每项带 `result`（同详情，无则 null） |
| GET | `/matches/{id}` | 五段详情（无 prediction） |
| GET | `/matches/{id}/odds` | 盘口段 |
| GET | `/matches/{id}/prediction?strategy=` | 亚盘预测（默认 CFFXDJ_5_V3；旧接口） |
| GET | `/matches/{id}/predictions?strategy=` | AH + `prediction_legs` 合集 |
| GET | `/dispatch/pending?window=close\|mid` | 临盘/中盘待发场（近似） |
| GET | `/table/matches?date_from&date_to&scope=jc\|ext\|all&strategy&include_live=none\|rule_1110\|all&format=nested\|flat` | 0.3.19 数据表页批量平铺（只读；见 `v2_0-table-matches-api.md`） |

`{id}` 可用 `match_uid` 或内部数字 id。

## curl 示例

```bash
curl -s http://127.0.0.1:8787/health

curl -s 'http://127.0.0.1:8787/matches?date=2026-10-06&scope=jingcai' | python3 -m json.tool

curl -s 'http://127.0.0.1:8787/matches/2026-10-06|一003' | python3 -m json.tool

curl -s 'http://127.0.0.1:8787/matches/2026-10-06|一003/odds' | python3 -m json.tool

curl -s 'http://127.0.0.1:8787/matches/2026-10-06|一003/prediction' | python3 -m json.tool

curl -s 'http://127.0.0.1:8787/matches?date=2026-10-06&scope=all' | python3 -m json.tool
```

## 说明

- 亚盘：澳门 `open` 在 JSON 里是裸数字时入库 `handicap` + 水位 NULL；读接口优先 `odds_raw`，保持裸数字形态。
- `jc_hhad.goal_line`：表列为 REAL，API 回读优先 `odds_raw` 保留字符串 `"-1"`。
- `kickoff_at` / `ids` / `schema_version` / `pipeline` / `note` 存在 `match_meta.extras_json`（schema 无独立列）。


## Monthly JSON import

```bash
# download/copy YYMM.json under $ODDS_DATA_DIR/imports/2026/
python scripts/import_monthly_json.py \
  $ODDS_DATA_DIR/imports/2026/2606.json \
  $ODDS_DATA_DIR/imports/2026/2607.json \
  --reset-db

# rollback one batch (CASCADE delete matches)
python scripts/rollback_import.py --batch-id N
```

Upsert: `ON CONFLICT(match_uid) DO UPDATE` on `matches`; child tables DELETE+INSERT.
Monthly import does **not** invent predictions (`write_prediction=False`).


## v1.1 · prediction_legs & dispatch

- 表 `predictions` **不动**（亚盘方向 `CFFXDJ_5_V3`）。
- 新表 `prediction_legs`：`market ∈ {ah,ou,1x2,jc_hhad}`；缺字段写 `gap_json`，不硬编。
- `GET /matches/{id}/predictions` → `{ ah, legs }`；无 AH 时 `ah=null`（不 404）。
- `GET /dispatch/pending?window=close|mid` 对齐仓库 `jingcai_collector.calc_collection_times`：
  - kickoff：优先 `match_meta.extras_json.kickoff_at`；否则 `jingcai_date + kickoff_hour`（+08:00）；**早场特殊带 [00:00,11:30]（仅整点 0–11）→ 次日**
  - **采集点（落在 jingcai_date 日历；日用 rule 0.3.9+）**：
    - **≥23:00 或早场特殊带**：中盘 `15:00`，临盘 `22:00`（rule；legacy 对照仍为 16:00/23:00）
    - **12:00–22:xx**：中盘 = kickoff−8h，临盘 = kickoff−1h
  - **待发判定**：`abs(now − collect_at) ≤ 30min` **或** `collect_at ≤ now ≤ kickoff`
  - 响应含 `mid_collect_at` / `close_collect_at` / `collection_schedule` / `approximation`
- `prediction_legs.stake`：整数「份」，可空；**不写死本金单位**，1 份金额由资金表另定

迁移（不删库）：

```bash
cd api
source .venv/bin/activate
python -c "from app.db import connect; from pathlib import Path; c=connect(); c.executescript(Path('docs/schema/v1_1_prediction_legs.sql').read_text()); c.commit(); print(list(c.execute('select * from schema_migrations')))"
```


## v1.2 · bankroll / alias / kickoff_at（API 0.3.0）

说明：`docs/schema/v1_2-schema-notes.md`

```bash
# 幂等迁移（重复执行安全；前后核对行数）
.venv/bin/python scripts/migrate_v1_2.py --dry-run
.venv/bin/python scripts/migrate_v1_2.py
```

| Method | Path | 说明 |
|--------|------|------|
| GET | `/bankroll/config` | 资金配置 + `amount_todo` + 最新余额快照 |
| PUT | `/bankroll/config/{key}` | 整键替换（stats_initial_bankroll / monday_base_bankroll / calculator_bankroll / unit_definition / stake_caps / stake_amount_limits） |
| POST | `/bankroll/snapshots` | 报余额；`set_as_monday_base=true` 同步周一基数 |
| POST | `/bankroll/calc` | 份→金额；单场 3 / 单日 10 份；单注 ≥50、≤ 剩余资金 50%；返回 capped / raised |

```bash
curl -s -X POST http://127.0.0.1:8787/bankroll/calc -H 'Content-Type: application/json' \
  -d '{"items":[{"match_id":"………|一…","stake_units":…}]}' | python3 -m json.tool
```

## v1.3 · 方案工场 M2（API 0.3.1 stubs）

说明：`docs/schema/v1_3-strategy-workshop-m2.md`  
DDL：`v1_3_strategy_workshop.sql`（表 `strategy_defs` / `strategy_validation_runs` / `strategy_validation_cache`）

| Method | Path | 说明 |
|--------|------|------|
| GET/POST | `/strategies` | 方案定义列表 / 登记一版 |
| GET | `/strategies/{key}/versions` | 某 key 全部版本 |
| POST | `/strategies/{id}/validate` | 创建验证 run（允许纯影子；编排 stub） |
| GET | `/strategies/runs/{run_id}` | run 摘要 |
| GET | `/strategies/runs/{run_id}/cache` | cache 行（可空） |

完整 validate 编排 OUT OF SCOPE（M3）；现网 AH / bankroll 语义不变。


## v1.4 · 方案编排器 M3 MVP（API 0.3.3）

说明：`docs/schema/v1_4-strategy-composer-m3.md`；OpenAPI：`openapi-v1.4-composer-draft.yaml`（无新 DDL）。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/strategies/templates` | 内置模板 `default` / `simple_gate` |
| POST | `/strategies/compose` | 模板 + `config_overrides` → 新 version（未知键进 extras，写 config_fingerprint） |
| POST | `/strategies/{id}/validate` | M3 起 fingerprint；**M4 起**未命中写非空 summary/cache（见下节） |

`CFFXDJ_5_V3` 默认与亚盘语义不变。

## v1.5 · 方案对比 M4（API 0.3.4）

说明：`docs/schema/v1_5-strategy-compare-m4.md`；OpenAPI：`openapi-v1.5-compare-draft.yaml`（无新 DDL）。

| Method | Path | 说明 |
|--------|------|------|
| POST | `/strategies/{id}/validate` | 指纹命中 `reused`；未命中写 **非空** summary（n/pnl/roi/skipped…）+ cache 行（简化亚盘 `macau_close` + juice …） |
| POST | `/strategies/compare` | 多方案 `series.cumulative_pnl` 折线；`stack: null` stub；默认 `auto_validate` |

M3 空 placeholder 同指纹可升级重算；已有非空 cache 仍严格复用。（M5 起结算/注额口径见下节，M4 run 不再被复用。）

## v1.6 · 方案叠加 + 验证精算 M5（API 0.3.5 → 0.3.6）

说明：`docs/schema/v1_6-strategy-stack-m5.md`；OpenAPI：`openapi-v1.6-stack-draft.yaml`；引擎 `app/backtest.py`（无新 DDL）。

- **validate**：`settlement_version=ah_v2_water_bankroll`。真实水位（`macau_close` 该侧 water ∈ (…,…]），否则回落 …，计 `juice_source=actual|fallback`；注额走资金曲线（本金 `stats_initial_bankroll`=…，… 份=周初权益×…，单场 … / 单日 … 份向下取整，单注 ≤ 剩余 …，< … 默认不下）；summary 带 `bankroll` 终值与 `max_drawdown{amount,pct}`。指纹含结算版本 + 注额/资金规则。
- **compare**：`stake_mode=per_strategy|unified`（+ `stake_rule`）；`stack` 为共用资金组合：`summary`（含 `max_drawdown`、`conflict_count`、`per_strategy`）、`series`（`bankroll`/`cumulative_pnl`/`drawdown_pct`/`conflict`/`sides`）、`conflicts[]`。同场反向各自结算、不对冲。stack 实时计算不落库。`conflicts[]` 带 `jc_id` / 规范队名 / `kickoff_at`。
- **测试影子方案** `TEST_V3_MIRROR`（id 8，`extras.test_only=true`，不进日用白名单）：`python scripts/seed_test_v3_mirror.py` 幂等重建；现网默认只取 V3，`/matches.has_prediction` 排除 test_only 方案。

```bash
curl -s -X POST http://127.0.0.1:8787/strategies/1/validate -H 'Content-Type: application/json' -d '{"shadow":true}'
curl -s -X POST http://127.0.0.1:8787/strategies/compare -H 'Content-Type: application/json' \
  -d '{"strategy_ids":[…],"stake_mode":"unified","stake_rule":{"default_units":…,"use_prediction_stake":false}}'
```

**0.3.6 修订**（`settlement_version=ah_v3_juice_src_raise`）：
- 水位：`macau_close` 为仓库固定 0.95（`juice_source=fixed_macau`，非缺陷）；crown/william 可选水档映射 `params.water_tier_map=true`（水位=0.75+(档−1)×0.05，超 1~9 回落并记 `tier_out_of_range`），默认关，进指纹。
- `below_min` 默认 `raise`（抬到 50）；剩余 < 100 时不下并记 `bankrupt_guard`；`skip` 可显式传。
- `data_rev` 自动进指纹（`ib:<import_batches 最大 id>`，无批次则核心表行数+max(id) 哈希；`params.data_rev` 可覆盖），validate/compare 响应回显。
- 测试参数 `params.initial_bankroll` 覆盖本金（进指纹）。
- **0.3.7**：自动 `data_rev` = `ib:<最大批次 id>|p:<本方案 predictions 行数>:<内容哈希>`；改某方案预测只让该方案重算，seed 重跑幂等。
- **0.3.8**（v1.7）：crown/william 亚盘水位由档位 t 迁移为中点水位 `0.70+0.05t`（t=0/10 为截断值，标 `water_censored`，原档位存 `extras_json.water_tier_raw`），结算直接用库内水位（`juice_source=tier_midpoint|actual`），可选 `exclude_censored`，`water_tier_map` 废弃；`settlement_version=ah_v4_water_midpoint`；说明 `docs/schema/v1_7-water-tier-midpoint.md`，迁移 `scripts/migrate_v1_7_water.py`（`--dry-run/--rollback`）。
- **0.3.9**（v2.0 D1）：dispatch 日用 rule 改为开赛≥23:00/早场特殊带→竞彩日 15:00/22:00，响应附 `rule_legacy_*` 对照；探针时间线进副本库 `data/v2d1/app.db`（见 `v2_0-d1-import-notes.md`），现网 `odds_asian` 不动。
- **早场带拓宽（2026-10-07）**：与仓库 私有仓 PR 同谓词 `[00:00,11:30]`（仅整点 `0<=h<=11`；11:31–11:59 需分钟）；≥23 中/临盘通道不变；见 `v2_0-cutoff-1130-pending-changes.md`。
- **0.3.10**：validate/compare summary 对齐影子台账三键 `n_eligible`/`hits`/`coverage`（←n/signal_count；无 eligible→coverage=null）；`multi_hit_count` 恒 null；不进指纹、不改结算。见 `mutex-buckets-min-n.md` §1。
- **0.3.11**（v2.0 D2 演练）：副本 `data/v2d2` legacy→`rule_legacy` snapshot + rule→odds_asian 仅 INSERT；VIEW `v_odds_asian_rule`；现网 `DUAL_WRITE_ODDS_ASIAN` 默认关。见 `v2_0-d2-rehearsal-notes.md`。
- **0.3.12**：影子台账 pending_shadow → `SHADOW_*` strategy_defs（test_only，无 predictions）；`GET /strategies` 回显 ledger_id/bucket，支持 `?test_only=`。见 `v2_0-shadow-defs-notes.md（未公开）`。
- **0.3.13**：挂 `SHADOW_S2/N4/S8` 预测（落地卡）；S8 `ledger_hit_mode=filter_skip`（不下注可计 hits）。见 `v2_0-shadow-predictions-s2-n4-s8.md（未公开）`。
- **0.3.14**（v1.8）：可选 `settlement_version=ah_v4_macau_actual_or_095`（macau 真水或回落 0.95）；默认仍固定 0.95；compare `include_settlement_sensitivity` 同注单副表 + 第二套 series；summary 带 `n_actual_water`/`n_fallback_095`/`fallback_rate`。见 `v1_8-settlement-actual-or-095.md`；方案卡 `settlement-fair-compare-card.md`。V3 哈希不变。
- **0.3.19**（推迟场 + kickoff_jc_conflict + S2-V2/N4-V2 + 探针人工复核）：推迟场目标时刻只用当时已公布的开赛时间（`kickoff_original`/`kickoff_actual`/`postponed_announced_at`/`postpone_ts_unknown`/`postpone_void_check`，v2d3 加列、API match 级），例外场按编号 + 原定开赛判；推迟 > 1h `pending` 暂不结算、validate 单独计数；`void_postponed` 钩子（`POSTPONE_VOID_HOURS=None`）；`kickoff_jc_conflict`（≥90 分钟）；`scripts/generate_shadow_s2n4_v2.py`（S2-V2 19 行 / N4-V2 0 行），旧 S2/N4 冻结禁止追加、旧条目 `ledger_note`「触发依据是换算水位」；探针 209/210 `manual_review`（validate 原因 `manual_review`）；即时（11:10）自采优先、时间线值进 `alt{line,water,tick_at}`、差异 `instant_src_diff` 进日核对（`daily_check_summary`、`scripts/daily_check_report.py`）；N5 每日 ρ 异质性字段 `rho_Q/rho_df/rho_I2/rho_Q_p` + 指纹 `rho_pool`；高亮 hl_v0.3（凯利 +0.02、返还率兜底按公司 P25 `fallback_p25/fallback_n`、水位异动初→临 + `tier_cross_mid`）；补数让路 `app/shared_api_yield.py`（三段让路、补数 ≤16 次/分钟）。详见 `CHANGELOG.md`。
- **0.3.18**（第二批口径 A–F + v2d3 只读实例 `APP_DB_PATH`/`APP_DB_LABEL`/`APP_READONLY`（8788，写接口 403，`meta.db`/`meta.promoted`）+ N5 ρ 快照强制）：例外场按竞彩编号判（`exception_rule=jc_code_ge_2300`，现网 143→146），竞彩日归属一律按编号（探针导入同改）；v2d3 `matches.kickoff_jc` + API `match.kickoff_jc`；N1 `close_unusable` + validate `by_close_basis`/`close_basis_split`；v2d3 odds_asian 澳门 open/mid/close 按 exact_minute 快照同步（`scripts/resync_odds_asian_from_snapshot.py`）；欧赔/竞彩格 `water_source`、欧赔基准 real_only；AH `water_move_eligible` / `tier_cross`，档位水位不进方案计算。详见 `CHANGELOG.md`。
- **0.3.17**（0316 follow-up 六条 + 前端 §13 对账 + hl_v0.2）：5DF 12:00 开赛占位符检查（`kickoff_source` / `kickoff_placeholder`，导入前校验 `scripts/validate_kickoff_placeholder.py`）；legacy_import 初盘 earliest 推定竞彩日 11:10（`ts_inferred`）、usable 同 api_opening 规则；open 对象 `open_basis_reason`；返还率空则 `baseline_method` 空；`config_version=hl_v0.2`、`return_hl_water=real_only`；shadow_evaluable 增 N1 `open_unusable`、N5 修订子原因/字段；v2d3 新影子 `SHADOW_S1_V2`；numpy/scipy 入 requirements。详见 `CHANGELOG.md`。
- **0.3.16**（§12 五条落地）：例外场 [23:00, 次日 11:30] 两端都含；中盘/临盘目标精确到分钟（`phase_target=exact_minute`）；返还率基准剔除档位中点水位 + `baseline_method`（`empirical`/`fixed_fallback`，兜底阈值 `config.return_rate_fallback`）；凯利多家平均不含本家 + `n_avg`；欧赔 `api_closing` 不进任何计算、完赛后单独返回。新增 `scripts/validate_ah_sign.py`（亚盘正负号入库前校验）。详见 `CHANGELOG.md`。
- **….1…**（数据表页）：新增只读 `GET /table/matches`（AG Grid「数据表」页每场一行；前端经 Vite `/api` 代理访问）。四家亚盘（澳门/皇冠/威廉/平博）× `open`（各家最早一条）/`mid`/`close`（规则值，`collection_schedule`）+ 例外场 `mid_real`/`close_real`；欧赔 1X2、竞彩胜平负；V3 预测（与单场接口同字段，冻结只读）；赛果与每场结算（复用 `backtest.settle_pnl`，默认 `ah_v4_water_midpoint` 不变，`pnl_units` 按份）；返还率、按公司滚动中位数基准（只用竞彩日严格早于本场的数据）、凯利（平博基准 / 多家平均）按 `hl_v0`；`live[]`（`include_live`，`rule_1…`=竞彩日 …:…）与 `last_prematch`（新鲜度 … 分钟）。未开赛 / 开赛 3h 内不返回赛果与结算。SQLite `mode=ro`，不写库；`DUAL_WRITE_ODDS_ASIAN` 仍关。说明 `docs/schema/v2_0-table-matches-api.md`；OpenAPI 片段 `v2_0-table-matches-openapi.json`。测试 `tests/test_table_matches.py`（需 `httpx`）。
- **N5 预备（………，未单独 bump API_VERSION）**：validate/compare summary 增 `n_not_evaluable`、`n_not_evaluable_by_reason`（pinnacle_missing/market_insufficient/model_insufficient/model_scope 四键恒在）、`n_not_evaluable_by_subreason`、`not_evaluable_items`；判据 = 预测 `rationale_json` 内 `feature_snapshot.evaluable=false` 或 `not_evaluable_reason` 非空，此类场不进 `n_eligible`、不算未触发（无赛果/无结算盘仍先进 `skipped_detail`）。旧缓存 run 读时补 …（改动前库内无此类预测）。不动表结构、不改结算、不进指纹；S1/S2/S8/N1/N4/V3 数字回归一致（`tests/test_not_evaluable.py`）。写入端 helper `app/shadow_evaluable.py`；种子脚本对 N5/N5-PIN 加保护。**来源分账**：summary 另给 `by_odds_source{live,hist,unknown}`（各来源单独模拟资金曲线，字段同顶层：n_eligible/hits/coverage/roi/n_not_evaluable(+by_reason) 等）+ 顶层 `odds_source_split=true`；compare 的 stack summary 与 `per_strategy[*]` 同样分账；无 `feature_snapshot.odds_source` 的行进 unknown（现有策略全部如此）；旧缓存 run 读时补（unknown=顶层拷贝，标 `odds_source_split_backfilled`）。顶层字段不变。N5/N5-PIN 快照 builder 必填 `odds_source=live|hist`，hist 不得记 `stale_tick`。**初盘口径分账**：同法另给 `by_open_basis{first_tick,api_opening,unknown}` + `open_basis_split=true`（键 `feature_snapshot.open_basis`，缺省→unknown）；builder 可选 `open_basis=first_tick|api_opening`，`api_opening` 不保证决策前可得——用到初盘派生特征（open_line/odds/return_rate/kelly/multi_avg）时必须 `usable_at_<决策阶段>=true`，否则拒绝。见 `docs/schema/v2_0-shadow-predictions-n5pin.md（未公开）`。
