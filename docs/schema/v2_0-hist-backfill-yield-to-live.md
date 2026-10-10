# 历史赔率补数：今日实时采集需要额度时暂停（yield-to-live）

**规则表述**：**补历史赔率时，若今日实时采集需要额度，则历史队列先暂停。**

**拍板**：2026-10-09（用户：日常实时采集优先；平时每分钟只补 1～3 场；今日该抓的数据抓稳后再填历史；不抢 5DF 给实时采集预留的冗余）。  
**范围**：`scripts/backfill/5df-multibook-history-queue/`；只写本目录 raw/logs/reports/queue；`DUAL_WRITE` **关**。  
**相关**：[`v2_0-live-miss-rescue-rules.md`](./v2_0-live-miss-rescue-rules.md)、`shared_api_yield.py`、`scripts/rescue_worker.py`。

---

## 1. 目标

| 项 | 约定 |
|---|---|
| 日常 | 今日实时采集（初盘 11:10、规则中盘/临盘、rescue 消费、daemon 忙）进行时 → **今日实时采集忙时，历史队列先暂停** |
| 空闲 | 不在补历史数据须让路的固定时间窗内、无邻近 due、额度富余、rescue 未在消费 → 补历史数据可以跑 |
| 节奏 | **默认每分钟最多补 1 场**（完整数据档；旧「只拉两家亚盘」时曾允许 1～3 场，现与完整档对齐为 1；`--max-per-min` 可配） |
| 额度 | 不抢今日实时采集的冗余；与 `shared_api_yield` 一致：补数合计 ≤16/min，**Remaining≤24 停**（最忙日至少留 ≥20%；工程默认留约 60%/24 次） |
| 默认 | 小时续跑 routine **仍暂停**；脚本就绪后可用低速试跑，**勿**猛开大批量 |

---

## 2. 补历史数据须让路的时间窗（与 rescue 对齐）

### 2.1 固定时间窗（北京时间，左闭右开）

补历史数据在下列时段 **不发 5DF 拉取**（`--yield-check` 默认开；另有 `shared_api_yield` 在请求层再挡一次）：

| 时间窗 | 覆盖 |
|---|---|
| **11:00–11:20** | 含 11:10 初盘高峰；覆盖 shared 的 11:05–11:20 + 前缘（与 rescue `CONSUME_BLOCK` 一致） |
| **14:55–15:15** | 规则中盘集中点（shared 同窗） |
| **21:55–22:15** | 规则临盘集中点（shared 同窗） |

> 注：`shared_api_yield.YIELD_WINDOWS` 仍是 11:**05**–11:20；补历史数据一侧故意更早从 11:00 开始让路，避免与 11:10 初盘抓取抢配额。

### 2.2 due 前 15 分钟（扫描今日实时采集 plan）

与 `rescue_worker.should_skip_for_due` 同口径：

- 扫 `5dollar/live/plan/{昨,今,明}.json`
- 跳过已在 `state/captured_{D}.json` 的 `target_key`
- 跳过已开赛场
- 若最近未完成目标满足 **−10min ≤ (T−now) ≤ 15min** → 暂停补历史数据（`due_avoid_sec=…`）
- 过点超过 10min 仍未 captured 的，交给 rescue / miss 路径，不无限挡补历史数据

### 2.3 动态忙信号

| 信号 | 条件 | `paused_reason` 例 |
|---|---|---|
| 今日实时采集 daemon 忙 | `logs/heartbeat.json` 新鲜（默认 ≤3min）且 `due>0` 或 `groups>0` | `live_daemon_busy_due=…` |
| remaining 过低 | heartbeat（或响应头经 shared 闸）`remaining ≤ 24` | `remaining_low=…` |
| 近 N 分钟写库 | 今日 `state/ingest_{D}.json` 或 `captured_{D}.json` mtime ≤ N min（默认 3） | `live_recent_write_…` |
| rescue 消费 | `pgrep rescue_worker.py` 有进程，或 `rescue_queue/logs/consume.jsonl` 近 N min 有消费事件 | `rescue_worker_running` / `rescue_recent_event=…` |

可用 `--busy-min`、`--remaining-floor`、`--no-recent-write-check` 微调。

### 2.4 恢复条件

同时满足才恢复补历史数据：

1. 不在「补历史数据须让路的固定时间窗」内  
2. 无 due 前 15 分钟内的紧迫目标  
3. heartbeat 非「due/groups 忙」且 remaining > floor  
4. 近 N 分钟无今日实时采集成功写库（若未关 recent-write 检查）  
5. rescue 未在跑且近 N 分钟无消费事件  

`--yield-wait`：睡一会再探测，直到空闲或本轮超时。  
默认（无 `--yield-wait`）：写 `paused_reason` 后 **干净退出**（exit 0，`stop=paused_yield`），由人工/定时再拉。

---

## 3. 限速

| 层 | 上限 | 实现 |
|---|---|---|
| **场次** | 默认 **1** 场/分钟（完整数据约 20～25 次调用/场） | `run_queue.py` / `run_fullmatch_queue.py --max-per-min`；场与场之间间隔 `60/max_per_min` 秒 |
| **API（本队列+其它补数合计）** | ≤16/min | `shared_api_yield.Gate` 跨进程账本 |
| **Remaining** | ≤24 停到 Reset | shared gate + hist_yield 读 heartbeat |
| **旧本地 gap** | 约 3.8s 兜底 | 不再以 ≤30/min 为主节奏 |

旧多庄亚盘档每场约 3 次调用；**一场完整数据**约 20～25 次调用，故默认 **1 场/分钟**（约 20～25 API/min 名义，实际受 shared ≤16/min 与缓存命中约束）。完整一场补数的方案文档暂未放入公开仓。

---

## 4. 状态字段（`queue/state.json`）

| 字段 | 含义 |
|---|---|
| `paused` | bool，是否因「补历史赔率时，若今日实时采集需要额度，则历史队列先暂停」而暂停 |
| `paused_reason` | 字符串原因；空闲为 `null` |
| `hist_mode` | `running` / `paused_yield` / `paused_quota` / `idle_empty` / `stopped` / `probe` |
| `max_per_min` | 本轮场次上限 |
| `dual_write` | 恒为 `OFF` |
| `last_progress_at` | 最近状态写入时间（BJ） |

---

## 5. 怎么跑（低速试跑）

```bash
cd scripts/backfill/5df-multibook-history-queue

# 只探测：现在能不能跑补历史数据？
python3 hist_yield.py
python3 run_queue.py --yield-check-only

# 冒烟（不打 API）
python3 run_queue.py --dry-run --limit 2 --max-per-min 1

# 空闲时低速实拉 1 场（有 key；pending 现多为 0）
python3 run_queue.py --limit 1 --max-per-min 1 --yield-check

# 长挂：遇到须让路则睡醒再继续（仍建议带 --limit）
python3 run_queue.py --limit 30 --max-per-min 1 --yield-check --yield-wait
```

**不要**在未确认空闲时 `CONFIRM_FULL_RUN=1` 无 limit 猛跑；pending 回补前先做别名/映射（见 §7）。

---

## 6. 半小时探测（建议，未强改现网旧 routine）

当前「历史补数·5DF多庄队列续跑」**仍暂停**（见 `v2_0-backfill-supervise-live.md`）。  
**不要**恢复 `supervise_cycles.sh` 的大批量 `drain --limit 220`。

完整数据队列建议 **每 30 分钟**探测一次是否空闲（脚本已备）：

```bash
# 建议 cron（北京时间）：每小时 14 分与 44 分
# 14,44 * * * *  cd <完整一场补数队列目录> && ./supervise_fullmatch_halfhour.sh
cd <完整一场补数队列目录>
python3 run_fullmatch_queue.py --yield-check-only || exit 0
python3 run_fullmatch_queue.py --limit 28 --max-per-min 1 --yield-check
```

原则：空闲才补；固定时间窗与 due 邻近直接 skip；**禁止**再无监督地大批量连排。完整一场补数队列（`5df-fullmatch-history-queue`）的代码与方案文档暂未放入公开仓。

---

## 7. 下一步（非本轮必做）

pending 目前多为 **0**；剩余 unmapped / no_hit 需：

1. 别名扫描 / alias90 残留 169+5 冲突处理  
2. **低速入队**（入队本身不打 odds/history，可与今日实时采集并行；但 CSL 日窗 map 会打 5DF，须同样在今日抓取时让路）  
3. 入队后再用 `--max-per-min 1 --yield-check` 抽完；完整数据走 `5df-fullmatch-history-queue`  

本轮重点只落地：**让路探测 + 场次限速 + 文档**（核心口号：补历史赔率时，若今日实时采集需要额度，则历史队列先暂停）。

---

## 8. 铁律

1. `DUAL_WRITE` 关；不写现网 / 不写 V3 手工库  
2. 只写研究副本路径与本队列目录  
3. 日志永不打印 API key  
4. 补历史数据不得在固定时间窗 / due 前 15 分钟 / remaining 触底时与今日实时采集抢配额  
5. 交付先落盘（本文件 + 脚本），默认不自动开大批量

---

## 9. 文件

| 路径 | 角色 |
|---|---|
| `backfill/5df-multibook-history-queue/hist_yield.py` | 只读探测：今日抓取忙时是否应暂停补历史数据 |
| `backfill/5df-multibook-history-queue/run_queue.py` | `--max-per-min` / `--yield-check` / `--yield-wait` |
| `match-analysis-api/app/shared_api_yield.py` | 请求层固定时间窗 + ≤16/min + Remaining≤24 |
| `scripts/rescue_worker.py` | rescue 消费侧的让路（补历史数据探测与之对齐） |
