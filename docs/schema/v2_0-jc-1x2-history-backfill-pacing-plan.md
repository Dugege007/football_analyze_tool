# 竞彩胜平负历史补数节奏方案（草案，待分析师确认后再执行）

> 状态：分析师已于 2026-10-10 确认窗口与速率；运行器已写好并测试，但没有运行，也没有调用任何接口。下周一由例程启动。

## 1. 背景

- 用户 2026-10-10 决定暂不接竞彩官方接口。竞彩胜平负初盘只取 5DollarFootballAPI（下文简称 5DF）历史开赛前的第一笔。
- 5DF 的 `chinasportslottery` 只支持 `1x2`、`asian`、`goalline`、`corner` 四种玩法，没有让球胜平负；`asian` 对老场次和今天的场次都返回空的 ticks；`1x2` 有变化记录。
- 本地目前只有 85 场竞彩胜平负历史文件（完整一场补数队列下载的）。

## 2. 队列

- 生成脚本：`scripts/backfill/build_jc1x2_history_queue.py`（只读，不调用接口，不写数据库）。
- 队列文件：`/workspace/odds-data/backfill/5df-jc1x2-history-queue/queue/pending.jsonl`，汇总在同目录 `summary.json`。
- 队列内容：已经映射到 5DF 比赛编号、但本地所有原始数据目录里都还没有 `<比赛编号>_chinasportslottery_1x2.json` 的场次。映射来源是完整一场补数队列（`done.jsonl`、`pending.jsonl`）和研究副本库、正式库的 `match_meta`（都只读）。
- 每场只调一次：`GET /fixtures/{fixture_id}/odds/history?bookmaker=chinasportslottery&market=1x2`。
- 2026-10-10 18:57 生成时共 4527 场。按竞彩日月份分布：2025-10 有 78 场，2025-11 有 647 场，2025-12 有 543 场，2026-01 有 539 场，2026-02 有 254 场，2026-03 有 400 场，2026-04 有 436 场，2026-05 有 417 场，2026-06 有 154 场，2026-07 有 188 场，2026-08 有 393 场，2026-09 有 428 场，2026-10 有 50 场。

## 3. 额度上限（已从代码确认，2026-10-10 仍然有效）

`api/app/shared_api_yield.py`：

- `SHARED_LIMIT_PER_MIN = 40`（账户每分钟上限），`ANALYST_LIVE_MAX_PER_MIN = 24`（实时采集每分钟最多 24 次）。
- `BACKFILL_MAX_PER_MIN = 40 − 24 = 16`：共享账本里所有补数合计每分钟不超过 16 次（单进程最小间隔 3.75 秒）。
- `REMAINING_FLOOR = 24`：接口返回的剩余额度小于或等于 24 时暂停。
- 让路时段 `YIELD_WINDOWS`：每天 11:05 至 11:20、14:55 至 15:15、21:55 至 22:15（北京时间），补数在这些时段内休眠，给实时采集让路。

已确认的运行窗口（分析师 2026-10-10）：周一到周五 08:14 至 23:44（北京时间）。窗口外运行器自动休眠到下一个窗口开始（加 `--exit-outside-window` 时直接退出）。

## 4. 运行规则

1. 只在补数窗口里运行，与现有历史补数相同；实时采集忙（让路时段、实时采集正在排队或剩余额度达到下限）时让路。
2. 所有请求都经过共享账本（`shared_api_yield.SharedApiYield`），调用方名称建议为 `jc1x2_history_queue`，与完整一场补数共用同一个每分钟 16 次的上限和剩余 24 次的暂停线。两者合计不能超过上限，本队列不另开额度。
3. 可以与完整一场补数穿插运行。已确认分配：完整一场补数在跑时，竞彩队列每分钟最多 6 次；完整一场补数停了或补完时，每分钟最多 8 次。判断方法：`/proc` 下存在命令行含 `run_fullmatch_queue.py` 的进程，并且它的 `queue/pending.jsonl` 还有内容，才算在跑；否则算停了或补完。两者共用 `shared_api_yield` 的账本，合计每分钟不超过 16 次。
4. 每场成功后写入原始文件 `raw/hist/<比赛编号>_chinasportslottery_1x2.json` 并在队列中标记完成；失败（例如 HTTP 错误）记录原因，最多重试 2 次，之后转入人工复核清单。返回空 ticks 的场次标记为 `empty`，不重试。

## 5. 时间估算

- 全部 4527 场，每场 1 次调用，共 4527 次调用。
- 按每分钟 6 次：约 755 分钟，即约 12.6 小时的有效运行时间。
- 按每分钟 8 次：约 566 分钟，即约 9.4 小时。
- 扣除让路时段和额度暂停后，如果每个工作日能有效运行约 6 小时，预计需要 2 至 3 个工作日。实际时长取决于分析师确认的补数窗口。

## 6. 导入方式

1. 导入前先用 SQLite 在线备份研究副本库（`/workspace/match-analysis-api/data/v2d3/app.db`）到 `/workspace/backups/` 下带时间戳的目录，并写 `ROLLBACK.md`。
2. 用 `api/scripts/import_jc_1x2_history_segments.py` 按已定口径导入 `odds_timeline_seg`：`book = jc`、`market = euro_1x2`、`source = 5df_hist_jc_1x2`，变盘点游程，只用开赛前的记录，主客颠倒时交换胜负赔率，映射不上的场次跳过并列出。脚本可以重复执行，不写 `odds_asian`。
3. 开赛前第一笔作为竞彩胜平负初盘（`status = ok`、`source_kind = official_open`、`quote_kind = official_first`、`open_time` 为该笔记录时间）。
4. 正式库 `data/app.db` 不动。研究副本库里还没有的场次（例如 2025 年的老场次）导入时会被列为映射不上；是否先在研究副本库里建这些场次，需要分析师另行决定。
5. 样本超过 100 场后，再由分析师决定是否给竞彩加截断分位表分组。

## 7. 运行器用法

运行器：`scripts/backfill/run_jc1x2_history_queue.py`（测试：`scripts/backfill/test_run_jc1x2_history_queue.py`）。

- 启动（后台运行，写 pid 文件和日志）：

      cd /workspace/football-analyze-tool && .venv/bin/python scripts/backfill/run_jc1x2_history_queue.py start

- 停止（写停止文件，并向 pid 文件里的进程发送终止信号；运行器处理完当前请求、导入已下载的批次后退出）：

      cd /workspace/football-analyze-tool && .venv/bin/python scripts/backfill/run_jc1x2_history_queue.py stop

- 查看状态：

      cd /workspace/football-analyze-tool && .venv/bin/python scripts/backfill/run_jc1x2_history_queue.py status

- 文件位置（目录 `/workspace/odds-data/backfill/5df-jc1x2-history-queue/`）：队列 `queue/pending.jsonl`，状态 `queue/state.json`（每场的结果、HTTP 状态码、尝试次数、原始文件路径和 SHA-256 校验值，以及停止原因），原始响应 `raw/hist/<比赛编号>_chinasportslottery_1x2.json`，日志 `logs/runner.log`，pid 文件 `runner.pid`，停止文件 `STOP`。
- 运行前需要环境变量 `FIVEDOLLAR_FOOTBALL_API_KEY`（不写进任何文件或日志）。
- 断点续跑：重新启动时跳过状态为完成（done）、空（empty）和失败（failed）的场次。单场失败最多尝试 3 次，每次都记日志；3 次都失败标为失败，不再自动重试。
- 自动停止：连续 3 次 401 或 403、连续 5 次 5xx 或网络错误、剩余额度响应头无法解析或小于 0；原因写进状态文件的 `stop_reason`。
- 导入：每下载 20 个有变化记录的文件为一批，先在线备份研究副本库到 `/workspace/backups/v2d3-before-jc1x2-queue-import-<时间戳>/`（附 `ROLLBACK.md`），再用 `import_jc_1x2_history_segments.py` 导入。

## 8. 不建比赛行的规则

1. 运行器只对研究副本里已经有的场次导入；不在研究副本里的场次不建比赛行，只保存原始文件。
2. 早于 2026-06 的比赛如果要进研究副本，必须按 `/workspace/odds-data/schema/v2_0-hist-raw-promotion-rules.md` 的五步对照规则走，不能由运行器自动建行。
3. 正式库 `data/app.db` 始终不动。
