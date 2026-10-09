# 5DF 多庄历史回补队列（P0 脚手架 → P1 主路径）

> 生成：2026-10-07（UTC+8）。**只写** `$ODDS_DATA_DIR/` 下研究／副本路径；**不写**现网库；`DUAL_WRITE` **关**（见本目录 `DUAL_WRITE.off`）。

## 目标（P1）

- 范围：竞彩近 12 个月（窗 `2025-10..2026-09` BJ；优先有 CSL 编号的 `2025-10-25→`）
- 庄家：`macauslot` + `pinnacle`
- 市场：亚盘（asian）开／中／收可推导素材
- 每场约 **3** 次调用：
  1. `GET /v1/fixtures/{id}/odds?bookmakers=macauslot,pinnacle`
  2. `GET /v1/fixtures/{id}/odds/history?bookmaker=macauslot&market=asian`
  3. `GET /v1/fixtures/{id}/odds/history?bookmaker=pinnacle&market=asian`
- 自限 **≤30/分**；`X-RateLimit-Remaining≤5` **停**
- mid 推导必须用 `recorded_at ≤` 目标时刻的 tick（铁律）；本脚手架只落 raw，不发明水位数字

## 硬约束

| 项 | 约定 |
|---|---|
| 双写 | **关**。本目录有 `DUAL_WRITE.off`；脚本不连现网 `app.db` |
| Key | `process.env.FIVEDOLLAR_FOOTBALL_API_KEY`；日志**永不**打印 key |
| 旧任务 | **勿**原样恢复 Bet365 小时续拉；本队列是多庄 `/odds`+`/history` |
| 全量跑 | `--limit` 省略时必须 `CONFIRM_FULL_RUN=1`，否则拒绝启动 |

## 目录

```
5df-multibook-history-queue/
  DUAL_WRITE.off          # 双写关闭标记
  build_queue.py          # 建／刷新 pending + unmapped
  run_queue.py            # 限速 worker
  health_check.py         # 巡检
  queue/
    pending.jsonl         # 待拉（有 fixture_id）
    done.jsonl            # 已完成
    unmapped.jsonl        # 年窗竞彩场尚无 5DF fixture 映射
    state.json            # 断点／汇总
  raw/odds/  raw/hist/    # 原始 API 响应
  logs/                   # call_log、smoke、fill_report
  reports/                # 结构化 fill 报告
```

## CSL 日窗映射（扩 pending）

```bash
# 有界首轮：最近 --days 日 或 --max-new 新映射，先到先停
python3 map_csl_fixtures.py --days 14 --max-new 200
# 产出：raw/csl/*.json、csl_fixture_map.json，并自动 rebuild 队列
```

限速：≤30/分；`Remaining≤5` 停。**2026-10-07 04:07 BJ：PAUSED-FOR-RATE，勿再打 5DF 直至解除。**

## 怎么跑

```bash
cd $ODDS_DATA_DIR/backfill/5df-multibook-history-queue

# 1) 建队列（幂等；复用 macau_mid_water_fixture_map + 已有 CSL raw）
python3 build_queue.py

# 2) 巡检
python3 health_check.py

# 3) 冒烟（不打 API）
python3 run_queue.py --dry-run --limit 3

# 4) 小批实拉（P1 起步）
python3 run_queue.py --limit 50

# 5) 全量（约 8h 量级；需显式确认）
CONFIRM_FULL_RUN=1 python3 run_queue.py
```

## 停止条件（run_queue）

- pending 空
- `X-RateLimit-Remaining ≤ 5`
- HTTP 429 / 401 / 403
- 连续错误过多（默认 ≥5）
- `--limit` 达到
- 缺 key（非 dry-run）

## 队列现状说明

P0 落盘时本机**已有**可映射 fixture 主要来自 `macau_mid_water_fixture_map.json`（~177）及既有 CSL 日缓存。  
竞彩年目标约 **5034**；其余场记入 `unmapped.jsonl`，待后续 `/chinasportslottery` 日窗映射补齐后再 `build_queue.py` 刷新进 pending。  
这是**诚实部分覆盖**，不是假装已有全年 fixture id。

## 相关文档

- `docs/schema/v2_0-history-backfill-roadmap.md`
- `docs/schema/v2_0-history-backfill-progress-eta.md（未公开）`
- `docs/schema/v2_0-backfill-p0-status.md`（本轮状态）
