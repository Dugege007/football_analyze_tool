# 每日场次范围 + JSON 落盘约定（v2 草案 · 2026-10-05）

对齐仓库 作者的私有分析仓 的 `00_数据集/YYYY/YYMM.json` 与 `README_数据说明.md`。  
原则：**竞彩场为主宇宙；非竞彩可记但必须可过滤；旧五段结构兼容；新机构/新市场只做扩展、不改旧字段语义。**

---

## 1. 每天看哪些场

### 1.1 主清单（必须采、必须分析）

- 来源：**中国体彩竞彩足球当日开售场次**（sporttery 赛程/计算器）。
- 竞彩日口径与仓库一致：`match.date` = 竞彩日；**0–10 点开赛记入前一竞彩日**。
- 标识：`match.jc.id` = `{weekday}{no}`，如 `日004`、`三080`；`match.jc.no` 为三位编号字符串。
- **排序**（与分析脚本一致）：
  1. `match.date` 升序  
  2. `jc_day_hour_order(kickoff_hour)`（0–10 → 24–34）  
  3. `match.jc.no` 数值升序（001、002…）
- 每日工作清单 = 该竞彩日主清单，按上序输出；报告/预测默认**只跑 `scope=jingcai`**。

### 1.2 附清单（API 有余力才记）

- 非竞彩开售、但 API 易得的同日/近窗比赛（五大联赛等）。
- 必须打标：`match.scope = "extra"`，且 **`match.jc` 为 `null`**（禁止伪造竞彩编号）。
- 可写入同月 JSON，或单独 `YYMM_extra.json`；分析默认排除，除非显式打开。
- 额度紧时：只保证主清单的初盘 11:10、中盘开赛前 8h、临盘开赛前 1h。

### 1.3 不做

- 不把非竞彩场塞进 `jc.no` 序列。  
- 不编造 `popularity_diff` / 竞彩 vote；缺则 `null`，支持率用 `stats.support_proxy_odds`（可选）。

---

## 2. JSON：兼容层 + 扩展层

### 2.1 顶层仍固定 5 段（兼容旧脚本）

```
match | result | stats | odds | meta
```

月文件仍是数组；路径建议继续 `00_数据集/YYYY/YYMM.json`。  
新版本加：`meta.schema_version`（建议 `"2.0"`）、`meta.pipeline`。

### 2.2 `match` 扩展（不删旧字段）

| 字段 | 说明 |
|---|---|
| 旧：date, weekday, jc{id,weekday,no}, competition, kickoff_hour, teams | 保持 |
| **`scope`** | `"jingcai"` \| `"extra"`（主清单必填 jingcai） |
| **`kickoff_at`** | 可选 ISO 本地/UTC 完整开赛时间（补 kickoff_hour） |
| **`ids`** | 可选：`sporttery_match_id`, `api_football_fixture_id`, `five_dollar_id`, … |

### 2.3 `result` / `stats`

- `result` 不变。  
- `stats` 旧字段保留；新增可选：
  - `stats.support_proxy_odds`：欧赔共识代理（非真实支持率）
  - `stats.sources`：各统计字段来源标记（可选）

### 2.4 `odds`：旧键保留 + 机构字典扩展

**兼容（必须仍可写）：**

- `odds.asian.macau.open/close`：仍可为**纯盘口数字**（旧口径）
- `odds.asian.crown|william.open/mid/close`：`{home_water, handicap, away_water}`
- `odds.euro_home_win.macau|william.open/close`
- `odds.jc_home_win.open/close`

**推荐扩展（新采写入，旧脚本可读则忽略）：**

```json
"odds": {
  "asian": {
    "macau": { "open": 0.5, "close": 0.5 },
    "crown": { "open": {"home_water":4.5,"handicap":0.5,"away_water":4.5}, "mid": {...}, "close": {...} },
    "william": { "...": "同上" },
    "bet365": { "open": {"home_water":...,"handicap":...,"away_water":...}, "mid": {...}, "close": {...} },
    "pinnacle": { "...": "可选" }
  },
  "euro_home_win": { "macau": {"open":...,"close":...}, "william": {"open":...,"close":...} },
  "euro_1x2": {
    "william": { "open": {"home":...,"draw":...,"away":...}, "close": {...} },
    "pinnacle": { "...": "可选" }
  },
  "jc_home_win": { "open": ..., "close": ... },
  "jc_hhad": {
    "open": { "goal_line": "-1", "home": ..., "draw": ..., "away": ... },
    "close": { "...": "可选" }
  },
  "totals": {
    "macau": { "open": {"line":2.5,"over":...,"under":...}, "close": {...} }
  },
  "_snap": {
    "open_at": "ISO",
    "mid_at": "ISO",
    "close_at": "ISO"
  }
}
```

约定：

- 新机构只往 `odds.asian.<book>` / `odds.euro_1x2.<book>` **加键**，键名小写稳定码：`macau|crown|william|bet365|pinnacle|sbobet|...`
- 时段统一 `open`（约 11:10）/ `mid`（开赛前 8h）/ `close`（开赛前 1h）；澳门旧数据可无 mid。
- 盘口符号：**主让为正、主受让为负**，0.25 步进（与仓库一致）。
- 大小球、完整 1X2、竞彩让球：属**新版本能力**，旧 AH 模型不读这些键即可。

### 2.5 `meta` 扩展

```json
"meta": {
  "source_file": "2026/2610.xlsx",
  "month": "2610",
  "schema_version": "2.0",
  "pipeline": "api_v1",
  "scope_counts": { "jingcai": 28, "extra": 5 }
}
```

单场也可有 `meta.collected_from`: `["sporttery","infersports","5dollar"]`。

---

## 3. 文件拆分建议

| 文件 | 内容 |
|---|---|
| `YYMM.json` | **仅** `scope=jingcai`（默认分析输入，与现仓库一致） |
| `YYMM_extra.json` | 可选非竞彩附清单 |
| Excel | 仅人工核对日志，不再当分析主输入 |

---

## 4. 每日作业清单（实现侧）

1. 拉 sporttery 当日（大陆出口）→ 生成主清单 + `jc.no`  
2. 按排序写出「今日竞彩场次表」  
3. 对主清单打点：11:10 / 开赛前 8h / 开赛前 1h  
4. 额度余量 → 可选附清单，写入 `_extra`  
5. 赛后回填 `result`；冲突按核验对照表记日志  

---

## 5. 样例（竞彩主场，扩展后示意）

```json
{
  "match": {
    "date": "2026-10-06",
    "weekday": "一",
    "scope": "jingcai",
    "jc": { "id": "一003", "weekday": "一", "no": "003" },
    "competition": { "name": "英超", "type": "联赛", "stage": 8 },
    "kickoff_hour": 3,
    "kickoff_at": "2026-10-07T03:00:00+08:00",
    "teams": { "home": "阿森纳", "away": "某客队" },
    "ids": { "sporttery_match_id": "1234567" }
  },
  "result": null,
  "stats": { "popularity_diff": null, "support_proxy_odds": null },
  "odds": {
    "asian": {
      "macau": { "open": 0.5, "close": null },
      "crown": { "open": { "home_water": 0.92, "handicap": 0.5, "away_water": 0.94 }, "mid": null, "close": null },
      "bet365": { "open": { "home_water": 1.90, "handicap": 0.5, "away_water": 1.95 }, "mid": null, "close": null }
    },
    "jc_home_win": { "open": 1.72, "close": null },
    "jc_hhad": { "open": { "goal_line": "-1", "home": 3.1, "draw": 3.4, "away": 2.0 }, "close": null },
    "_snap": { "open_at": "2026-10-06T11:10:00+08:00", "mid_at": null, "close_at": null }
  },
  "meta": { "month": "2610", "schema_version": "2.0", "pipeline": "api_v1" }
}
```
