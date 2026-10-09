# 前向实时采集（live-capture-mid-close）

脚本：`scripts/live/live_capture.py`（单测 `scripts/test_live_capture.py`，5 个，全过）。
常驻进程：`$ODDS_DATA_DIR/python scripts/live/live_capture.py daemon`
（2026-10-08 18:01 UTC+8 起在 box 上运行；`ensure-daemon` 幂等拉起，flock 保证只有一个实例）。

## 目标时刻（每场竞彩）
| phase | phase_variant | 目标 | odds_snapshot (channel, point) |
|---|---|---|---|
| mid | rule | 例外场 = 竞彩日 15:00；其余 = 开赛−8h（精确到分钟） | (rule, mid) |
| close | rule | 例外场 = 竞彩日 22:00；其余 = 开赛−1h | (rule, close) |
| mid | real | 仅例外场：开赛−8h | (actual, t8) |
| close | real | 仅例外场：开赛−1h | (actual, t1) |
| live | rule_1110 | 竞彩日 11:10（即时（11:10）） | (rule, rule_1110) |

- 例外场 = 有竞彩编号、属于竞彩日 D、开赛 ≥ D 23:00（`exception_rule=jc_code_ge_2300`，无 11:30 上限）。
- 目标由后端 `app/collection_schedule.py`（`rule_targets` / `phase_exception` / `actual_targets` / `jingcai_date_from_code`）算，和 `/dispatch/pending` 用的是同一组函数。`/dispatch/pending` 读的是现网库，新竞彩日的场不在库里，所以场次清单改从 5DF `/v1/chinasportslottery?types=jingcailottery` 取（自带 fixture id），竞彩日只按编号归属。库里已有的场会交叉核对（`plan` 输出 `dispatch_cross_check`）。
- 开赛时间用 5DF 的（`kickoff_source=5df`）。5DF 给出整 12:00 的场标 `kickoff_placeholder_5df_1200_unverified`，因为手里没有竞彩官方时间可以对照。

## 调度
- 每 60 秒跑一轮 `tick`：今天的清单每 60 分钟刷新一次（2 次 CSL 调用），明天的每 120 分钟刷新一次，10:50 强制刷新一次。目标 T 落在 [T−2min, T+10min] 内就抓一次。同一场同一时刻到点的几个目标合并成一次 `/odds` 调用。抓完立刻入副本，再更新汇总。
- 轮询 60 秒只是检查本地计划，不调 API。所以 fetched_at 通常落在 T−2…T−1 分钟，不晚于决策时点。11:10 这种集中到点的时刻，按 2.5 秒间隔排队，最后几场会晚到 T+1~3 分钟。
- 过了 T+10min 还没抓到的目标记 `missed`，并 **R-C 入队** `rescue_queue/pending/`（不调 API）。补救规则全集见 `schema/v2_0-live-miss-rescue-rules.md`。执行路径：
  - `capture --allow-late`：落 staging 标 `late`，**不入副本**（旧口径，仅演练）。
  - **`asof-backfill`（2026-10-09 拍板，= R-A）**：用 5DF `/odds/history` 取 **as-of≤T** 最近一次赛前亚盘变化，写入 `odds_snapshot` 的 `channel=rule point=mid|close`，`source=5df_hist_asof`。**禁止**把 T 之后的即时盘当规则中盘/临盘。详见 `schema/v2_0-mid-rule-asof-backfill.md`。
    ```
    python scripts/live_capture.py asof-backfill --date 2026-10-09 --missed-mid-rule
    python scripts/asof_backfill_mid_rule.py --date 2026-10-09 \
        --target-key '2026-10-09|五001|mid|rule' --target-key '2026-10-09|五002|mid|rule'
    ```
  - **多日缺口扫描**（S17+）：`gap-scan --days 7`（默认回溯 7 天）对照 plan + state + 副本快照，应采未采幂等入队。
  - **自动 worker 消费队列**：`rescue-consume`（让路窗与 11:00–11:20 禁发；due 前 15min 避让；日上限 40）。队列说明见 `rescue_queue/README.md`；口径卡 `schema/v2_0-live-miss-rescue-rules.md` §7。
  - **ensure-daemon 恢复后**：先 `gap-scan` 再空闲窗 `rescue-consume`（**不要**塞进 tick 热路径）。
  - **补救默认（2026-10-09 再拍）**：approx 中盘 **120**min / 临盘 **60**min；开赛后 >3h 仍 as-of 写入规则格（`sim_ok/features_ok=true`），仅 `recommend_live_ok=false`；过远首开默认可标（`far_open`）采纳，方案可降权。见 `schema/v2_0-live-miss-rescue-rules.md`。
- 健康：`logs/heartbeat.json`（每轮覆盖）、`logs/health.jsonl`（事件）、`logs/call_log.tsv`（每次 API 调用及剩余额度）、`logs/missing_mapping.jsonl`、`logs/ingest_undo.jsonl`（每次写库前的旧值，供回滚）。
- 日汇总：`summary/<竞彩日>.md|json`。每次抓取后更新，次日 12:05 再出一次终版。

## 落盘
- 原始：`all/<竞彩日>/<fixture>_<抓取时刻>_<phase-variant>.json`（完整 `/odds` 响应加 `_meta`），平博亚盘切片另存 `pinnacle_ah/<竞彩日>/`（口径卡 §7 路径）。
- 归一化：`staging/<竞彩日>.jsonl`（只追加），`staging/<竞彩日>.csv`（汇总时由 JSONL 去重导出）。
- 清单：`csl/<竞彩日>.json`（含历史快照）、`plan/<竞彩日>.json`；状态：`state/captured_*.json`、`state/ingest_*.json`。

## 入库（只写 v2d3 副本）
- 只接受 `…/v2d3/app.db`，现网 `data/app.db` 直接拒绝；`DUAL_WRITE_ODDS_ASIAN` 被打开时也拒绝运行。
- 有新行要写时，先用 sqlite backup 备份到 `backups/`（保留最近 24 份，外加每天第一份），然后在一个事务里 upsert。键 = `(match_id, book, market, channel, point)`，也就是 场次+公司+市场+phase+variant。内容没变就不写（幂等）。同一个键上已有非 live 来源的行时保留原行，不覆盖，记 `conflict_non_live_row_kept`。
- 行字段：`source=5df_live`、`water_src=actual`、`recorded_at=fetched_at`、`target_at`、`line` 用 5DF 记法（负数=主让，和现有快照一致，接口层会取反）。`extras_json` 存 `capture=own, odds_source=live, phase, phase_variant, label, fetched_at, target_at, fetch_lag_min, tick_age_rule=live_fetched_at, phase_target=exact_minute, exception_rule, orientation, open_api{…}, raw_path, raw_sha256`。
- 入库前校验：
  - 水位要在 0.50–1.50 之间（亚盘、大小球的港赔），不在范围内拒收。
  - 赛中已有报价的行拒收。
  - fetched−target 要在 […, …] 分钟内。
  - 正负号：用后端 `validate_ah_sign.check_rows`。open 取 5DF opening，mid/close 取我们的规则快照，平手不判。单场命中送人工复核；「公司 × 来源 × 中盘」整批反号超过 20% 就整批拦下。中盘入库时临盘还没抓到，所以这次校验在临盘到点后才真正生效；已经入库、事后被拦下的中盘会从副本撤下，staging 保留。
  - 主客方向：和副本场次的队名或 team_aliases 比对，判断为相反就把线取反、主客对调。对不上的标 `unverified`。
- 副本里没有这场比赛 → 记 `no_replica_match`，下一轮再试。`--create-missing-matches`（或 `LIVE_CAPTURE_CREATE_MATCHES=1`）可以按 5DF 在副本里补一条 stub 场次，**默认关，需要用户批准后才能开**。


## 命令
```
cd $ODDS_DATA_DIR
python scripts/live_capture.py ensure-daemon      # 看门狗：没在跑就拉起
python scripts/live_capture.py status --date 2026-10-09
python scripts/live_capture.py plan --date 2026-10-09
python scripts/live_capture.py ingest --date 2026-10-09 [--dry-run]
python scripts/live_capture.py summary --date 2026-10-09
python scripts/live_capture.py gap-scan --days 7 --dry-run   # 多日缺口（只报告）
python scripts/live_capture.py gap-scan --days 7            # 入队 pending
python scripts/live_capture.py rescue-consume --dry-run     # 空闲窗消费（演练）
python scripts/live_capture.py rescue-consume --max 8       # 真正 asof（含 R-D 晚补）
kill -TERM $(cat 5dollar/live/logs/daemon.pid)                  # 停
```

## 12:00 占位 / 改期（2026-10-08 18:3x 上线，决议第 3 条 + 18:22 补充 + 18:23）
- 5DF 整 12:00 → `kickoff_placeholder_suspect`。这类场（`placeholder_ever` 粘住）另加 D 日 15:00 / 22:00 目标：快照 `channel=pending`、`point=d1500|d2200`，`extras.phase_pending=true`、`phase=null`，不进 `v_odds_asian_rule`。
- 开赛确认后（改成非 12:00 → 确认时刻取发现时刻；5DF 状态已开赛 → 取开赛时刻；或 `state/kickoff_confirm.json` 人工确认，须带 `confirmed_at`），下次入库按「到目标时刻为止已公布的开赛时间」写 `phase_assigned`（如 `["close|rule"]`）；确认晚于目标 → `phase_assign_late=true`（live 台账不计命中，hist 只作参考）。
- 由占位开赛推出的目标（例外场规则列、真实列、非例外场中盘/临盘）`features_ok=false`；开赛确认且时间没变才转 true；变了 → `superseded_by_kickoff_change=true`。11:10 不受影响。
- 每次刷新比开赛时间：变了 → `kickoff_rev+1`，`plan.kickoff_log` 与 `logs/kickoff_changes.jsonl` 记 `kickoff_original / kickoff_from / kickoff_actual / detected_at`；`postponed_announced_at` 一律留空，`postpone_ts_unknown=true`；发现时刻只记作公布上界 `kickoff_known_by`。还没到的旧目标作废（`targets_superseded`），新目标 key 带 `|k<rev>`。
- **开赛提前空档补抓**（决议 18:36）：新目标尚未到 → 丢掉旧目标只抓新目标；新目标已过窗口 → 立刻补抓一次（`target_at=发现时刻`、`planned_target_at=理论新目标`、`catchup=true`、`phase_assign_late=true`，live 不计命中；事件 `catchup_on_discovery`）；旧目标已抓过的保留在 state；已开赛则无法补抓（`new_target_already_past`）。发现时刻只用来调度，不当 `postponed_announced_at`。
- **0.3.20 入库字段**（只写 v2d3）：`odds_snapshot.extras_json` 必写 `phase_pending` / `features_ok` / `phase_assign_late`（bool）；`matches.kickoff_rev`（无列则 ALTER 加上）+ `match_meta.extras_json.kickoff_rev`。现网不改。后端读快照 extras / matches.kickoff_rev，不读 state。
- 推迟作废：`postpone_void_hours=24`、`postpone_void_src=OE67/2018-art11`，`void_postponed = 开赛比原定晚 > 24h`（原定本身是占位的不算推迟）。
- 前一竞彩日还有未确认占位场时（开赛后 3 小时内），继续每小时刷新 CSL 等确认；刷新中开赛时间/确认状态有变且已有 staging 时，会重跑一次入库以更新 extras。
