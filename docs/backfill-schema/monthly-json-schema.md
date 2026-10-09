# 作者的私有分析仓 月度 JSON 口径（回填用）

来源：仓库 `00_数据集/README_数据说明.md` + `2607.json` 样例。研读时间：2026-10-05 21:05 UTC+8。

## 顶层

`YYMM.json` = **数组**，每场比赛一个对象，固定 5 个一级字段：

`match` / `result` / `stats` / `odds` / `meta`

## match

| 字段 | 类型 | 说明 |
|---|---|---|
| date | YYYY-MM-DD | **竞彩日**：开赛小时 0–10 记前一天 |
| weekday | 一…日 | 对应竞彩日 |
| jc.id / jc.weekday / jc.no | 如 日004 | API 回填暂无免费源 → **一律 null**，不编造 |
| competition.name/type/stage | 字符串 | 赛事名/联赛或杯赛/阶段 |
| kickoff_hour | 0–23 int | 开赛整点（UTC+8） |
| teams.home / teams.away | 字符串 | 队名 |

## result

home_goals, away_goals, total_goals (int), wdl ∈ {胜,平,负}；未完赛可为 null。

## stats（可暂 null）

- recent.last10/last6：home/away 的 gf/ga
- recent.home_streak_last6
- h2h.matches / h2h.last6.home_gf|home_ga / h2h.home_streak_last6
- rank.home/away
- popularity_diff（雷速独有，API 无 → null）
- injury / weather

## odds

### asian
- **macau**：仅 `open`/`close` 为盘口数值（主让正/主受让负），**无水位、无 mid**
- **crown** / **william**：`open`/`mid`/`close` 各含 `home_water`, `handicap`, `away_water`

### euro_home_win
- macau/william 的 open/close：**仅主胜**

### jc_home_win
- open/close：竞彩主胜 → API 无则 **null**

## meta

- source_file, month (如 2609)
- 回填扩展（仓库可忽略）：source, uid, collected_at, water_scale, jingcai_fields

## 回填约定（本目录）

1. 竞彩相关字段永不编造，保持 null。
2. API 返回欧式小数亚盘赔率时，水位按 `HK_water = decimal - 1` 写入 `home_water`/`away_water`，并在 meta 标 `water_scale=hk_water_from_decimal`。
3. 额外机构（bet365/pinnacle）写在 `odds.asian.bet365` 等扩展键，不覆盖 macau/crown/william 空位（除非来源明确是该机构）。
4. InferSports 停更期间 open=close=同一快照，meta.stale=true。
5. 大小球先放 `odds.ou_*` 扩展，不改原 schema。
