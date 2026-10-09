# P0 状态：5DF 多庄历史队列脚手架（2026-10-07 UTC+8）

## 交付物

| 路径 | 说明 |
|---|---|
| `$ODDS_DATA_DIR/backfill/5df-multibook-history-queue/` | 队列根目录 |
| `README.md` | 中文：怎么跑、停止条件、P1 范围、双写关 |
| `build_queue.py` | 幂等建 pending / unmapped |
| `run_queue.py` | ≤30/分 worker；`--dry-run` / `--limit`；全量需 `CONFIRM_FULL_RUN=1` |
| `health_check.py` | pending/done/错误率/mtime/`DUAL_WRITE.off` |
| `DUAL_WRITE.off` | 双写关闭标记 |
| `queue/pending.jsonl` | **177** 待拉（有 fixture_id） |
| `queue/unmapped.jsonl` | **4993** 年窗竞彩场尚无 5DF 映射 |
| `queue/done.jsonl` | 0 |
| `queue/state.json` | 汇总 |
| `logs/p0_smoke_20261007_040412.txt` | build + dry-run×3 + health 冒烟stdout |

## 队列规模（诚实部分覆盖）

| 项 | 数 |
|---|---:|
| sporttery 年窗宇宙 `2025-10..2026-09` | **5034**（对齐 `call_volume_summary.A_jc_matches`） |
| 本机已映射并入 pending | **177**（几乎全来自 `macau_mid_water_fixture_map`；月份 **2026-06/07**） |
| unmapped | **4993**（待 `/chinasportslottery` 日窗补映射） |
| P1 目标剩余（路线图） | ~4857 × 3 次 ≈ 14571 调用 |

**结论**：P0 脚手架可用，可先对已映射 177 场跑 P1 小批；**全年 5034 场的 fixture 映射尚未落盘**——这是扩大 pending 的主阻塞，不是 worker 本身。

## 冒烟结果

- `build_queue.py` → exit 0；pending=177，unmapped=4993  
- `run_queue.py --dry-run --limit 3` → exit 0；`pulled_ok=3`，`api_calls=9`（仅计划，未打 API）；pending 未改写  
- `health_check.py` → exit 0；`ok=true`，`dual_write_marker_present=true`  
- 日志 **无** API key 泄漏

## 硬约束核对

- 只写 `$ODDS_DATA_DIR/backfill/5df-multibook-history-queue/`（raw/logs/queue/reports）  
- **无**现网 DB 写入  
- `DUAL_WRITE` 标记 OFF；脚本拒绝 env 打开双写  
- 未恢复旧 Bet365 小时续拉  

## 阻塞 / 下一步

1. **主阻塞**：年窗 CSL 日映射缺口（unmapped≈4993）。需按竞彩日拉 `/chinasportslottery`（24h 窗）写入 raw/csl 后再跑 `build_queue.py`。  
2. **可立即做**：对已有 177 先实拉验证：
   ```bash
   cd $ODDS_DATA_DIR/backfill/5df-multibook-history-queue
   python3 run_queue.py --limit 50
   python3 health_check.py
   ```
3. 全量（limit 省略）须：`CONFIRM_FULL_RUN=1 python3 run_queue.py`  
4. 巡检：活跃时每 2–4h 跑 `health_check.py`；`Remaining≤5` 自动停。

## 下一步命令（P1 起步）

```bash
cd $ODDS_DATA_DIR/backfill/5df-multibook-history-queue && python3 run_queue.py --limit 50
```

## P1 batch --limit 50

- **When**: 2026-10-07 04:04:54 → 04:06:49 UTC+8
- **Log**: `logs/p1_limit50_20261007_040454.txt`；health: `logs/p1_limit50_health_20261007_040652.txt`（约）
- **pulled_ok**: 23
- **errors**: 1 (`fixture_id=143916975`, `rate_reserve`)
- **processed**: 24（limit 目标 50，提前停）
- **pending_before → after**: 177 → 154
- **done after**: 23
- **api_calls**: 50
- **rate_remaining**: 5
- **stop**: `rate_reserve`（Remaining≤5，干净停）
- **dual_write**: OFF
- **wall**: ~115 s（≈1.9 min）
- **health_check**: `ok=true`；pending=154，done=23，unmapped=4993，error_rate=0.037，`dual_write_marker_present=true`

无 prod DB；无 API key 泄漏。

## P1 continue after rate_reserve

- **When**: 2026-10-07 04:07:20 → 04:43:00 UTC+8（先 sleep 90s 等窗口回充，期间不打 5DF；实拉 04:09:42 → 04:43:00）
- **起点 health**（sleep 后）: pending=**326**, done=**23**, unmapped=4915（映射扩队列后 pending 已大于先前 ~154）, `dual_write_marker_present=true`
- **终点 health**: pending=**0**, done=**349**, unmapped=4686, fill_ok=352, fill_errors=1, error_rate=0.0028, `ok=true`, dual_write OFF

### Batches

| batch | log | pending_before→after | pulled_ok | api_calls | stop | wall |
|---|---|---:|---:|---:|---|---:|
| 1 | `logs/p1_continue_20261007_040942.txt` | 326→166 | 160 | 407 | `limit_reached` | 945 s |
| 2 | `logs/p1_continue_20261007_042539.txt` | 166→91 | ~75（checkpoint 未写完） | ~163 | **DNS crash** (`URLError` Temporary failure in name resolution)；非 rate_reserve | 398 s |
| 3 | `logs/p1_continue_20261007_043230.txt` | 91→0 | 91 | 272 | `pending_exhausted` | 630 s |

（另有空日志 `logs/p1_continue_20261007_040936.txt`：首次因缺 `/usr/bin/time` 未真正开跑，已废弃。）

### 累计（本会话 continue）

| 项 | 值 |
|---|---:|
| done 累计 | **…**（本会话 …；先前 limit5… 已有 …） |
| pending（已映射可拉） | **0** |
| 本会话 API 调用 | **842**（`call_log.tsv` 自 04:09:42，不含表头） |
| rate_reserve 停次数 | **0**（未触达 Remaining≤5） |
| 连续 rate 停 | 0 / 3 上限 |
| 实拉墙钟 | **1998 s ≈ 33.3 min**（04:09:42–04:43:00） |
| 含 sleep90 墙钟 | **≈35.7 min**（自 04:07:20） |
| dual_write | OFF（`DUAL_WRITE.off` 在位） |
| prod DB | 无写入 |

**结论**：已映射 pending 已清零。扩大覆盖仍依赖 CSL 日映射（unmapped≈4686）。mapping 扩队列与拉数并行时，pending 曾从 154 涨到 326 再被拉完。


## Cycle 2026-10-07 next（CSL map → solo pull）

- **When**: map 04:44:16→04:44:44；pull 04:44:55→05:08:30 UTC+8
- **Map**: `--days 14 --max-new 200` → API days Sep05–16（12），cache Sep17/27；new_mappings=**204**；api_calls=**12**；rate_remaining=28
- **Neighbor**: exact=82 + neighbor_jc=122（offset −1）；local-only 复跑 Δ=0
- **build_queue**: pending **0→204**；unmapped **4686→4482**；done 349；mapped_in_universe 348→552
- **Pull**（solo，无并行 map）:

| batch | log | pending_before→after | pulled_ok | api_calls | stop | wall |
|---|---|---:|---:|---:|---|---:|
| 1 | `logs/p1_cycle_next_b1_20261007_044455.txt` | 204→4 | 200 | 600 | `limit_reached` | ~23.2 min |
| 2 | `logs/p1_cycle_next_b2_20261007_050804.txt` | 4→0 | 4 | 12 | `pending_exhausted` | ~26 s |

- **终点 health**: pending=**0**, done=**553**, unmapped=**4482**, fill_ok=556, fill_errors=1, error_rate=0.0018, `ok=true`, dual_write OFF
- **rate_reserve 停**: **0** / 3 上限
- **本轮 API 合计**: map 12 + pull 612 = **624**
- **raw/csl**: 26 日（Sep05–30）
- **csl_fixture_map.json**: n=**866**
- **key 泄漏**: 无；prod DB: 无写入

**结论**: unmapped …；本轮新增 done …（全部来自新 map）。下一步继续 map 更早日窗后再 solo pull。


## Cycle 2026-10-07 r3（routine 05:44 BJ：续拉收尾 → CSL map → 拉数交由并行 solo worker）

- **05:44 接手时**：已有 `run_queue.py --limit 220`（05:24 起）在跑，未并发；05:46:07 `pending_exhausted`（188 场 / 562 调用 / Remaining 36）→ done **862**，pending 0
- **Map**（05:46:17→05:46:45）：`--days 14 --max-new 200` → API 日 12（2026-01-04、2025-12-13/14/20、2026-01-31(空)、01-17、04-11、05-24、08-16、2025-10-30、11-09、2026-01-24）；new_mappings=**353**（exact 218 + neighbor −1 日 135，ambiguous 0）；api_calls=**12**；rate_remaining=24；stop=`max_new_200`
- **build_queue**：pending **0→355**；unmapped **4173→3820**
- **Pull**：本 routine 05:46:58 起 `--limit 200`，~20 s 后被另一会话启动的 solo worker（`logs/p1_pull_solo_limit220_20261007.txt`，05:47:22 起，`--limit 220`）终止（exit 143）；为避免双 worker 抢同一队列，本轮未重启，由该 worker 继续
- **05:47 health**：pending 352、done 868、unmapped 3820、fill_errors 1、error_rate 0.0011、dual_write OFF、prod DB 无写入、key 无泄漏
- **预计**：solo worker 拉完 220 场约 06:12 BJ，剩 ~132 pending；若无人接手，下一次 routine 为 10-08 01:43 BJ
