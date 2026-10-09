# CHANGELOG · match-analysis-api

## 0.3.22 — 2026-10-08

依据：协作中拍板「澳门也要水位」+ 终版规则（主列手工 / 并列 5DF / 同 lane 高亮）。

### 版本号
- API `0.3.22`；`config_version` 仍 `hl_v0.3.1`；新增 `config.macau_5df`。
- **只读投影**；DUAL_WRITE 关；现网 `data/app.db` 未写水位、未 DDL。

### 澳门并列列
- `ah.macau.*` = **手工主列**（`book_lane=macau_manual`）：优先 `odds_snapshot/rule_legacy`；禁止 5DF timeline/rule 水进主列。
- `ah.macau_5df.{open|mid|close}` = **5DF 并列**（`book_lane=macau_5df`，`source` 固定 `"5df"`；`water_source`+`water_src`）。
- 返还率 / `water_move` / `tier_cross` **只在同 lane 内**；禁止 `macau`↔`macau_5df` 交叉比。
- 现网无 5DF 快照 → `macau_5df` 全 unavailable（前端可藏列）。

### 影子旁注（仅 v2d3）
- `scripts/reclassify_macau_5df_shadow_v2d3.py`：SHADOW_S1 / S1_V2 加 `water_src_reclassified=macau_to_5df`；不改 direction。

### 验收
- `$ODDS_DATA_DIR/backfill/macau-5df-parallel-col-2026-10-08/{ACCEPTANCE,FIELD_MAP}.md`
- 测试：`tests/test_macau_5df_0322.py`

> 0.3.14 及更早版本的记录在 `README.md`「v1.x / 0.3.x」各节。本文件从 0.3.15 起记。

## 0.3.21 — 2026-10-08

依据：竞彩／基本面草案 + 协作中拍板（hhad 决策选线、成分场 kickoff+3h、竞彩 11:10 同窗）。

### 版本号
- API `0.3.21`；`config_version` 仍 `hl_v0.3.1`；新增 `config.jc_fundamentals`。
- **只写 v2d3**；DUAL_WRITE 关；现网未动。

### 竞彩表（副本）
- 新表 `odds_jc_had`；`odds_jc_hhad` 元数据列；`odds_jc_hhad_line_hist`（含 `superseded_at`）。
- `odds_jc_home` 冻结只读；不投影 `odds_snapshot` jc_spf/jc_hhad（本地采集机 空）。

### 读路径
- `/table/matches`：`jc_1x2` 优先 had；仅 home → `jc_1x2_incomplete`；新 `jc_hhad` 决策时刻选线（禁止默认当前主行）。
- 竞彩 11:10：`out_of_window` 同亚盘窗；超窗进 alt。
- `/matches/{id}` stats：`meta.source/as_of` + `obs`（manual_seed 打标；injury 不伪 known_empty）。

### 辅助
- `app.fundamentals`：成分闸门／hhad 选线／11:10 窗；迁移 `scripts/migrate_jc_fundamentals_schema.py`。
- 验收：`$ODDS_DATA_DIR/backfill/jc-fundamentals-schema-2026-10-08/`

## 0.3.20 — 2026-10-08


依据：`docs/schema/v2_0-data-table-highlight-rules.md`「0.3.19 后续四件」+ `v2_0-repo-jc-order-fix-plan.md`「V4–V7 历史数字的处理」。

### 版本号
- API `0.3.20`；`config_version=hl_v0.3.1`（hl_v0.3 的补丁：返还率兜底分阶段 / 加 P10·P90·偏高档；凯利仍是 hl_v0.3 过渡余量 0.02，重档留给 hl_v0.4）。
- `config.hl="v0.3.1"`。

### 11:10 自采窗口
- 自采 `captured_at ∈ [11:00, 11:20]`（含端，`|fetch_lag_min| ≤ 10`）才算「即时（11:10）」主值。
- 超出：照存；主值退回时间线表；自采值进 `alt`（`{line, water, tick_at, origin, captured_at, fetch_lag_min, out_of_window=true}`）；`instant_src_diff=null`（不同时刻不比）；`match.daily_check += own_1110_out_of_window`。
- 特征能不能用仍按 usable 闸门（`captured_at ≤ target_at`）。

### 返还率兜底 hl_v0.3.1
- 按 `(market, book, phase∈{open,mid,close})` 分开算 P10 / P25 / P90；`n` = 场次数（同场同阶段按 match pk 去重），满 100 场才上色。
- as-of 不变：竞彩日严格早于本场、真实水位、人工复核场不进。
- 档：`< P10` medium、`< P25` light、`≥ P90` high（中性色，悬停「返还率偏高」；**不进**告警 / 日核对 / 导出风险统计）。无重档。
- 格子字段：`fallback_p10` / `fallback_p25` / `fallback_p90` / `fallback_n` / `fallback_hl_eligible` / `fallback_hl_level`。

### kickoff_drift_min（只作信息）
- `match.kickoff_drift_min` = 实际开赛（澳门场中首笔推算）− 赛程 `kickoff_at`（分钟）；来源 `config/kickoff_drift.json`（`scripts/build_kickoff_drift.py`）。
- 一006 / 五204 确认不按推迟处理：不写推迟列、不改目标时刻；只展示 drift≈15。
- 不进推迟口径 / 日核对 / 特征 / 策略。分析师侧 `phase_pending` / `features_ok` / `kickoff_rev` **本版不进 API**。

### alt.water
- 亚盘 `{home, away}`；大小球 `{over, under}`；1X2 `{home, draw, away}`（确认）。

### leak_suspect（不写库）
- `config/leak_suspect.json` 叠加：`LEGACY_V4`…`V7`（及中文别名）→ `cutoff_plus24`；V3 及其他 = null。
- 现网 / v2d3 的 `strategy_defs` / `predictions` **都没有** LEGACY 行（`in_db=false`）；前端只读字段。
- 挂到：`/strategies`、`/strategies/leak_suspect`、`/strategies/{key}/versions`、`/strategies/compare`、`/strategies/{id}/validate`、`/strategies/runs/{id}`、`/table/matches`（`strategy_leak_suspect`）、`/matches/{id}/prediction(s)`。
- 可选 migration `scripts/migrate_leak_suspect_live.py`（默认干跑；写现网需 `--approved-by`；LEGACY 不在库 → 回填 0 行）。

### 占位/阶段字段（副本；分析师写值）
- 格子（ah / x1x2 / jc）：`phase_pending` / `features_ok`（默认 true）/ `phase_assign_late` —— 读 `odds_snapshot` 列（若有）或 `extras_json`；列未建 → 默认。
- 比赛：`kickoff_rev`（默认 0）—— 读 `matches.kickoff_rev`；列未建 → 0。比赛级三字段给默认，方便前端先绑。
- **不读** `5dollar/live/state`；不重算；不改现网库、不在 v2d3 建列（等分析师 ingest 自带）。
- 现网 live / v2d3 截至验收：**四列都还没有**，extras 也还没有这些键 → 全程默认。

### 测试 / 服务
- 新增 `tests/test_hl_v031_0320.py`；全量 pytest 通过。
- 现网 `data/app.db` 未写、未加列。8787 / 8788 重启后验 version + meta + 8788 POST 403。

## 0.3.19 — 2026-10-08

依据：`docs/schema/v2_0-0316-followup-decisions.md`「0.3.18 之后与别名审查的口径」→「0.3.18 的五件事」+「推迟场补充（算法顾问）」。

### 推迟场
- `collection_schedule`：`known_kickoff_target` / `postpone_targets`（目标时刻只用到该时刻为止已公布的开赛时间：公告 ≤ 原定−Δ → 新开赛−Δ；公告更晚 → 原定−Δ；公告时刻不明 → 原定−Δ 并标 `postpone_ts_unknown`）；`postpone_void_check`（推迟 > 1h → pending）；`POSTPONE_VOID_HOURS=None` 钩子；`channel_targets(..., postpone=)`（例外场按编号 + `kickoff_original` 判）。
- v2d3 `matches` 新列：`kickoff_original`、`kickoff_actual`、`postponed_announced_at`、`postpone_ts_unknown`、`postpone_void_check`、`postpone_evidence`（`scripts/apply_postpone_manual_review_v2d3.py`）。日092：原定 07-06 08:00 / 实际 09:00 / 07:10 公布。
- API：match 级 `postponed`、`kickoff_original`、`kickoff_actual`、`postponed_announced_at`、`postpone_ts_unknown`、`postpone_void_check`、`postpone_delay_minutes`、`daily_check[]`；`schedule.kickoff_for_exception`、`schedule.postpone_target_basis{mid,close}`。pending → `settlement_hidden_reason=postpone_void_pending`。
- 日092 快照重导（`reexport_phase_snapshots_exact_minute.py` 认推迟列）：actual t8 01:00→00:00、t1 08:00→07:00（t1 盘值 0.76/1.08→0.74/1.10）；rule 15:00/22:00 不变。
- validate：pending 场不进 bets / n_eligible / hits，单独计 `n_postpone_pending` / `postpone_items`；`n_void_postponed` + summary `postpone_void_hours`（null，不进指纹；写死后才进）。v2d3 有推迟 / 人工复核状态时 `data_rev` 追加 `sp:<hash>`。

### kickoff_jc_conflict
- match 级 `kickoff_jc_conflict`（`kickoff_jc` 与 `kickoff_at` 差 ≥ 90 分钟）；v2d3 1 场（五201），现网 null。

### S2-V2 / N4-V2
- 新脚本 `scripts/generate_shadow_s2n4_v2.py`（不走 seed；只认真实水位；odds_source=hist）：v2d3 SHADOW_S2_V2 … 行、SHADOW_N4_V2 … 行（定义 id … / …）。S2-V2 validate：n=…、ROI …、赢 … / 赢半 … / 走 … / 输半 … / 输 …。
- 新模块 `app/ledger_registry.py`：旧 SHADOW_S2 / SHADOW_N4 禁止再写（`generate_shadow_predictions.py` 写库前拦，默认 `--only S8`；`import_lib` 同拦）；旧冻结条目读出时加 `prediction.ledger_note="触发依据是换算水位"`（N4 全部 13 条；S2 b 分支 2 条：五030、一044），`/table/matches`、`/matches/{id}/prediction(s)` 返回。

### 探针 209/210 人工复核
- v2d3 `matches.manual_review=1`、`manual_review_reason=ah_sign_mismatch_probe`；生成器（含 S1-V2）排除；表格不进返还率基准，结算隐藏 `manual_review`；validate 不可评估原因 `manual_review`（子原因 `ah_sign_mismatch_probe`）。

### 即时（11:10）合并自采 live 行（分析师追加 1，18:15 口径）
- `rule_1110_entries` / `build_live`（18:15 口径，替代先前「取最近」）：同一场、同一 book×market，自采行（odds_snapshot extras `capture=own`、`odds_source=live`、point/label `rule_1110`）和时间线表（开始 ≤ 11:10 的最后一段）都有 → 以自采为准，时间线值进 `alt={line, water, tick_at}`；只有时间线 → 照旧，`alt=null`。每格 `origin`、`odds_source`、`capture`、`captured_at`、`fetch_lag_min`、`merge_rule=own_capture_first;alt=timeline`。盘口不同或水位差 > 0.03 → `instant_src_diff=true`，`match.instant_src_diff` + `match.daily_check` 计入（不论 include_live），响应 `daily_check_summary`；全量只读清单 `scripts/daily_check_report.py`。`alt` 不参与升降盘与策略计算。as-of：11:10 ≤ as_of、自采 captured_at ≤ as_of。`tests/test_live_1110_merge_0319.py`（10 条）。

### 补数让路（分析师追加 2）
- 新模块 `app/shared_api_yield.py`：北京 11:05–11:20 / 14:55–15:15 / 21:55–22:15 不发请求；补数合计 ≤ 16 次/分钟（40 − 分析师 24；跨进程 flock 账本）；剩余 ≤ 24 停到 Reset。接入：`scripts/fill_1x2_odds_snap.py`、`scripts/fill_macau_mid_water.py`、`odds-data/backfill/5df-multibook-history-queue/{run_queue,map_csl_fixtures,map_alias90_fixtures}.py`、`odds-data/scripts/backfill_macau_mid_water.py`、`odds-data/backfill/scripts/5dollar_odds_resume.py`。`tests/test_shared_api_yield_0319.py`（30 条）。

### 其它
### N5 每竞彩日 ρ 异质性（分析师追加，18:15）
- `shadow_evaluable`：快照字段 `rho_Q`、`rho_df`、`rho_I2`（0–1 小数）、`rho_Q_p`，指纹 `rho_pool=fe|re_dl`，与 0.3.18 `rho_global` 同处强制（`_check_n5_rho_day`）；`rho_Q_p<0.05 ⇔ re_dl`；不可评估行可 null；非 N5 带这些字段拒写；旧快照不改。`tests/test_n5_rho_day_0319.py`（25 条），0.3.18 测试样例补字段。dc_model.py 未改。

### 高亮 hl_v0.3（分析师追加，18:19；只管上色，不进策略）
- `config_version=hl_v0.3`、`config.hl=v0.3`。
- 凯利：`config.kelly_highlight`，margin 0.02（轻：凯利 − 返还率 ≥ 0.02；中：凯利 ≥ 1.02；重：原无定义，null）。
- 返还率兜底：ah / x1x2 格新增 `fallback_p25` / `fallback_n` / `fallback_hl_eligible`（n ≥ 100）。按公司取真实水位返还率 P25，三阶段合并，只用竞彩日严格早于本场的数据，人工复核场不进；`config.return_rate_fallback_hl_v03`。
- 水位异动只比初 → 临：结果在 close 格，mid 格恒 null。初、临不同盘时 `tier_cross.kind=line`，只悬停；同盘但中盘换过盘时 `tier_cross_mid=true`。`TierCross` 增 `kind`，`TierPoint` 增 `line`。
- `tests/test_hl_v03_0319.py`（10 条）；`test_second_batch_0318` 的水位异动用例按 hl_v0.3 改写；`test_table_matches` / `_0317` 版本号改为 hl_v0.3。
- hl_v0.4（`kelly_thr_{h,d,a}` / `kelly_n_{h,d,a}`）未上。

### 收尾
- API 0.3.19；OpenAPI 片段重生成；`tests/test_postpone_0319.py`（23 条）。现网 app.db 未写、未加列。

## 0.3.18 — 2026-10-08

依据：`docs/schema/v2_0-0316-followup-decisions.md`「0.3.17 之后的第二批口径」A–F + `v2_0-odds-phase-terminology.md` 末节（例外场按编号判）。

### 例外场 / 竞彩日归属（A + 术语文档末节）
- 新口径 `exception_rule=jc_code_ge_2300`（旧 `time_window_2300_1130`）：有竞彩编号的场，属竞彩日 D 且开赛 ≥ D 23:00 即例外场（无上限），规则 mid/close = D 15:00 / 22:00，`*_real` 仍 T−8h / T−1h；无编号的场仍按 [D 23:00, D+1 11:30]（含端）。
- `collection_schedule.phase_exception(...)`、`rule_targets(..., has_jc_code=)`、`channel_targets(..., has_jc_code=)`、`jingcai_date_from_code(jc, kickoff)`；`/table/matches` schedule 与 `/collection/pending` 改走编号口径；`schedule.exception_rule`、`config.exception_rule`。
- 探针导入 `import_odds_timeline_probe.ensure_probe_match` 竞彩日改按编号星期推（不再拿开赛时间窗反推）。
- 例外场：现网 143 → 146、v2d3 144 → 147（新增 六008 / 二020 / 六036）；v2d3 这 3 场澳门 rule mid/close 快照重导（extras `exception_rule`、`target_at_before_reexport`）。

### 开赛时间核对（B）
- 五201 / 四201 / 日092：澳门场中首笔 tick + 公开赛程均支持 5DF → 保留 5DF。v2d3 `matches` 新列 `kickoff_jc`（竞彩官方整点；177/179 场有值）；API `match.kickoff_jc`（现网无列 → null）。新增 `scripts/add_kickoff_jc.py`。

### N1 close_unusable（C）
- `shadow_evaluable`：N1 新原因 `close_unusable`（子原因 `close_time_unknown_api_closing`，须 `close_basis=api_closing`）；快照字段 `close_basis`（`rule_tick` | `api_closing`）、`ledger_note`；validate 新分组 `by_close_basis{rule_tick, api_closing, unknown}` + `close_basis_split=true`；api_closing 组带 `ledger_note`「用到了时间未知的收盘价，只作历史参考」。旧冻结 N1 条目不改，按快照形状归 api_closing 组。
- v2d3：追加 173 条 hist N1 不可评估行（close_unusable）；冻结 113/114 不动。

### odds_asian 与快照同步（D）
- 新增 `scripts/resync_odds_asian_from_snapshot.py`（open / mid / close）；v2d3 同步 … 行（close … = … 盘口 + … 水位；open … 盘口；mid … 水位），写后 validate_ah_sign 三阶段 exit …。S1-V2 重跑：n=…、ROI …，与 ….1… 相同。

### 欧赔 / 竞彩 water_source（E）
- `x1x2.{book}.{phase}.water_source`、`jc_1x2.{phase}.water_source`：直接报价 `actual`（含已核实的旧手工欧赔 / 竞彩逐位录入），档位换算 `tier_midpoint`，未知 / 不可见 null。欧赔返还率基准只收 actual（`config.x1x2_return_hl_water=real_only`）。

### §2 水位异动只认真实水位（F）
- AH mid/close 格新字段 `water_move_eligible`（与上一阶段同盘且两格 actual → true；任一格非 actual → false；不可判 → null）、`tier_cross {from:{phase,home,away}, to:{...}, sides}`（档位换算格同盘跨档时给出，仅悬停，不上色）。`config.water_move_rule`。
- `generate_shadow_predictions.py`：tier_midpoint 水位默认不进方案计算（`--allow-tier-water` 仅复现旧行）。

### v2d3 只读实例
- `app/db.py`：`APP_DB_PATH`（不设 = data/app.db）、`APP_DB_LABEL`（live｜v2d3）、`APP_READONLY=1`；只读时连接 `mode=ro`，中间件把 POST/PUT/PATCH/DELETE 一律 403（含 validate）；v2d3 必须只读、不得指向现网库。`/health`、`/table/matches` 带 `meta={db, promoted, readonly}`。127.0.0.1:8788 指向 data/v2d3/app.db（日志 `logs/uvicorn-v2d3-0.3.18.log`）；8787 启动命令不变。

### N5 ρ
- `build_snapshot`：N5 / N5-PIN 快照必须有 `rho_global` / `rho_league` / `rho_league_se`，指纹必须 `rho=global_pooled_ivw`、`rho_se_method=profile_kish`；缺或值不对拒写。dc_model.py 未改。

### 不变
- 现网 `data/app.db` 未写；`DUAL_WRITE_ODDS_ASIAN` 关；冻结预测（SHADOW_S1 / SHADOW_N1 / V3）及结算未动；未跑种子脚本。

## 0.3.17 — 2026-10-08

依据：`docs/schema/v2_0-0316-followup-decisions.md`（六条）+ 用户更正（legacy_import 不一刀切 true，分析师已确认）、`v2_0-n5-poisson-spec-v1.md（未公开）` 末节修订、`v2_0-data-table-highlight-rules.md` hl_v0.2、前端 §13 对账四条。

### 变更（`GET /table/matches`）
- **开赛占位符**（决策 1）：5DF 开赛恰为 12:00 时对竞彩官方时刻（`meta.jingcai_kickoff_at` 完整时刻 > `meta.jingcai_kickoff_hour` > `matches.kickoff_hour` 整点）：不一致 → 用竞彩（`kickoff_source=jingcai`，`kickoff_placeholder=5df_1200`，仅整点时 `phase_target=hour_floor`）并重算 mid/close；无竞彩时刻 → 分钟按未知（hour_floor）+ `kickoff_placeholder=5df_1200`；竞彩整点 = 12 → 一致、保留 5DF（竞彩整点不带自然日）。行级 `match.kickoff_source` / `match.kickoff_placeholder`，`schedule.kickoff_source` / `kickoff_placeholder` / `kickoff_check`；`schedule.phase_target` 在分钟未知时为 `hour_floor`。现网 / v2d3 0 场触发（3 场世界杯 12:00 = 竞彩 12 点，in-play tick 与公开赛程印证 5DF 正确）。
- **legacy_import 初盘**（决策 4）：`earliest_ts_quote_at` 推定为竞彩日 11:10（新字段 `ts_inferred=true`；本家有更早真实报价则用真实值、`ts_inferred=false`），`usable_at_mid/close` 与 api_opening 同规则（earliest ≤ 该阶段 target_at，含端），不一刀切 true。
- 每个 open 对象都带 `open_basis`；为空时 `open_basis_reason`（`no_open_data` | `after_as_of`）。
- 返还率为空的格子 `baseline_method=null`。
- hl_v0.2：`config_version="hl_v0.2"`，`config.return_hl_water="real_only"`；empirical 基准与「≥20 场」计数只用 `water_source=actual`（正向白名单）。上色仍在前端。

### shadow_evaluable
- N1：新原因 `open_unusable`（子原因 `open_time_unknown_after_decision_possible`；只允许 N1，须带 open_basis 且决策阶段 usable=false）。不进四键恒在的 `REASONS`，出现时 by_reason 另列。写入端接受 `open_basis=legacy_import`（validate 分账仍归 unknown，桶不变）。
- N5 / N5-PIN 修订：`model_insufficient` 子原因 `fit_not_converged` / `team_wn_lt_min` / `league_wn_lt_min`；快照字段 `fit_message`（原文）、`home_w_n` / `away_w_n` / `league_w_n`（非负有限数）、`fingerprint`（`min_team_wn=…`、`min_league_wn=…`、`converge_check=true`、`n5_water=real_only` 固定值）。
- N5：`market_insufficient` 子原因 `real_water_lt_3`；快照 `water_source{book: actual|tier_midpoint}`，n_books / tick_age 计入非 actual 机构即拒绝，可评估须 ≥3 家 actual。

### 脚本 / 数据（只写 v2d3）
- 新增 `scripts/validate_kickoff_placeholder.py`（导入前占位符校验，只读；退出码 0/1/2）；`fill_kickoff_minutes.py` / `import_kickoff_minutes_prod.py` 计划阶段调用，占位符行 `hold_kickoff_placeholder` 不写。
- 新增 `scripts/resync_odds_asian_mid_from_snapshot.py`：v2d3 澳门 mid 派生行按 exact_minute 快照重同步（7 行：7 水位 + 1 盘口），写后 validate_ah_sign。
- 新增 `scripts/generate_shadow_s1_v2.py`：`SHADOW_S1_V2`（新 key/version，hist，符号校正 + exact_minute 快照）；旧 SHADOW_S1 两行冻结。
- `generate_shadow_predictions.py` N1 路径：只追加（`--n1-since` 必填），冻结行与 def 不动；威廉 1X2 初盘 usable_at_close=false → `open_unusable` 不可评估行；不拿 11:10 顶替初盘。
- `requirements.txt`：`numpy==2.5.3`、`scipy==1.18.1`（N5 dc_model 前置；未接生成器、未挂 N5）。

### 不变
- 现网 `data/app.db` 未写；`DUAL_WRITE_ODDS_ASIAN` 关；冻结预测（SHADOW_S1 / SHADOW_N1 / V3）未动；settlement 默认不变。

## 0.3.16 — 2026-10-08

依据：`docs/schema/v2_0-odds-phase-terminology.md` 末两节（§12 五条 + 算法顾问补充）、`v2_0-data-table-highlight-rules.md`（hl_v0.1，含 `baseline_method`）。

### 变更（`GET /table/matches` / `app.collection_schedule`）
- 例外场 = 开赛 ∈ [23:00, 次日 11:30]，两端都含（`collection_schedule.is_phase_exception`；与 0.3.15 行为一致，现网 0 场变化）。
- 中盘/临盘规则目标：非例外场 = 开赛 − 8h / − 1h **精确到分钟**（`rule_targets`；`kickoff_minute_known=0` 按整点）；例外场仍 15:00/22:00；`*_real` 精确到分钟。`schedule.phase_target="exact_minute"`、`schedule.kickoff_minute_known`。`calc_collection_times` 同步精确到分钟（`/dispatch/pending` 的采集时刻随之变化）。
- 返还率基准剔除档位中点换算水位（`water_source=tier_midpoint`）；每个返还率格子新增 `baseline_method`：`empirical` | `fixed_fallback`；`config.return_rate_fallback` = hl_v0.1 §4/§5 兜底阈值。
- 凯利多家平均改为不含本家（leave-one-out），格子新增 `n_avg`；`x1x2_base.*.multi_avg` 参考列仍含全部（新增 `n_avg`、`includes_self`）。
- 欧赔 `api_closing`：不进 `close` / 返还率 / 基准 / 凯利 / 多家平均；赛果可见后以 `x1x2[book].api_closing`（`label=api_closing`，「收盘（时间未知）」）单独返回，赛前不出现（flat 列恒在、全 null）。
- 初盘对象新增 `open_basis`（`first_tick`｜`api_opening`｜`legacy_import`）、`earliest_ts_quote_at`、`usable_at_mid` / `usable_at_close`、`unusable_reason`：本家有带时间戳报价时取最早一笔当初盘；只有 api_opening 时开盘时刻未知、不保证早于决策点 → 仅当 earliest_ts_quote_at ≤ 该阶段目标时刻才 usable；api_opening 的 `minutes_since_open` 为 null。展示用返还率/凯利仍可用；特征/策略使用初盘必须过 usable_at_*。时间线改为全库加载。

### 脚本
- 新增 `scripts/validate_ah_sign.py`（亚盘正负号入库前校验：单场复核 + 来源×阶段 20% 整批拦截；退出码 0/1/2）。
- 新增 `scripts/normalize_macau_mid_sign_v2d3.py`（v2d3 一次性：澳门 mid 163 行取反为正数=主让）。
- 新增 `scripts/reexport_phase_snapshots_exact_minute.py`（v2d3 快照按精确分钟重导，旧导出 hour_floor 留档）。
- `sync_odds_asian_from_snapshot.py` / `fill_macau_mid_water.py`：快照→odds_asian 写入前取反（API 负=主让 → 正=主让），提交前跑 `validate_ah_sign`，不通过回滚。

### 不变
- 现网 `data/app.db` 未写；`DUAL_WRITE_ODDS_ASIAN` 关；predictions / `produced_at` 未动；settlement 默认不变。

## 0.3.15 — 2026-10-08

### 新增
- `GET /table/matches`：数据表页（AG Grid）只读批量平铺接口，每场一行。
  - 参数：`date_from` / `date_to`（缺省：库内最近竞彩日往前 7 天）、`scope=jc|ext|all`（兼容 `jingcai|extra`）、`strategy`（默认 `CFFXDJ_5_V3`）、`channel=rule`、`include_live=none|rule_1110|all`、`settlement_version`、`as_of`、`baseline_window_days`、`baseline_min_n`、`format=nested|flat`、`limit`/`offset`。
  - 阶段：`open` = 各家最早一条；`mid`/`close` = 规则值（`collection_schedule`）；例外场另给 `mid_real`/`close_real`（赛前 8h/1h，as-of）；`live[]` 即时盘口（`label=rule_1110` = 竞彩日 11:10）；`last_prematch`（`minutes_before_kickoff`、`stale`，门槛 15 分钟）。
  - 后端计算（`config_version=hl_v0`）：亚盘/欧赔返还率；按 (玩法, 公司, 阶段) 的滚动中位数基准，样本只取竞彩日严格早于本场的数据；凯利（主基准平博去水，平博本家或缺平博 → 多家平均 `multi_avg`）。
  - 结算复用 `backtest.resolve_juice` / `settle_pnl`，取 `odds_asian(settle_book)`，默认 `ah_v4_water_midpoint` 不变；`settlement.code` 与 `backtest` 同值，`pnl_units` 按份。
  - 防泄漏：`as_of` < 开赛 + 3h 或无比分 → 不返回 `result` / `settlement`（`hidden_reason`）。
  - 快照 / 时间线表（v2d3 结构）存在时自动优先使用，亚盘盘口统一为「正数 = 主让」。
- 测试 `tests/test_table_matches.py`；`requirements.txt` 追加 `httpx`（TestClient）。

### 不变
- 只读：连接 `mode=ro`；不写任何表、不改表结构；`DUAL_WRITE_ODDS_ASIAN` 默认关；V3 预测与 `produced_at` 不动；settlement 默认不动；其它路由行为不变。

### 文档
- `docs/schema/v2_0-table-matches-api.md`
- `docs/schema/v2_0-table-matches-openapi.json`
