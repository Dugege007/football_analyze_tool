# 决策卡：竞彩日早场特殊 cutoff 带拓宽至 ~11:30

> 2026-10-07。状态：**口径已定，代码待改**。不改现网已冻结 V3（177）。  
> 相关：`.cursor/rules/analysis_window_cutoff.mdc`；[`v2_0-walkforward-rolling-weight-scan.md（未公开）`](./v2_0-walkforward-rolling-weight-scan.md（未公开）)；[`../research/shadow-ledger/as-of-betting-iron-rules.md`](../research/shadow-ledger/as-of-betting-iron-rules.md)；[`../research/shadow-ledger/acceptance-leakage-checklist.md`](../research/shadow-ledger/acceptance-leakage-checklist.md)。

---

## 问题

连续窗防泄漏用「竞彩日 + 开赛时刻」决定 `cutoff_hour`。部分**前一日竞彩序号**会在**次日自然日中午前**开赛；旧特殊带只盖到整点 **10**，会漏掉约 **10:01–11:30** 的场，导致同日更早场的赛果不该进／该进的边界与真实「钉在临盘前」不一致。

---

## Old vs New

| | Old（仓库现状） | New（文档工作口径） |
|--|-----------------|---------------------|
| 特殊带 | `0 <= h <= 10` → `cutoff_hour = 21` | 开赛时刻 ∈ **[00:00, 11:30]**（含）→ `cutoff_hour = 21` |
| 晚场 | `h > 10` → `cutoff_hour = h - 4` | **不变**：特殊带以外 → `cutoff_hour = h - 4`（有分钟时按时刻换算到 hour 规则，或等价实现） |
| 轴 | `(jingcai_date / match.date, kickoff_hour)` | 同轴；优先用分钟／`kickoff_at` |
| 影响文件 | `analysis_window_cutoff.mdc` 中 `0<=h<=10` 一行及 `build_cutoff_indices` 同类逻辑 | 仅拓宽特殊带；**禁止**借机改 h-4 或其它窗模式 |

---

## Why

用户 2026-10-07：有些序号属于前一天竞彩日的比赛，会在第二天 **约 12:00 前**（含约 11:30）开赛。特殊带须盖到该范围，否则「当时可知／不可知」边界错位，模拟下注失真。

---

## Impact

1. **规则文件**：`analysis_window_cutoff.mdc` 将 `0<=h<=10` 改为与下节谓词一致（合入 PR 时改，本文不直接改仓库）。
2. **实现**：`asian_poisson_direction_core.build_cutoff_indices`（及任何复制该谓词处）同步。
3. **影子**：cutoff 变更后须重跑相关影子／TW-DECAY 对照（新 fingerprint）。
4. **现网 V3**：**不得**因 cutoff 变更整表 rebuild 覆盖已灌 177；继续 splice / `--skip-existing` 冻结快照。
5. **分钟**：`kickoff_minute_known=0` 且仅有整点时，过渡谓词见下；补齐分钟后再收紧 11:31–11:59。

---

## Proposed predicate（待合入代码）

```text
# Prefer minute-aware kickoff on jingcai-day axis (local …).
# EARLY_BAND → cutoff_hour = 21
# else → cutoff_hour = kickoff_hour - 4   # unchanged late rule

if kickoff_minute is known (or kickoff_at):
    t = hour * 60 + minute
    early = (0 * 60 + 0) <= t <= (11 * 60 + 30)   # [00:00, 11:30] inclusive
else:
    # hour-only fallback; documents that 11:31–11:59 cannot be excluded without minutes
    early = (0 <= h <= 11)

cutoff_hour = 21 if early else (h - 4)
```

等价伪代码（Python 风格）：

```python
def cutoff_hour_for_kickoff(h: int, minute: int | None = None) -> int:
    if minute is not None:
        t = h * 60 + minute
        early = 0 <= t <= 11 * 60 + 30  # 00:00 .. 11:30 inclusive
    else:
        early = 0 <= h <= 11  # fallback; note 11:xx through 11:30 needs minute field
    return 21 if early else (h - 4)
```

历史可用：`X.date < D` 或（同竞彩日且 `kickoff_hour <= cutoff_hour`——若升级到分钟比较，须与实现一致并单独测）；本场及更晚一律不可用。

---

## Checklist before code merge

- [ ] PR 只改特殊带 + 测试用例（10:00 / 11:00 / 11:30 / 11:31 / 12:00）
- [ ] 影子包新 fingerprint；对比旧 cutoff 差异表归档
- [ ] 现网 V3 177 不整表重写
- [ ] 验收清单 §3 勾选

---

## 修订

| 日期 | 变更 |
|------|------|
| 2026-10-07 | 首版：旧 `0<=h<=10` → 新至 ~11:30；谓词；冻结 V3 |

## 竞彩日归属规则（用户 2026-10-10 确认）

1. 一场比赛属于哪一个竞彩日，以它的竞彩编号为准：编号里的星期就是它所属的竞彩日。例如「周五012」属于周五的竞彩日，即使这场比赛在周六 12:00 以后才开赛，它也仍然属于周五的竞彩日。
2. 只有当一场比赛没有竞彩编号时，才使用兜底规则：北京时间 00:00 到 11:30（包含 11:30）开赛的比赛属于前一个竞彩日，其他时间开赛的比赛属于开赛当天的竞彩日。按兜底规则确定的竞彩日必须标记为「待复核」。
3. 旧写法「0 点到 10 点开赛的比赛属于前一个竞彩日」已经作废，以本节为准。
4. 「当日赛程」页按正式库 matches 表的 jingcai_date 字段筛选比赛，这个字段存的已经是按上述规则确定的竞彩日。页面打开时默认显示北京时间此刻所属的竞彩日：北京时间 11:30 之前打开页面时显示前一天，11:30 之后显示当天。
