# fixture ↔ 竞彩场消歧规则草案（待拍板）

> 生成：2026-10-07（UTC+8）。依据后端 CONDITIONAL 验收 + QA 抽检；**未实施**，实施前须用户确认。  
> **用户拍板（2026-10-07）**：开赛差阈值 **≤90 分钟**；邻日 `med` 可进影子对照、不盖手工；**2025-10-25 前**不靠 CSL 对竞彩（永久跳过竞彩编号映射，其他字段照常补）。
>
> 硬约束：消歧完成前不进副本、不喂影子；`DUAL_WRITE` 关。

---

## 1. 为什么要消歧

- 竞彩日与 5DF `/chinasportslottery` 日窗常差 **±1 日**。
- 现 map 中约 **1078** 个 `fixture_id` 挂了两个 `match_uid`（邻日双胞胎）；约 834 条 done 落在 neighbor 侧。
- 若不消歧入库，同一盘口会污染两场的 as-of 特征与影子 ROI。

---

## 2. 目标状态（通过线）

| 项 | 标准 |
|---|---|
| `match_uid` | 唯一标识一场竞彩场 |
| `fixture_id` | **最多**绑定一个 `match_uid`（可空=未映射） |
| 一对多 | 清零；或仅剩已标注 `ambiguous` 且**禁止入库** |
| done 双写 | 同一 `fixture_id` 只保留一条权威 done 行 |

---

## 3. 匹配优先级（建议）

对每个待绑定的 sporttery/`match_uid`：

1. **P0 精确**：同 `jc_id`（或规范化竞彩编号）且 **jingcai_date == CSL file_date**  
2. **P1 邻日**：仅当 P0 无命中；`file_date ∈ {jd−1, jd, jd+1}`，且  
   - 主客队经别名规范化后双向匹配；  
   - 开赛时刻差最小（阈值建议 ≤90 分钟，可调）；  
3. **P2 拒绝**：多候选得分并列、队名冲突、或缺开赛时刻 → 标 `ambiguous`，不进 pending/入库。

**禁止**：无队名校验的「只按 jc_id ±1」裸绑。

---

## 4. 冲突裁决（同一 fixture_id → 多 match_uid）

| 情况 | 裁决 |
|---|---|
| 一侧 P0、一侧 P1 | **保留 P0**，邻日侧断开并记 `rejected_neighbor` |
| 两侧皆 P1 | 比开赛时刻差；仍平 → `ambiguous` |
| done 已拉两侧 | 权威保留 P0（或时刻更近的一侧）；另一侧从导入白名单剔除，raw 文件可留档不删 |
| 双 worker 重复 done | 同 `fixture_id` 去重，保留最新完整 fill |

---

## 5. 元数据（每条映射必带）

- `map_source`: `exact` \| `neighbor_m1` \| `neighbor_p1` \| `manual`  
- `map_confidence`: `high` \| `med` \| `low`  
- `kickoff_delta_min`（可空）  
- `disambiguated_at` / `rule_version`（如 `disambig-v0`）

导入钩子：**仅** `map_confidence∈{high,med}` 且非 ambiguous 可写副本。

---

## 6. 验收抽样

- 随机 50 对曾双胞胎：人工/脚本核对队名+开赛  
- 一对多计数 = 0（或仅 ambiguous 清单）  
- 导入试跑 skip-existing，现网手工行 mid/close 不变

---

## 7. 已拍板

- 开赛时刻差阈值：**≤90 分钟**
- 邻日 `med`：可进影子对照层，不盖手工
- 2025-10-25 前：跳过 CSL 竞彩编号映射；其他需填字段继续补
