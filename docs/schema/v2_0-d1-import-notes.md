# v2.0 D1 · 探针时间线导入笔记（2026-10-06）

> 配套设计：`v2_0-odds-timeline-storage-design.md` §1.2 / §3.3 / §5；日用通知节奏：（日用通知节奏文档，未公开）（本包不改消息发送）。  
> 影子台账 `research/shadow-ledger/`：**本包不落影子方案**；探针 `match_meta.extras.shadow_reserved` 预留 `hits` / `coverage` / `n_eligible` 字段供后续对接。

## 1. 路径

| 项 | 路径 |
|---|---|
| 副本库 | `api/data/v2d1/app.db`（从现网 `data/app.db` 拷贝后只加新表 + 探针场） |
| Migration | `docs/schema/v2_0_odds_timeline.sql`（`channel` ∈ `rule\|rule_legacy\|actual`） |
| 导入脚本 | `api/scripts/import_odds_timeline_probe.py`（幂等） |
| 探针原料 | `$ODDS_DATA_DIR/5dollar/probe-2026-10-06/raw` |
| 钟点工具 | `api/app/collection_schedule.py` |
| 现网 API | **0.3.9**（dispatch 日用改新 rule；不回写 `odds_asian`） |
| 现网生产库 | `$APP_DB_PATH` — **本任务未改写 `odds_asian`** |
| 备份 | `backups/v2d1-20261006234200/`（main.py.bak + live_oa_baseline.json） |

**策略**：副本库 = 现网拷贝 + v2.0 DDL + 2 场 `scope=extra` 探针（`match_uid=probe:<fixture_id>`，`extras.source=probe_d1`）。现网仍 177 场正式竞彩，无 probe 行、无 timeline 表。

## 2. 规定通道钟点（实现）

| channel | 条件 | mid target_at | close target_at |
|---|---|---|---|
| `rule`（日用） | 开赛小时 ≥23 或 ∈0–10 | 竞彩日 **15:00** | 竞彩日 **22:00** |
| `rule` | 开赛小时 11–22 | kickoff−8h（竞彩日时钟） | kickoff−1h |
| `rule_legacy` | **仅** 0–10 | 竞彩日 **16:00** | 竞彩日 **23:00** |
| `actual` | 始终 | t8 = kickoff−8h | t1 = kickoff−1h（另有 open/close=首末赛前 tick） |

dispatch `/dispatch/pending`：**筛选只用 `rule`**；响应增加 `collection_schedule_rule_legacy`、`rule_legacy_mid_collect_at`、`rule_legacy_close_collect_at`（不适用则为 null）。**禁止**用新钟点回写旧手工 mid/close。

## 3. 导入场次与行数（对照 probe README）

### FRA–BEL（周一002，开赛 2026-10-06 02:45+08；jingcai_date=2026-10-05，hour=2）

| book × market | 赛前 tick（README） | 入库 seg | 一致 |
|---|---|---|---|
| macau × asian | **8** | 8 | ✓ |
| crown × asian | **69** | 69 | ✓ |
| bet365 × asian | 22（README）/ **21**（实测无连续重复后） | 21 | ✓（按实测） |
| macau × ou（goalline） | **6** | 6 | ✓ |
| jc × euro_1x2 | **5** | 5 | ✓ |

Snapshot 通道样例（macau asian）：

| channel | point | target_at (UTC+8) |
|---|---|---|
| rule | mid | 2026-10-05 **15:00** |
| rule | close | 2026-10-05 **22:00** |
| rule_legacy | mid | 2026-10-05 **16:00** |
| rule_legacy | close | 2026-10-05 **23:00** |
| actual | t8 | 2026-10-05 **18:45**（kickoff−8h） |
| actual | t1 | 2026-10-06 **01:45**（kickoff−1h） |

### KOR–UZB（周二001，开赛 2026-10-06 19:00+08；hour=19）

| book × market | 赛前 tick | seg |
|---|---|---|
| macau × asian | **15** | 15 |

- `rule` mid/close = 11:00 / 18:00（与 actual t8/t1 重合）  
- `rule_legacy` = null（非 0–10）

其它：`odds_fetch_blob` 索引 8 个文件（path + sha256，正文仍在库外）；二次导入 seg/snap 插入 0 行（幂等）。

## 4. 现网未动证明

| 检查 | 结果 |
|---|---|
| 现网 `odds_asian` 行数 | **1413**（导入前后副本亦 1413；内容指纹 `<指纹已移除>` 与基线一致） |
| 现网 matches | **177**；无 `probe:%` |
| 现网无 v2 表 | timeline/snapshot/fetch_* 仅在副本 |
| V3 哈希 | **<指纹已移除>** |
| V3 validate | n3… / bet1 / **…** / 期末 …（reused） |
| smoke | `check_cffxdj5v3_smoke.py` ok |

## 5. 回退

1. **丢弃 D1 副本**：`rm -rf data/v2d1/`（或只删 `data/v2d1/app.db`）。  
2. **回退 dispatch 0.3.9→0.3.8**：`cp backups/v2d1-20261006234200/main.py.bak app/main.py`，并视需要还原 `app/collection_schedule.py` / `app/db.py` 的 v2.0 路径行；重启 uvicorn。  
3. **现网 odds_asian**：本任务未改写，无需回滚。

## 6. 验收命令（可复制）

```bash
cd api

# 副本导入（幂等）
.venv/bin/python scripts/import_odds_timeline_probe.py --db data/v2d1/app.db

# 任意时刻水位
.venv/bin/python scripts/import_odds_timeline_probe.py --db data/v2d1/app.db \
  --query --match-uid probe:1263863300 --book macau --market asian \
  --at '2026-10-05T18:00:00+08:00'

# 现网健康 / 哈希 / OA 行数
curl -s http://127.0.0.1:8787/health
.venv/bin/python scripts/v3_hash.py
.venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/app.db').execute('select count(*) from odds_asian').fetchone()[0])"

# dispatch 新口径说明
curl -s 'http://127.0.0.1:8787/dispatch/pending?window=mid&scope=jingcai' | .venv/bin/python -c "import sys,json;d=json.load(sys.stdin);print(d['approximation'])"

# V3 默认数字
curl -s -X POST http://127.0.0.1:8787/strategies/1/validate \
  -H 'content-type: application/json' -d '{"shadow":true}' \
  | .venv/bin/python -c "import sys,json;s=json.load(sys.stdin)['summary'];print(s['pnl_amount'],s['settlement_version'])"
```

## 7. 影子台账预留（不落库方案）

后续对接 `research/shadow-ledger/` 时，建议在 validate/compare summary 增加：

- `n_eligible`：通道下可结算样本数  
- `hits`：方向命中数  
- `coverage`：hits / n_eligible  

D1 探针 extras 已占位 `shadow_reserved`，值为 null。
