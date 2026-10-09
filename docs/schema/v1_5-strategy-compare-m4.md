# 方案对比 M4（后端 · schema 切片 v1.5 · 2026-10-06）

> 状态：**可测**（简化亚盘结算 + 折线序列；完整精算可迭代）。
> OpenAPI 切片：`openapi-v1.5-compare-draft.yaml`。
> API 版本：`0.3.4`（在 M3 `0.3.3` 之后）。
> 表结构沿用 M2/M3（`strategy_defs` / `strategy_validation_runs` / `strategy_validation_cache`），**无新 DDL**。

## 目标

1. **加厚** `POST /strategies/{id}/validate`：指纹命中仍 `reused:true`；未命中写 run + **非空** summary metrics + **若干 cache 行**（不再空 `items:[]` 占位）。
2. **对比** `POST /strategies/compare`：多方案资金/指标**时间或场次序列**，供前端折线；`stack` 叠加字段先 stub。
3. **不改**现网亚盘语义与 `CFFXDJ_5_V3` 默认行为。

## 与 M3 关系

| 能力 | M3 | M4 |
|---|---|---|
| compose / templates / fingerprint | ✅ | ✅ 沿用 |
| validate 写 run | ✅ | ✅ |
| summary | placeholder 空壳 | **n / pnl / roi / skipped…** |
| cache | 可空 | **predictions×赛果×结算盘** 写出行 |
| compare / 折线 | ❌ | ✅ |
| stack 叠加 | ❌ | stub（`null`） |

M3 空占位 run（`summary.placeholder=true` 且 cache 0 行）：同指纹再次 validate / compare(auto_validate) 时会**删除空 stub 并按 M4 重算**（不算「重复写真实结果」）。已有非空 cache 的指纹仍严格 `reused`。

## run_fingerprint（M4）

`sha256(canonical({config_fingerprint, scope, settle_book, params, strategy_def_id}))`。
相对 M3 增加 `strategy_def_id`，避免同模板多方案撞指纹。

## validate 加厚口径

### 输入

- `strategy_defs` 的 `strategy_key` → 对齐 `predictions.strategy`
- `settle_book`（默认 `macau_close`）→ `odds_asian.book` + `phase`（`macau` + `close`）
- `config.juice`（默认 `0.95`）；有水位则优先用该侧 water，缺水位用 juice
- `scope` 过滤 `matches.scope`（默认 `jingcai`）
- `params.shadow` 等进 fingerprint；可选 `linked_prediction_batch` 仅记入 summary（尚无独立 batch 列）

### 简化亚盘结算（`settlement: simplified_ah_v1`）

1. 仅处理有 **赛果** 且有 **结算盘口线** 的场；缺则计入 `skipped`（不写 cache 行）。
…. `direction=不下注` → `result_code=skip`，`pnl_units=…`，`stake_units=…`（**仍写 cache 行**，计入 `n`）。
3. `direction∈{主,客}` → 主队视角 handicap；四分盘拆成相邻半/整盘各 50%；赢侧支付 `juice×stake`，输全损；半赢/半输按半份。
4. 默认 `stake=1` 份（`predictions.stake` 为空时）；**非**完整 Kelly / 闸门精算。

### summary 主要字段

| 字段 | 含义 |
|---|---|
| `n` / `sample_count` / `cache_rows` | 写入 cache 的行数 |
| `bet_count` | 实际下注（主/客）笔数 |
| `skip_dir_count` | 不下注行数 |
| `skipped` + `skipped_detail` | 无法结算（无赛果/无盘口/坏方向） |
| `pnl` / `pnl_units` / `units` | 累计盈亏（份） |
| `stake_risked` / `roi` | 风险份与 ROI（无下注则为 null） |
| `wins` / `losses` / `pushes` / `win_half` / `lose_half` | 结果计数 |
| `settlement_note` | 口径说明（placeholder 边界） |
| `placeholder` | 恒为 `false`（M4） |

### cache 行含义

- 唯一键：`(run_id, match_id, market)`；本阶段 `market=ah`
- `side`：主/客；不下注为 `null`
- `line`：结算 handicap
- `result_code`：`win|win_half|push|lose_half|lose|skip`
- `pnl_units`：以份计
- `metrics_json`：日期、比分、juice_used、settlement 标签等

## compare

### 请求 `POST /strategies/compare`

```json
{
  "strategy_ids": [1],
  "strategy_keys": ["CFFXDJ_5_V3"],
  "scope": "jingcai",
  "settle_book": "macau_close",
  "shadow": true,
  "params": {},
  "auto_validate": true
}
```

- `strategy_ids` 与 `strategy_keys` 可混用；key 取该 key **最新 version** 一行。
- `auto_validate=true`（默认）：无匹配 fingerprint 的 run 时触发与 validate 相同的最小回测写入。
- `auto_validate=false` 且无 run → 该项 `status=missing_run`。

### 响应

```json
{
  "scope": "jingcai",
  "settle_book": "macau_close",
  "params": {"shadow": true},
  "items": [
    {
      "strategy_def_id": 1,
      "strategy_key": "CFFXDJ_5_V3",
      "version": "2026.10.06",
      "status": "ok",
      "run_id": 10,
      "summary": {"n": …, "pnl": …, "...": "..."},
      "series": {
        "x": ["2026-06-01", "..."],
        "match_ids": [1, 2],
        "pnl": […, …],
        "cumulative_pnl": […, …],
        "result_codes": ["skip", "skip"]
      },
      "reused": true
    }
  ],
  "stack": null,
  "stack_note": "M4 stub：多方案叠加未计算"
}
```

`series` 按 `jingcai_date`、`match_id` 排序；`cumulative_pnl` 供折线。

### stack stub

- 响应顶层 `stack: null` + `stack_note`。
- 后续可扩展为多方案加权/串联权益曲线；本阶段不实现。

## 非目标

- 不改 `CFFXDJ_5_V3` / 现网亚盘方向语义、bankroll、dispatch。
- 不做完整 Kelly、CLV、多机构对比、状态机闸门实盘。
- 不新增 DDL；未知 config 键仍进 `extras`。
- 不做真实资金叠加（stack）与组合再平衡。

## 验收（本地）

```bash
curl -s http://127.0.0.1:8787/health   # version 0.3.4

# validate CFFXDJ_5_V3（id=1）；summary.n>0，cache 非空；二次 reused
curl -s -X POST http://127.0.0.1:8787/strategies/1/validate \
  -H 'Content-Type: application/json' -d '{"shadow":true}'

curl -s -X POST http://127.0.0.1:8787/strategies/compare \
  -H 'Content-Type: application/json' \
  -d '{"strategy_ids":[1],"auto_validate":true}'
```
