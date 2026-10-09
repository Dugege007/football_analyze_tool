# Schema v1.2 说明（后端 · 2026-10-06）

> 状态：**正式，已应用**到 `$APP_DB_PATH`（`schema_migrations.version = 'v1.2'`）。  
> DDL：[`v1_2_bankroll_alias_kickoff.sql`](./v1_2_bankroll_alias_kickoff.sql)  
> OpenAPI：[`openapi-v1.2.yaml`](./openapi-v1.2.yaml)（API `0.3.0`）  
> 应用脚本：`api/scripts/migrate_v1_2.py`（幂等；重复执行忽略 duplicate column）  
> 对齐：[`data-conventions-v1.md`](./data-conventions-v1.md) §1.2 / §2.2 / §4.4、[`analysis-tool-roadmap-20261006.md`](./analysis-tool-roadmap-20261006.md) §5  
> 前身：`v1_2-schema-increment-draft.md`（草案，已被本文件取代）

## 1. 已关闭的拍板项

| # | 决定 | 落地 |
|---|---|---|
| 1 | 别名 **全局唯一** | `team_aliases.alias` / `league_aliases.alias` 各自 `UNIQUE`；`source` 只是溯源列，不进主键（主键为自增 `id`）。多源同名只能指向同一主名 |
| 2 | `matches` 过渡 | 继续存文本 `home_team` / `away_team` / `competition_name`（可冗余规范名）；`home_team_id` / `away_team_id` / `league_id` 可空，逐步回填 |
| 3 | 不建注额重算审计表 | 重算 = 记一条 `bankroll_snapshots` + 预测行写 `updated_at`（`produced_at` 不动） |
| 4 | 统计本金 / 单注金额上下限（2026-10-06 补拍板） | `stats_initial_bankroll = {"amount": 10000, "currency": "CNY"}`；`stake_amount_limits = {"min_stake_amount": 50, "max_stake_pct_of_remaining": 0.5}`（单注 ≥50，≤ 当前剩余资金 50%）。已有库仅在 amount 仍为 null 时补 10000，不覆盖人工改值 |

**实体表口径**：**不使用**统一 `entities` 表；`teams` / `leagues` 分表（各带别名表）为准，与数据约定 §2.2 同构，仅把 `PRIMARY KEY(alias, source)` 改为 `UNIQUE(alias)`。

## 2. 表与列变更

| 对象 | 变更 |
|---|---|
| `teams` / `leagues` | 新表：`id`、`name_zh_canonical UNIQUE`、`created_at`、`note` |
| `team_aliases` / `league_aliases` | 新表：`id`、`alias UNIQUE`、`team_id`/`league_id`（FK, CASCADE）、`source`、`created_at` |
| `matches` | +`kickoff_at TEXT`（ISO-8601 +08:00 到分钟）、+`kickoff_minute_known INTEGER DEFAULT 0`、+`home_team_id`/`away_team_id`/`league_id`（可空 FK）；`kickoff_hour` 保留 |
| `predictions` | +`stake INTEGER`（「份」，≥0，可空）、+`stake_rule`、+`message_sent_at`、+`updated_at`；仍 `UNIQUE(match_id, strategy)`，每策略一行 |
| `prediction_legs` | +`stake_rule`、+`p_used`、+`f_star`、+`status`（默认 `active`）、+`updated_at`。注：`stake` 列 v1.1 为 REAL（SQLite 改不了类型），API 统一输出 int |
| `bankroll_config` | 新表：`key` PK、`value_json`、`updated_at`、`note` |
| `bankroll_snapshots` | 新表：`reported_at`、`balance ≥0`、`source`、`note` |

### kickoff_at 回填（本次已执行）

- 只填空值，不覆盖。优先 `match_meta.extras_json.kickoff_at`（→ `minute_known=1`）；现库 177 场该值全为 null。
- 否则用 `jingcai_date + kickoff_hour` 合成整点，**0–10 点记次日**（竞彩日规则），`minute_known=0`。
- 结果：177/177 有 `kickoff_at`，全部 `kickoff_minute_known=0`（历史仅整点）。
- 导入钩子：`scripts/import_lib.py::sync_kickoff_at`——JSON 带 `kickoff_at` 则写入并置 1；否则合成整点（不覆盖已知到分钟的值）。
- API 解析 kickoff 顺序：`meta.extras.kickoff_at` → `matches.kickoff_at` → 合成；`/dispatch/pending` 结果与 v0.2.2 一致。

### bankroll_config 占位键（INSERT OR IGNORE，不覆盖已改值）

| key | 初始 value | 说明 |
|---|---|---|
| `stats_initial_bankroll` | `{"amount": 10000, "currency": "CNY"}` | 统计用固定初始本金（拍板）；也是 calc 基数的最后回退 |
| `monday_base_bankroll` | `{"amount": null, "as_of": null, "snapshot_id": null}` | 周一基数 B_week；可由 snapshot `set_as_monday_base=true` 写入 |
| `calculator_bankroll` | `{"amount": null}` | 计算器默认本金（用户自设） |
| `unit_definition` | `{"unit_fraction": 0.005, "base_key": "monday_base_bankroll"}` | 1 份 = 基数 × 0.5% |
| `stake_caps` | `{"per_match": 3, "per_day": 10, "mode": "clamp"}` | 单场 / 单竞彩日上限（份） |
| `stake_amount_limits` | `{"min_stake_amount": 50, "max_stake_pct_of_remaining": 0.5, "currency": "CNY"}` | 单注金额下限 50；单注 ≤ 当前剩余资金 50% |

未放 `kelly` 键：凯利计算层未验证，不抢接（调研 S8 仅草案）。

## 3. 注额计算器 API（已实现，API 0.3.0）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/bankroll/config` | 全部键 + `amount_todo`（amount 仍 null 的本金键）+ `latest_snapshot` |
| `PUT` | `/bankroll/config/{key}` | 整键替换（非合并）；只接受上表 6 个键，带校验 |
| `POST` | `/bankroll/snapshots` | 用户报余额；`set_as_monday_base=true` 时同步周一基数 |
| `POST` | `/bankroll/calc` | 份 → 金额；只算不写库 |

**calc 规则**

- 基数：请求 `bankroll` 优先；否则 `base_key`（缺省 `unit_definition.base_key` = 周一基数）对应 `amount`；未显式传 `base_key` 且周一基数为空时回退 `stats_initial_bankroll`（10000）；都没有 → **422**。
- `unit_amount = round(base × unit_fraction, 2)`；`amount = round(份 × unit_amount, 2)`。
- 份数来源：条目 `stake_units`（整数 ≥…，小数直接 …）优先；否则按 `(match_id, strategy 缺省 CFFXDJ_5_V3)` 查 `predictions.stake`；null 份 → `amount=null`。
- 上限处理（**选择：clamp + 显式标记**，不静默改数）：
  1. 单条 > 单场上限 → 截断到 3，`cap_reasons += per_match`
  2. 同一场多条（多策略）合计 > 3 → 等比例缩放向下取整，`per_match_total`
  3. 同一竞彩日合计 > 10 → 等比例缩放向下取整（调研 S8 原文），`per_day`
  - 被改条目 `capped=true`，保留 `stake_units_requested`；响应顶层 `capped` 汇总。
  - `cap_mode=warn`：份数不改，只写 `warnings`。
  - 注意：等比例 + floor 对小份数较狠（1 份可能被缩到 0）；如需「最大余数」补足到上限，请分析师拍板后再改。
  - 上限按**真实敞口**计：多策略对比请分开调用 calc，避免互相挤占单日额度。
- 金额上下限（`stake_amount_limits`，在份数上限之后执行；份数 null/0 不参与）：
  …. `amount_from_units < …` → 抬到 …，`raised=true`，`min_stake_raised`
  2. `amount > 当前剩余资金 × 0.5` → 截到上限，`capped_amount=true`，`max_pct_of_remaining`
  3. 截完仍 < 50（剩余资金不足 100）→ `amount=0`，`skipped_below_min`（不下注，不硬凑）
  - 「当前剩余资金」= 请求 `remaining_bankroll` → 最新 snapshot `balance` → 基数；同一请求内**按条目顺序逐注扣减**（第二注看第一注扣完后的剩余），响应带 `remaining_bankroll.start/after` 与每条 `remaining_before`。
  - 抬到 … 后金额 ≠ 份 × unit_amount，故同时返回 `amount_from_units` 与最终 `amount`，统计/回测仍按「份」。
  - `cap_mode=warn`：份数和金额上限都不改只写 `warnings`；下限抬升仍执行并标 `raised`。

示例：

```bash
# 默认基数（周一基数空 → 统计本金 10000）：unit=50；2 份=100；5 份截到 3 份=150（cap_reasons=[per_match]）
curl -s -X POST http://127.0.0.1:8787/bankroll/calc -H 'Content-Type: application/json' \
  -d '{"items":[{"match_id":"………|一…","stake_units":…},{"match_id":"………|一…","stake_units":…}]}'

# 下限：本金 5000 → unit=25；1 份=25 抬到 50（raised=true, min_stake_raised）
curl -s -X POST http://127.0.0.1:8787/bankroll/calc -H 'Content-Type: application/json' \
  -d '{"bankroll":…,"items":[{"stake_units":…,"jingcai_date":"………"}]}'

# 上限：剩余 120 → 第 1 注 3 份=150 截到 60（max_pct_of_remaining）；第 2 注剩余 60×0.5=30<50 → 0（skipped_below_min）
curl -s -X POST http://127.0.0.1:8787/bankroll/calc -H 'Content-Type: application/json' \
  -d '{"remaining_bankroll":…,"items":[{"stake_units":…,"jingcai_date":"………"},{"stake_units":…,"jingcai_date":"………"}]}'
```

## 4. 仍 TODO

1. `monday_base_bankroll`：首个周一 11:00 由 `POST /bankroll/snapshots {set_as_monday_base:true}` 写入；之前 calc 回退统计本金 10000。
2. 队/联赛规范名种子 + 导入钩子按别名解析 `*_id`（表已建，暂空）。
3. 预测写入侧（ingest 脚本）填 `stake` / `stake_rule`（简化档 `|s|=5→2`、`|s|=3→1` 由分析师侧定稿后接）。
4. OU / 1X2 / jc 盘口字段扩展——等取数方案定了再出 v1.3。

## 5. 修订

| 日期 | 说明 |
|---|---|
| 2026-10-06 | 草案（`v1_2-schema-increment-draft.md`） |
| 2026-10-06 | 正式 v1.2：拍板 1–3 关闭；UNIQUE(alias)；不用 entities 统一表；bankroll 四接口上线 |
| 2026-10-06 | 补拍板：统计本金 10000 CNY；单注 ≥50、≤ 剩余资金 50%（`stake_amount_limits`，calc 强制并标 raised / capped_amount） |

## 分析师口径确认（2026-10-06）

1. 剩余资金：请求 `remaining_bankroll` → 最新 snapshot → 基数；同请求内逐注扣减。
2. 超限：按比例缩小后向下取整（可到 0）；不做补满分配；缩到 0 标 `skipped`/`capped`。
3. 多策略对比分开调 `/bankroll/calc`；日 10 份只约束实盘敞口那一次调用。

协作中确认：v1.2 通过，前端可接。

## 里程碑补记（2026-10-06）

- 少发进度通知；日常验收走通知维护者。
- 预留 `bankroll_config` 键：`fx_display_currency`（默认 CNY）、`fx_rates`（`base=CNY`，`rates` 空表占位）。**不**阻塞 `/bankroll/calc`；付费汇率源暂缓。
- 旧整点：`kickoff_minute_known=0` 继续兼容。
