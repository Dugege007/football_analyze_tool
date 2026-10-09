# 澳门 mid 水位 · 19 场 fixture 映射扩包（2026-10-07 · 已落地）

> 不阻塞先前 SHADOW_S1 158 子集；本包补完后底座扩至 **175**，已升 version 重跑 S1（仍未达 L0，不宣称）。

## 结果摘要

| 项 | 值 |
|---|---|
| 新映射成功 | **19 / 19**（`unmapped` 清空） |
| 方法 | 队名+开赛对齐（非 jc_id） |
| · 国际友谊 → `国际比赛` league `1967686768` | **6** |
| · 世界杯 → `世界杯 2026` league `2127451752` | **13** |
| 主客对调（已在 import 翻转线/水） | match_id **43**（竞彩 哥伦比亚–约旦 ↔ 5DF 约旦–哥伦比亚） |
| 有映射且 macauslot history 有赛前 tick | **17** |
| 有映射但 macauslot asian ticks=[] | **2**：mid=38 委内瑞拉–土耳其；mid=43 哥伦比亚–约旦 |
| CSL 即时重拉 | 首包曾见 **400**，同窗重试 **200**；但冷门 jc_number / 世界杯编号仍不在日窗返回 → **队名路径为准**；API-Football 禁用 |

## 未灌水位的 2 场（映射保留 · 已拍板）

| match_id | match_uid | home–away | fixture_id | 标记 | 原因 |
|---|---|---|---|---|---|
| 38 | 2026-06-06\|六216 | 委内瑞拉–土耳其 | 1511982553 | `macau_ah_unavailable` | 5DF `macauslot` asian history / odds 快照皆空 |
| 43 | 2026-06-07\|日204 | 哥伦比亚–约旦 | 1064503264 | `macau_ah_unavailable` | 同上（且主客对调；即使有盘也需翻转） |

**拍板（2026-10-07 · 足球分析师）**：底座停在 **175**；上述 2 场接受为「无澳门亚盘、不可评」——不另寻源、不硬套皇冠代理进 S1 主指纹。澳门水位补数线收口。

## 映射产物

- `odds-data/backfill/macau_mid_water_fixture_map.json`（n=177，`unmapped=[]`，含 `map19` 元数据）
- 备份：`macau_mid_water_fixture_map.json.bak-pre-map19`
- 池/结果：`odds-data/5dollar/macau-mid-water-fill-2026-10-07/logs/map19_*.json`

## 验收

```bash
cd api
.venv/bin/python scripts/fill_macau_mid_water.py report --db data/v2d3/app.db
# mid 有水 → 175；map fixtures=177 unmapped=0
```
