# 开放数据缺口清单（2026-10-07 · 澳门/1X2 线收口后）

现网 `DUAL_WRITE_ODDS_ASIAN=false`。影子多在 `data/v2d3`。

## 已收口 / 已挂影子

| 项 | 状态 |
|---|---|
| 澳门 mid 真实水位 | v2d3 **175/177**；38/43=`macau_ah_unavailable` |
| SHADOW_S1 | v2d3·… · hits=… · 影子至 …… |
| 完整 1X2（william/macau/crown open+close） | v2d3 **175/177**（同 2 场空） |
| SHADOW_N1 | v2d3·… · hits=… · pnl=… · `upper_side_n1` 冻结 |
| settlement actual_or_095 | API **0.3.14**；默认仍 0.95 |
| 开赛到分钟（v2d3） | **177/177 known=1**（5DF `fixture_ko`；冲突 39 记日志仍写入副本）；见 `v2_0-kickoff-minute-fill.md` |

## 仍开着

| 缺口 | 现状 | 建议下一刀 |
|---|---|---|
| **现网开赛到分钟** | 现网 177 仍 `known=0` / 全整点 | 另拍板再从 v2d3 导入；day_skew 8 场已复核 mapping_ok / day_skew_confirmed（见 kickoff-minute-fill 报告） |
| **SHADOW_N3** | blocked：缺 1X2 history + 联赛×盘口平赔 P90；177 不够稳 | 另包：扩历史窗口建分位表后再挂 |
| **SHADOW_N5** | blocked：无泊松/DC 管线、无平博去水 | 算法顾问方案卡后再开工 |
| **现网澳门水位 / mid** | 现网 odds_asian 澳门水位仍空、无 mid | 等拍板再从 v2d3 导入；DUAL_WRITE 仍关 |
| **现网完整 1X2** | 仅 `odds_euro_home.home_win` | 等拍板再同步 `odds_snapshot` 或新表 |
| **predictions.csv ingest** | 覆盖至 2026-06-06；2607+ 待分析师刷新 | 分析师出新 CSV 后重跑 ingest |
| **S8 skip2 支路** | v0 关闭（缺连续 tick） | timeline 密度够再开 |

## 不做

- 38/43 另寻源或皇冠代理进 S1/N1 主指纹
- N3 在 177 底座上硬建 P90
