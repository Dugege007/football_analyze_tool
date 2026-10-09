# 影子方案台账

> 创建：2026-10-06。只登记可测规则，不改生产库、不改 V3 默认。  
> 2026-10-08：盘口术语／CLV 三列对齐用户定稿；不改统计结论与数字。  
> 明细表：[`shadow-schemes.csv`](./shadow-schemes.csv)  
> 互斥分桶＋最少样本：[`mutex-buckets-min-n.md`](./mutex-buckets-min-n.md)  
> 结算公平对照：[`settlement-fair-compare-card.md`](./settlement-fair-compare-card.md)  
> 预测落地口径：[`prediction-landing-cards.md（未公开）`](./prediction-landing-cards.md（未公开）)  
> 来源：`bilibili-whsds/methodology-summary.md`、`bilibili-survey/survey.md`、`in-play-prediction-review.md`、`xg-sources.md`。

## 约定

| 项 | 口径 |
|---|---|
| 结算（默认） | 澳门**临盘（规则）**（`close`）AH；赢侧水位 ×**0.95**；走水单独统计 |
| 对照 | 同批须报 CLV 三列（均以**平博**为准）：CLV（规则）=`close`；CLV（真实）=例外场 `close_real`；CLV（收盘）=`last_prematch`（新鲜度暂定 15 分钟，待密度统计复核）。指纹写明用哪列；三列样本不合并 |
| 盘口术语 | 见 [`../../schema/v2_0-odds-phase-terminology.md`](../../schema/v2_0-odds-phase-terminology.md)：初盘=`open`（各家首次开盘）；中盘／临盘默认=`mid`/`close`；开赛 ≥23:00 例外保留规则 15:00／22:00 + 真实 `mid_real`/`close_real` 对照；11:10＝即时（11:10） |
| 状态 | `pending_shadow` / `blocked_need_*` / `running` / `reported` / `rejected` |
| 默认汇报日 | **2026-11-06**（不足则延期并写新汇报日） |
| 不做 | 国内「凯利指数=庄家意图」/ 假凯利离散度跟庄（大角已驳） |

## 本批挂入（用户 2026-10-06 同意）

- 扫地僧优先：`S1`、`S2`、`S8`（「看不清就不下」叠在 V3 / CFFXDJ_5_V3 上；对话里曾称第三优先为 S3，台账 id 以研究文档 **S8** 为准）。  
- B 站新：`N1`、`N3`、`N4`、`N5`；`N2` 为 `blocked_need_median_table`（先建联赛×盘口「中庸平」表再挂测）。  
- 滚球 stub：`IP-B`、`IP-A`；BSD xG stub：`XG-BSD-1`。

## id 规则

- `S*` = 扫地僧；`N*` = B 站调研；`IP-*` = 滚球；`XG-*` = xG 特征。  
- **id 全局不重复**；变体用后缀（如 `S2-a`），新开行登记，不改写旧行语义。

## API 对齐（0.3.10）

`validate` / `compare` summary 已写入 `n_eligible` / `hits` / `coverage`（及可选 `multi_hit_count=null`）。回测映射：`n_eligible≈n`，`hits≈signal_count`，`coverage=hits/n_eligible`（无分母→`null`）。旧字段保留。前端可将 `hits<…` 的 ROI 灰显（L1 门槛，实现归前端）。
