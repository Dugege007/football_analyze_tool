# rescue_queue（实时漏点补救队列）

口径权威卡：`docs/schema/v2_0-live-miss-rescue-rules.md`（§7 多日缺口）。

## 目录

| 路径 | 用途 |
|---|---|
| `pending/` | 待消费（`mark_missed` / `gap_scan` 写入；**热路径不调 API**） |
| `done/` | 已结案（asof 成功或 no_odds） |
| `failed/` | 超重试 / 永久失败 |
| `shadow/` | R-D 晚补索引报告（主值已写入规则格；关即时推荐） |
| `logs/` | 入队 / 消费 jsonl |
| `reports/` | `gap_scan_*.json`、`rescue_worker_*.json` |
| `schema_example.json` | 单条字段样例 |

## 接线

| 步骤 | 命令 |
|---|---|
| 单场漏点入队 | `live_capture` tick → `mark_missed` → pending |
| 多日缺口扫描 | `scripts/rescue_gap_scan.py --days 7` 或 `live_capture.py gap-scan` |
| 空闲窗消费 | `scripts/rescue_worker.py` 或 `live_capture.py rescue-consume` |
| 手动 asof | `live_capture.py asof-backfill --date D --target-key …` |

**ensure-daemon 恢复后**：先 `gap-scan`，再在非让路窗 / 非 due 邻近时 `rescue-consume`。勿塞进 tick。

## 默认参数

| 项 | 值 |
|---|---|
| 回溯天数 | **7** |
| 单轮消费 | **8** |
| 日上限（成功/late/no_odds） | **40** |
| 开赛后 >3h | **R-D 晚补**：仍写规则格；`recommend_live_ok=false` |
| approx | 中盘 **120**min / 临盘 **60**min；`far_open` 默认可标采纳 |
| 禁消费 | 让路窗 + 11:00–11:20；due 前 15min |

## 消费禁区

- `shared_api_yield` 让路窗：11:05–11:20、14:55–15:15、21:55–22:15
- 额外：11:00–11:20 不消费（避开 11:10 高峰）
- 禁止用 T 后即时盘冒充规则 mid/close
- 真实通道（R-F）暂 defer，留 pending
