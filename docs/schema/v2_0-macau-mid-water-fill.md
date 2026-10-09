# v2.0 · 澳门中盘水位补数（2026-10-07）

> 对齐足球分析师口径：5DF `macauslot` asian history；欧式小数−1=港盘水位；`rule.mid` 取 ≤中盘时刻最后赛前 tick；稀 tick 标 `approx` + `tick_age_hours`；只填 NULL、不覆盖旧手工；现网 `DUAL_WRITE_ODDS_ASIAN` 仍关。

## 1. 缺口（补前）

| 库 | macau open | macau mid | macau close | open/close 水位 NULL | mid 水位 |
|---|---|---|---|---|---|
| **现网** `data/app.db` | 177 行有盘口 | **0** | 177 行有盘口 | **177/177** | — |
| v2d1 | 同现网 | 0（非 probe） | 同现网 | 全 NULL | — |
| v2d2 | 同现网 | 0（非 probe；探针 2 行） | 同现网 | 全 NULL | 探针有 |
| **识别** | `odds_asian.book=macau` | 无 mid 行 | water_src 全 NULL | 旧手工 JSON 仅盘口数字、无水位；`extras_json` 空 | timeline 仅 D1 探针 2 场 |

`odds_timeline`：现网无表；副本仅探针 FRA–BEL / KOR–UZB。

旧手工不可覆盖识别：`home_water/away_water` 已非空，或 `water_src` 已有且非本源 → SKIP（本批澳门原全 NULL，跳过 0）。

## 2. 可用源

| 源 | 状态 | 用途 |
|---|---|---|
| **5DF** `/v1/fixtures/{id}/odds/history?bookmaker=macauslot&market=asian` | 密钥已配，`plan=…`，限速自限 ≤30/分 | **主源** |
| JC 日缓存 `research/fixture-volume-and-scheduling/cache/jc/` | 已有 2026-06/07 | fixture_id 映射 |
| InferSports | 密钥已配 | **未用**（无澳门亚盘 history 口径） |
| 已有 probe 快照 | 仅 2 场探针 | 不覆盖 177 底座 |
| API-Football | **已暂停，禁用** | — |

### 映射

- 脚本产出：`$ODDS_DATA_DIR/backfill/macau_mid_water_fixture_map.json`
- **可映射 158 / 177**（按 `jingcai_date`±1 × `jc_norm` 对齐缓存）
- **未映射 19**：缓存无对应竞彩编号（多为友谊赛冷门号）；CSL 即时重拉本环境返回 400，留待下一步

## 3. 补数结果（副本 `data/v2d3/app.db`）

从 `v2d2` 拷贝新建 **v2d3**；现网 / v2d1 / v2d2 **未改** `odds_asian`。

| 指标 | 数值 |
|---|---|
| 拉取 history | **158/158** HTTP 200，赛前 tick 均 >0 |
| 写入 macau mid 行（非 probe） | **158**（`water_src=actual`，`source=5df_macauslot_history`） |
| open/close 水位从 NULL→actual | 各 **158** |
| 仍缺 mid / 仍 NULL 水位 | **19** 场（未映射） |
| mid 标 `approx=true`（tick_age>2h） | **96/158**（澳门稀 tick，预期内） |
| timeline_seg 新增 | 2796 |
| odds_snapshot 新增（rule open/mid/close + actual t8/t1） | 790 |
| 旧手工覆盖 | **0** |

### 补后缺口表

| 库 | mid 行(有水) | open 有水 | close 有水 | open/close 仍 NULL |
|---|---|---|---|---|
| 现网 | 0 | 0 | 0 | 177+177 |
| v2d1 | 0 | 0 | 0 | 177+177 |
| v2d2 | 0（非 probe） | 0 | 0 | 177+177 |
| **v2d3** | **158** | **158** | **158** | **19+19** |

## 4. S1 解锁评估

规则卡要求：澳门 asian **open/mid/close 三段盘口 + 上盘真实水位**（B2/B3）。

| 项 | 结论 |
|---|---|
| `s1_feature_ready`（非 probe） | **158 / 177** |
| 立刻挂全底座 177 预测 | **否**（差 **19** 场无 fixture→无 mid/水） |
| 可挂子集影子 | **是**：对已补 158 场可挂 `SHADOW_S1`（fingerprint 须含 `water_src=actual`、`source=5df_macauslot_history`、`approx` 覆盖率）；**勿**与 V3 混宣称 |
| 代理 crown 冒烟 | 仍可用，但不进日用宣称 |

**下一步**：
- ✅ 158 子集已挂 `SHADOW_S1`（见 `v2_0-shadow-predictions-s1.md（未公开）`）；未达 L1，不宣称跑赢 V3。
- 19 场 fixture 映射扩包：`v2_0-macau-fixture-map-19.md`（不阻塞）。

## 5. 现网保护

| 检查 | 结果 |
|---|---|
| `DUAL_WRITE_ODDS_ASIAN` | **false / 0**（health `dual_write_odds_asian:false`） |
| 现网 `odds_asian` 指纹 | **<指纹已移除>**（1413 行，未变） |
| V3 哈希 | **<指纹已移除>**（未变） |
| 现网 macau mid / 水位 | 仍 0 / 全 NULL |
| API | 0.3.13 |

## 6. 路径与脚本

| 项 | 路径 |
|---|---|
| 补数脚本 | `match-analysis-api/scripts/fill_macau_mid_water.py`（`fetch` / `import` / `report`） |
| 原料 | `odds-data/5dollar/macau-mid-water-fill-2026-10-07/raw/{fixture_id}_macauslot_asian.json` |
| 日志 | `.../logs/fetch_summary.json`、`import_summary.json` |
| 映射 | `odds-data/backfill/macau_mid_water_fixture_map.json` |
| 副本库 | `match-analysis-api/data/v2d3/app.db` |

中盘钟点复用 `app/collection_schedule.resolve_collection_ats`（≥23 或 0–10 → 竞彩日 15:00；其余 T−8h）。

## 7. 验收命令

```bash
cd api

# 缺口报告（现网 vs 副本）
.venv/bin/python scripts/fill_macau_mid_water.py report --db data/v2d3/app.db

# 现网指纹 / V3 / dual_write（只读）
.venv/bin/python scripts/sync_odds_asian_from_snapshot.py --db data/app.db fp
.venv/bin/python scripts/v3_hash.py
curl -s http://127.0.0.1:8787/health   # dual_write_odds_asian:false

# 拒绝写现网
.venv/bin/python scripts/fill_macau_mid_water.py import --db data/app.db
# → 应拒绝

# 副本幂等再导入
.venv/bin/python scripts/fill_macau_mid_water.py import --db data/v2d3/app.db

# 抽样
.venv/bin/python -c "
import sqlite3,json
c=sqlite3.connect('data/v2d3/app.db')
print('mid', c.execute(\"select count(*) from odds_asian where book='macau' and phase='mid' and home_water is not null\").fetchone()[0])
print(c.execute(\"select handicap,home_water,away_water,water_src from odds_asian where book='macau' and phase='mid' limit 3\").fetchall())
"
```

## 8. 回退

1. 丢副本：`rm -rf data/v2d3/`  
2. 现网未写，无需回滚。  
3. 原料可保留在 `odds-data/5dollar/macau-mid-water-fill-2026-10-07/` 供重导。


## 9. 19 场扩包（2026-10-07 续）

| 项 | 值 |
|---|---|
| 新映射 | **19/19**（队名+开赛；国际比赛 6 + 世界杯2026 13） |
| 新补 mid/open/close 水位 | 各 **…**（… 场 macauslot ticks 空） |
| v2d3 macau mid 有水 | **175 / 177** |
| approx mid | **105/175** |
| 仍空 | mid=38、43（映射在、澳门无盘） |
| 旧手工覆盖 | **0** |
| 现网 OA 指纹 | 仍 **<指纹已移除>**（1413） |
| DUAL_WRITE | **false** |

详见 `v2_0-macau-fixture-map-19.md`；S1 重跑见 `v2_0-shadow-predictions-s1.md（未公开）`。

## 10. 收口拍板（2026-10-07）

- 底座停在 **175/177**；match_id **38**（委内瑞拉–土耳其）、**43**（哥伦比亚–约旦）标 **`macau_ah_unavailable`**（有 5DF fixture、无澳门亚盘）。
- 不另寻源、不把皇冠水位代理进 S1 主指纹。
- SHADOW_S1 保持 v2d3 · version `….0…-shadow-s1-macau-actual-…` · hits=… 影子态至 ……；现网 DUAL_WRITE 仍关；B1–B4 不改。
- **澳门水位补数这条线收口。**
