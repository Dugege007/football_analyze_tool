# v1.8 · 结算可选真实水位 `ah_v4_macau_actual_or_095`（API 0.3.14 · 2026-10-07）

> 对齐算法顾问方案卡：`research/shadow-ledger/settlement-fair-compare-card.md`。  
> 引擎：`match-analysis-api/app/backtest.py`；接线：`app/main.py`。  
> **不改** V3 预测哈希 `<指纹已移除>`；现网 `DUAL_WRITE_ODDS_ASIAN=false`；现网 `odds_asian` 澳门水位不动。

## 1. 一句话

**主对比两边必须同一 `settlement_version`（默认固定 0.95）；真实水位只做同注单灵敏度副表，不当选边主裁判。**

## 2. `settlement_version`

| 值 | 用途 | macau 水位 |
|---|---|---|
| `ah_v4_water_midpoint`（**默认**） | 日用 / V3 对照 / 影子选边主表 | 固定 0.95 → `juice_source=fixed_macau` |
| `ah_v4_macau_actual_or_095`（可选） | 同注单灵敏度 | `water_src=actual` 且水位∈(0,2] → `actual`；否则回落 0.95 → `fallback` |

- 请求：`POST /strategies/{id}/validate` 与 `POST /strategies/compare` 顶层字段 `settlement_version`，或 `params.settlement_version`。  
- 非法值 → HTTP 400。  
- 进指纹（`extra.settlement_version` + `bankroll_rules.settlement_version`）；换版本 → 新 run，不污染默认缓存。  
- 其它书（crown/william/…）行为与 v1.7 相同（中点/真实水位；与本版 macau 开关无关）。

## 3. 公平对照字段（summary / stack / compare 顶层）

| 字段 | 含义 |
|---|---|
| `n_actual_water` | 有方向场中 `juice_source=actual` 的笔数 |
| `n_fallback_095` | 有方向场中 `juice_source=fallback` 的笔数 |
| `fallback_rate` | `n_fallback_095 / (n_actual_water + n_fallback_095)`；分母 0 → `null` |
| `juice_*_count` | 既有明细：`actual` / `fixed_macau` / `tier_midpoint` / `fallback` |

`fallback_rate` 偏高时，「真实水位」结论应标不可靠（前端可提示）。

## 4. compare：主表一条线；可选双结算副表

- **默认**：只返回当前请求 `settlement_version` 的 items + stack（一条共用资金曲线 + MDD）。  
- **`include_settlement_sensitivity=true`**（且主表不是已经是 actual 口径）：  
  - 同注单再跑一遍 `ah_v4_macau_actual_or_095`；  
  - 响应 `sensitivity`：`items[]` / `stack`（第二套 series）/ `delta_vs_primary`（Δ pnl / Δ roi）；  
  - 副表**不落库、不进主指纹**；不替代主表选边结论。  
- 禁止：A 用真水、B 用 … 直接比 ROI（响应带 `fair_compare_note`）。

## 5. 验收 curl

```bash
# 健康 + dual_write 关
curl -s http://127.0.0.1:8787/health
# → {"ok":true,"version":"0.3.14","dual_write_odds_asian":false}

# V3 默认仍 … → pnl …
curl -s -X POST http://127.0.0.1:8787/strategies/1/validate \
  -H 'Content-Type: application/json' -d '{"scope":"jingcai","settle_book":"macau_close"}'

# 可选灵敏度（现网 macau 水位空 → n_fallback_0…=…, fallback_rate=…, pnl 仍 …）
curl -s -X POST http://127.0.0.1:8787/strategies/1/validate \
  -H 'Content-Type: application/json' \
  -d '{"settlement_version":"ah_v4_macau_actual_or_095"}'

# M5 stack（默认固定 0.95）
curl -s -X POST http://127.0.0.1:8787/strategies/compare \
  -H 'Content-Type: application/json' \
  -d '{"strategy_keys":["CFFXDJ_5_V3","TEST_V3_MIRROR"],"stake_mode":"unified","stake_rule":{"default_units":…,"use_prediction_stake":false}}'

# 双结算：主表 0.95 + sensitivity 真水副表（两套 series）
curl -s -X POST http://127.0.0.1:8787/strategies/compare \
  -H 'Content-Type: application/json' \
  -d '{"strategy_keys":["CFFXDJ_5_V3","TEST_V3_MIRROR"],"stake_mode":"unified","stake_rule":{"default_units":…,"use_prediction_stake":false},"include_settlement_sensitivity":true}'
```

V3 哈希自检：`.venv/bin/python scripts/v3_hash.py` → `<指纹已移除>`。

## 6. 实测（2026-10-07）

| 场景 | settlement_version | pnl | juice | n_actual / n_fallback / rate |
|---|---|---|---|---|
| 现网 V3 validate 默认 | `ah_v4_water_midpoint` | **…** | fixed_macau=… | … / … / null |
| 现网 V3 + actual_or_0… | `ah_v4_macau_actual_or_0…` | … | fallback=… | … / … / **…** |
| 现网 compare stack 默认 | `ah_v4_water_midpoint` | stack **…**，MDD … / … | — | — |
| 副本 **v2d3** V3 actual | `ah_v4_macau_actual_or_0…` | …（该注真水恰 …） | actual=… | **… / … / …** |
| 副本 v2d3 MIRROR actual | 同上 | …（… 注全 actual） | actual=… | **… / … / …** |

副本路径：`data/v2d3/app.db`（仅验证；不切现网、不双写）。

## 7. 代码与单测

- `app/backtest.py`：`SETTLEMENT_MACAU_ACTUAL`、`resolve_juice(..., settlement_version=)`、`summarize` 公平字段。  
- `app/main.py`：0.3.14；`_resolve_settlement_version`；validate/compare 可选字段；`include_settlement_sensitivity`。  
- `tests/test_settlement_version.py`（5）+ `tests/test_water.py`（4）。

## 8. 非目标

- 不改现网 `odds_asian` macau 水位；不打开 DUAL_WRITE。  
- 不把 actual 设为日用默认；不改 V3 定义/预测。  
- stack 灵敏度副表不落库。
