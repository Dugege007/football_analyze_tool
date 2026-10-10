# 更新记录

本文件记录 football-analyze-tool 公开仓库每个版本的变化。版本号遵循语义化版本（Semantic Versioning）规则。


## v0.1.4（2026-10-10）

### 「当日赛程」列表排序
- 比赛列表接口 GET /matches 的排序规则从「先按开赛小时、再按竞彩编号」改为「同一竞彩日内按竞彩编号数字升序；没有竞彩编号的比赛排在最后，再按开赛时间排列」。这样凌晨开赛、编号却较大的场次（例如六208 到六217）不会再排到白天场次（例如六201）的前面。
- 「当日赛程」页和「预测结论」页都通过同一个比赛列表接口取数，因此两页都会按新的编号顺序显示。网页界面说明文字已同步更新。
- 后端接口版本号从 0.3.24 升为 0.3.25。新增测试 `api/tests/test_match_list_sort_0325.py`。

## v0.1.3（2026-10-10）

### 「当日赛程」页
- 页面打开时默认显示北京时间此刻所属的竞彩日，不再固定显示 2026-06-06。日期选择器旁边增加了「上一日」「下一日」和「今日」按钮。
- 「对阵」列拆成「主队」和「客队」两列，两列之间用一个只显示「vs」的窄列隔开。
- 开赛时间显示完整的北京时间年月日和时分，例如「2026-06-07 01:45」。分钟未知的比赛只显示到小时，并注明「分钟未知」。
- 「赛果」列后面增加「总进球数」列，数值是主队进球数加客队进球数。没有赛果的比赛显示「-」。
- 「总进球数」列后面增加「使用方案」列，显示这场比赛当时做预测时使用并冻结下来的正式方案代码。影子方案和测试方案不显示，没有预测的比赛显示「-」。
- 暂时去掉「方向」列。以后改成五个预测方向列的格式建议稿写在 docs/schema/schedule-page-prediction-direction-columns-draft.md，等待用户确认。
- 某一天正式库里没有比赛时，页面显示「这一天正式库里还没有比赛数据。」

### 后端接口（API 0.3.24）
- 比赛列表接口 GET /matches 的每一场比赛增加 strategies 字段，列出这场比赛冻结预测所用的正式方案代码。这个字段只读取数据库，不改变数据库结构。

### 竞彩日归属规则
- 一场比赛属于哪一个竞彩日，以它的竞彩编号为准：编号里的星期就是它所属的竞彩日。例如「周五012」属于周五的竞彩日，即使它在周六 12:00 以后才开赛。
- 只有没有竞彩编号的比赛，才按北京时间 00:00 到 11:30（包含 11:30）开赛属于前一个竞彩日的规则兜底，并且要标记为「待复核」。详细说明见 docs/schema/v2_0-jingcai-day-early-kickoff.md。

## v0.1.2（2026-10-10）

本版本把作者本机开发目录中尚未进入公开仓库的代码改进合并进来，之后本机运行的接口、网页界面与采集脚本都以本仓库为唯一代码来源。

### 新增

- 新增赛前盘口与水位折线接口 `GET /matches/{match_id}/odds/timeline`（新文件 `api/app/odds_timeline_chart.py`）。v0.1.0 的网页界面已经包含这张折线图，但当时的公开仓库缺少对应接口，因此图表无法取数；本版本补上。接口只在用户打开某一场比赛时才向 5DollarFootballAPI 拉取变盘历史，不写入任何主表；结果在内存中短时缓存；实时采集繁忙时返回 503 状态码并说明原因。默认博彩公司为澳门，默认盘口类型为亚洲让球盘。
- 新增该接口的契约文档 `docs/schema/v2_0-prematch-odds-timeline-chart.md` 与测试 `api/tests/test_odds_timeline_0323.py`。
- 新增 `scripts/backfill/5df-multibook-history-queue/hist_yield.py`：只读判断今天的实时采集或漏点补救是否需要调用额度；需要时，历史补数暂停，让实时采集优先。实时采集运行目录取 `$ODDS_DATA_DIR/5dollar/live`。
- 新增让路规则文档 `docs/schema/v2_0-hist-backfill-yield-to-live.md`。

### 修改

- 后端接口版本号从 0.3.22 升为 0.3.23，原因是新增了上述接口。
- `scripts/live/live_capture.py`：常驻进程的心跳与睡眠改用单调时钟，并拆成每段最多 5 秒的短睡眠，每段结束都刷新心跳文件；新增后台心跳线程，只刷新 `watchdog_ts` 字段，不冒充主循环的业务进度；网络请求与域名解析设置 60 秒硬超时；收到 SIGTERM 或 SIGINT 信号时可以在短睡眠之间及时退出；临时文件名带进程编号、线程编号与随机后缀，避免多个线程同时写同一个心跳文件时出错。这些修改用于解决墙钟回拨导致长时间不采集、以及卡住时守护程序难以及时发现的问题。`scripts/live/test_live_capture.py` 新增对应测试。
- `scripts/backfill/5df-multibook-history-queue/run_queue.py`：默认每分钟最多补 1 场比赛（可用 `--max-per-min` 调整）；新增 `--yield-check`（默认开启）、`--yield-wait`、`--yield-check-only`、`--busy-min`、`--remaining-floor`、`--due-avoid-min` 与 `--no-recent-write-check` 参数；暂停原因写入队列状态文件。`health_check.py` 输出中新增暂停状态与限速字段，同目录的 README 同步更新。
- 网页界面：公开仓库 v0.1.0 的接口已经把竞彩官方数据缺失原因从 `msi_empty` 改名为 `collector_empty`，但网页界面仍只认旧名称，导致「暂无竞彩官方数据」的提示不显示。现在网页界面新增 `isCollectorEmpty` 判断函数，两个名称都按同一种情况处理，提示文字改为「本地采集器未接通或尚未采集」。
- `api/app/table_matches.py`：竞彩官方数据的来源名称 `sporttery_local` 与旧名称 `sporttery_msi` 视为等价，旧数据仍然能被优先选用。
- 测试：`test_hl_v031_0320.py` 中两条按合成示例配置断言的用例，改为固定读取示例配置文件，本机存在真实配置时不再误报失败；`test_not_evaluable.py` 中依赖真实影子方案台账的用例，在台账只有表头时改为跳过。

### 未包含

- 调好的策略参数、真实数据、验证成绩以及由它们生成的配置文件（例如 `config/strategy_params.json`、`api/config/kickoff_drift.json`、`api/config/leak_suspect.json`）仍然只放在作者本机，并被 `.gitignore` 排除。
- 完整一场历史补数队列的代码与方案文档暂未放入公开仓库。

## v0.1.1（2026-10-10）

- 新增 `start.bat`：在 Windows 上双击即可启动后台接口和网页界面，并自动打开浏览器。
- 新增 `stop.bat`：在 Windows 上双击即可静默关闭后台接口和网页界面两个窗口及其服务进程。
- README 新增「Windows 一键启动与关闭」一节。

## v0.1.0（2026-10-10）

### 新增

- 新增网页界面目录 `web/`（Vite、React 与 TypeScript 工程），包含比赛列表、单场详情、预测结论、方案对比与多方案叠加曲线、方案工坊、验证、资金计算、设置与数据表等页面。
- 单场详情页新增赛前盘口与水位折线图：默认显示澳门亚洲让球盘，可以选择其他博彩公司，横轴为对数刻度的距开赛时间。
- 网页界面的接口地址改为可配置：新增 `web/.env.example`，可通过 `VITE_API_BASE`（默认 `http://127.0.0.1:8787`）、`VITE_API_REPLICA_BASE`（默认 `http://127.0.0.1:8788`）与 `VITE_DEV_PORT`（默认 5173）修改。
- 新增跨平台文件锁模块 `api/app/portable_lock.py`：Linux 与 macOS 使用 fcntl，Windows 使用标准库 msvcrt。
- 新增测试 `api/tests/test_portable_lock.py`，验证非阻塞加锁冲突与解锁后的再次加锁。
- 新增环境变量 `APP_CORS_ORIGINS`，用于配置后端接口允许的跨源资源共享（Cross-Origin Resource Sharing，简称 CORS）来源，说明已写入 `config.example.env`。
- 新增本文件 `CHANGELOG.md`。

### 修改

- `scripts/live/live_capture.py`、`api/scripts/build_jc_match_rows.py` 与 `api/app/shared_api_yield.py` 改用跨平台文件锁，Windows 上可以运行 `python scripts/live/live_capture.py check-config`。
- 后端接口的跨源资源共享默认从「允许任意来源并携带凭据」收紧为只允许 `http://127.0.0.1:5173` 与 `http://localhost:5173`；需要其他来源时通过 `APP_CORS_ORIGINS` 配置。
- 根目录 `README.md` 新增「在本机运行网页界面」一节（包含 Windows PowerShell 与 Cursor 终端的操作步骤），更新目录说明、环境要求与仓库地址（仓库已改名为 football-analyze-tool）。
- `.gitignore` 新增网页界面的依赖目录、构建输出与本机配置文件 `web/.env.local`。

### 未包含

- 网页界面开发过程中的截图、真实数据、调好的参数与验证成绩均未放入公开仓库。
- 后端接口版本号保持 0.3.22 不变，因为本版本没有改动任何接口的请求或响应格式。
