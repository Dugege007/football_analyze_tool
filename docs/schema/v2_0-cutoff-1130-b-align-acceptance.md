# ACCEPTANCE · 本机 B 早场带对齐 [00:00, 11:30]

> 2026-10-07 UTC+8。对照仓库 PR 私有仓 PR（CloudAgent <internal-agent>）；**未 clone / 未 merge**。  
> 现网 V3 **未重算**；DUAL_WRITE **关**。

## 谓词（与 私有仓 PR 一致）

- 关键函数：`app.collection_schedule.is_early_kickoff_band(h, minute=None)`
- 有分钟：`0 <= h*60+minute <= 11*60+30`（含 11:30）
- 仅整点：`0 <= h <= 11`（注释：11:31–11:59 需分钟才能排除）
- `is_rule_fixed_clock` = 早场带 **或** `h >= 23` → mid/close = 15:00/22:00
- 12:00 起：非早场 → T−8h / T−1h
- ≥23:00 通道未改

## 改动文件

1. `match-analysis-api/app/collection_schedule.py`
2. `match-analysis-api/app/main.py`（`resolve_kickoff_dt` / dispatch）
3. `match-analysis-api/scripts/import_lib.py`（`sync_kickoff_at`）
4. `match-analysis-api/scripts/import_odds_timeline_probe.py`
5. `match-analysis-api/README.md`（决策相关句）
6. `match-analysis-api/tests/test_early_kickoff_band.py`（新增）

## 抽查（`data/app.db`）

| 样本 | 期望 | 实测 |
|------|------|------|
| 10:00（n=10） | early + rule 15/22 | PASS |
| 11:00 五场（含 `2026-06-19|五032` 等） | **现应进特殊／rule 固定钟点** | PASS |
| 12:00 三场 | 非早场；04:00/11:00 | PASS |
| ≥23（n=7） | 非 early；仍 15:00/22:00 | PASS |
| `resolve_kickoff_dt` 合成 h=11 | jingcai+1 日 11:00 | PASS |
| `resolve_kickoff_dt` 合成 h=12 | 同 jingcai 日 12:00 | PASS |
| 优先 `kickoff_at` | 11:00 场用库内次日 11:00 | PASS |

## 硬闸

| 项 | 结果 |
|----|------|
| `CFFXDJ_5_V3` 行数 | **177** |
| `2026-06-06|六204` direction | **主**（未改） |
| DUAL_WRITE | 关 |
| pytest | 15 passed（含 early-band 6 + water/settlement） |

## 笔记

- 待改清单状态：[`v2_0-cutoff-1130-pending-changes.md`](./v2_0-cutoff-1130-pending-changes.md) → **本机 B 已对齐**
