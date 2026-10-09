# 短方案卡：真实水位 vs 固定 0.95 的公平对照

> 2026-10-07 算法顾问。配合 M5 compare／`settlement_version`；不改 V3 默认哈希。  
> 2026-10-08：盘口术语对齐用户定稿（[`../../schema/v2_0-odds-phase-terminology.md`](../../schema/v2_0-odds-phase-terminology.md)）；不改统计结论与数字。

**术语对齐（本卡）**：结算默认用澳门**临盘（规则）**（字段 `close`）AH 水位；不得「用临盘代替即时」。其他时刻快照标「即时（抓取时间）」（`live`，必带抓取时间）。开赛 ≥23:00 例外保留：`mid`/`close`＝当天 15:00／22:00（规则），`mid_real`/`close_real`＝赛前 8h／1h（对照）。初盘=`open`＝各家首次开盘；竞彩日 11:10＝即时（11:10）。详见 [`../../schema/v2_0-odds-phase-terminology.md`](../../schema/v2_0-odds-phase-terminology.md)。

## 一句话

**比「谁选边更好」时，两边必须同一结算口径；「真实水位」只当灵敏度，不当主裁判。**

## 推荐对照设计

| 对照目的 | 怎么跑 | 主结论看哪条 |
|---|---|---|
| 影子规则 vs V3（选边能力） | 两边都用 **固定 …**（现网默认／V3 哈希不变） | ROI／资金曲线／MDD @ `fixed_0…` |
| 真实赔付接近度 | **同一批注单**再跑一遍 `ah_v4_macau_actual_or_095` | 只当并列副表；不替代上一行结论 |
| 结算口径本身影响 | 同一策略、同一指纹，双结算并排 | 差额 = 水位建模差，不是规则差 |

禁止：A 方案用真实水位、B 方案用 … 直接比 ROI——那是在比「结算假设」，不是比规则。

## `settlement_version` 用法

1. **日用／V3 默认**：继续固定 0.95，不改哈希。  
2. **影子／compare 可选参数**：`ah_v4_macau_actual_or_095`（有 `water_src=actual` 且水位∈(0,2] 用真水，否则回落 0.95）。  
3. 每次 compare 响应里带：`settlement_version`、`n_actual_water`、`n_fallback_095`、`fallback_rate`。`fallback_rate` 高时，「真实水位」结论要标不可靠。

## 公平报告最低字段

- 主表：`settlement_version=fixed_0…` 下的 hits／coverage／ROI／MDD（与互斥页 L0–L2 门槛一致）。  
- 副表（可选）：同注单在 `actual_or_0…` 下的 ROI／MDD 差值 Δ。  
- 若副表 Δ 很大但主表无优势：写清「优势来自水位假设，不是选边」。

## 给前后端

- 后端：compare 默认仍 0.95；可选 version 进影子；summary 带 actual／fallback 计数。  
- 前端：叠曲线默认按当前选中的 `settlement_version` 画一条资金线；若同时开双结算，两条线分色并注明口径，勿混成一条。
