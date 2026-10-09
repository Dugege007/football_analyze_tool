# 待改清单：竞彩日早场特殊窗拓宽至 11:30

> 2026-10-07 UTC+8。**状态：本机 B 已对齐**（仓库 私有仓 PR 谓词对照完成；现网 V3 177 未动；DUAL_WRITE 仍关）。  
> 触发：足球分析师 — 旧 `0<=h<=10`→`cutoff_hour=21` 不够；**kickoff 时刻 ∈[00:00, 11:30]（含）→ cutoff=21**；**12:00 起仍走 h-4**。  
> 决策卡：[`v2_0-jingcai-day-early-kickoff.md`](./v2_0-jingcai-day-early-kickoff.md)  
> 验收清单 §3：[`../research/shadow-ledger/acceptance-leakage-checklist.md`](../research/shadow-ledger/acceptance-leakage-checklist.md)  
> 本机验收：[`v2_0-cutoff-1130-b-align-acceptance.md`](./v2_0-cutoff-1130-b-align-acceptance.md)

## 拍板（2026-10-07 · 分析师）

| 项 | 口径 |
|----|------|
| **B 一并扩** | 与防泄漏 **同一谓词**：开赛时刻 ∈[00:00, 11:30]（有分钟用分钟；仅整点则 `h≤11`，并注明 11:31–11:59 需分钟） |
| 适用范围 | 凡「竞彩日凌晨特殊窗／归属／cutoff=21」语义：含 `collection_schedule`、`resolve_kickoff_dt`/dispatch、`import_lib`、probe |
| **勿误改** | `≥23:00` 的规定中盘 15:00／临盘 22:00 通道（`23` 仍属 rule 固定钟点，与早场拓宽正交） |
| 仓库 | CloudAgent [<internal-agent>](<internal-agent-link>) · PR 私有仓 PR（对照 diff，未 clone） |
| 本机状态 | **B 四处 + README + 单测已改完**；现网 V3 不重算；DUAL_WRITE 关；私有仓 PR 不 merge |

---

## 本机 B 改动文件（2026-10-07）

| 文件 | 关键符号 | 变更摘要 |
|------|----------|----------|
| `app/collection_schedule.py` | `is_early_kickoff_band`、`EARLY_BAND_END_MINUTE_OF_DAY`、`OVERNIGHT_OR_LATE_HOURS`、`is_rule_fixed_clock`、`is_rule_legacy_applicable`、`calc_collection_times*`、`resolve_collection_ats*`、`channel_targets` | 早场带扩至 [00:00,11:30]（分钟感知；仅整点 0–11）；保留 ≥23→15:00/22:00 |
| `app/main.py` | `resolve_kickoff_dt`、`calc_collection_times`、`resolve_collection_ats`、`dispatch_pending` | 合成路径早场带 +1 日；优先 `kickoff_at`；dispatch 有 ka 传分钟；docstring 对齐 |
| `scripts/import_lib.py` | `sync_kickoff_at` | 仅 hour 合成：`kickoff_hour <= 11` → +1 日 |
| `scripts/import_odds_timeline_probe.py` | `ensure_probe_match` | 用 `is_early_kickoff_band(h, minute)` 挂前一日 |
| `README.md` | dispatch / 0.3.9 段 | 旧「0–10」改新谓词表述 |
| `tests/test_early_kickoff_band.py` | 新增 | 10/11/11:30/11:31/12/23 单测 |

---

## 0. 范围与纪律（仍有效）

| 项 | 约定 |
|----|------|
| 现网 V3 | **不重算**；177 快照继续 splice / `--skip-existing` 冻结 |
| DUAL_WRITE | 关 |
| clone / merge | 不 clone 作者的私有分析仓；不 merge 私有仓 PR |
| A 类（仓库 cutoff） | 由 私有仓 PR 承担；本机 API **无** `analysis_window` 实现 |

**两类硬编码：**

1. **A · 防泄漏 cutoff**：仓库 `analysis_window_cutoff` / `build_cutoff_indices`（私有仓 PR）。
2. **B · 竞彩日挂日 / 规定通道 mid·close**：本机已对齐同谓词。

---

## 1. A 类 · 防泄漏 cutoff（仓库；本地笔记）

仓库谓词已在 私有仓 PR；本地笔记旧「0<=h<=10→21」表述可后续随改（非本刀阻塞）：

| 路径 | 状态 |
|------|------|
| `v2_0-jingcai-day-early-kickoff.md` | 决策卡已新口径 |
| `acceptance-leakage-checklist.md` | 文档已新口径；勾选随合入 |
| `v2_0-walkforward-rolling-weight-scan.md（未公开）` 等 | 旧表述 PENDING 文案对齐（非代码） |

---

## 2. B 类 · 已对齐（原硬编码清单）

见上「本机 B 改动文件」。`fill_macau_mid_water.py` 走 `channel_targets`，随 collection_schedule 自动联动，无需单独改谓词。

Schema／约定笔记 §2.2 旧「0–10」表述：非本刀必改；决策相关句已在 pending／ACCEPTANCE／README 更新。

---

## 3. 底座抽查（现网 `data/app.db`，2026-10-07）

| 开赛时钟 | n | early | rule fixed 15/22 | 结果 |
|----------|--:|:-----:|:----------------:|:----:|
| 10:00 | 10 | ✓ | ✓ | PASS |
| **11:00**（旧带外新带内） | 5 | ✓ | ✓ | PASS |
| 12:00 | 3 | ✗ | T−8/T−1 | PASS |
| ≥23:00 | 7 | ✗ | ✓ | PASS |

V3 `CFFXDJ_5_V3` 行数 **177**；`2026-06-06|六204` 仍「主」。

---

## 4. 计数摘要

| 项 | 值 |
|----|-----|
| 本机 B 代码文件 | collection_schedule / main / import_lib / probe + README + 单测 |
| 单测 | `tests/test_early_kickoff_band.py` + 既有 water/settlement **15 passed** |
| 现网 V3 | **177 未动** |
| 本笔记 | `docs/schema/v2_0-cutoff-1130-pending-changes.md` |
| ACCEPTANCE | `docs/schema/v2_0-cutoff-1130-b-align-acceptance.md` |
