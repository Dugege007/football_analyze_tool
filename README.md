# football-analyze-tool

> 竞彩足球亚盘分析工具链：本地只读 API + 多公司赔率定时采集 + 漏点补救 + 口径文档与影子台账。
> A toolkit for Jingcai (China Sports Lottery) football Asian-handicap analysis: local read-only API, scheduled multi-bookmaker odds capture, gap rescue, and data-convention docs.

## 项目简介

本仓库是作者足球分析工作流中**可公开的工具部分**：

- **`api/`**：基于 FastAPI 的本地 API，读取 SQLite 库，提供赛程、盘口、预测、数据表（`/table/matches`）、策略回测等只读接口；
- **`web/`**：网页界面（Vite 与 React 构建），包含比赛列表、单场详情、预测结论、方案对比与多方案叠加曲线、赛前盘口与水位折线图（可选择博彩公司、横轴为对数刻度的距开赛时间）、数据表等页面，通过本地 API 取数；
- **`scripts/live/`**：按竞彩日规则时点抓取多家公司（澳门、皇冠、威廉、平博、365、马会、竞彩等）赔率快照，只写研究副本库；附 as-of 补中盘、多日缺口扫描、补救队列 worker；
- **`docs/schema/`**：盘口阶段术语、漏点补救规则、as-of 补盘、赛果入库节奏、场次消歧、防泄漏铁律等口径文档与 DDL；
- **`research/`**：影子方案台账与验收清单（不含任何实盘资金细节）。

与作者的私有分析仓的关系：后者是方法论研究与旧方案（月度数据集、回测脚本）的仓库，不公开；本仓只放工具代码与口径文档，二者数据格式（月度 JSON 五段 `match/result/stats/odds/meta`）保持一致。

**公开什么、不公开什么**：框架代码、库表结构、采集与补救规则、方法论文档全部公开；**调好的参数、验证有效的方案组合、由真实数据派生的配置、预测结果不公开**。代码里原本写死的调参数值已移到 `config/strategy_params.json`（公开仓只给全 `null` 的模板），详见 [`docs/STRATEGY_PARAMS.md`](docs/STRATEGY_PARAMS.md)。

**方法论一句话**：以「竞彩日」为时间轴，在固定规则时点（初盘 / 中盘 / 临盘 / 11:10 即时）记录多家公司的亚盘与欧赔快照，所有特征严格按「as-of 决策时点」取数、绝不使用决策时点之后的信息，用于亚盘方向预测的回测与影子验证。

> 本项目仅用于数据分析与研究，不构成任何投注建议。

## 目录说明

```
.
├── api/                    FastAPI 本地 API（app/、scripts/ 运维与导入脚本、tests/、config/）
├── web/                    网页界面（Vite + React + TypeScript；src/ 源码、.env.example 接口地址样例）
├── scripts/
│   ├── live/               实时采集 live_capture、as-of 补中盘、gap-scan、rescue_queue/worker
│   ├── backfill/           历史补数（5DF 多公司队列、澳门中盘补水、API-Football/InferSports/football-data 日更）
│   ├── model/              Dixon-Coles 泊松模型（N5）及其单测
│   └── tools/              小工具（文本规范化 dry-run 等）
├── integrations/5dollar/   5DF 接入说明、实时采集说明、补救队列格式、响应样例（schema_example，合成数据）
├── docs/
│   ├── schema/             口径卡、DDL、OpenAPI 草案、补救/采集/消歧/赛果入库等文档
│   ├── backfill-schema/    月度 JSON v2 格式说明与演示样例（json-v2-sample.json，虚构数据）
│   └── SENSITIVE.md        各数据源需要的 key/账号、放哪个变量、哪些东西故意不进仓库
├── research/shadow-ledger/ 影子台账框架、方案登记模板（shadow-schemes.csv 仅表头）、防泄漏验收清单
├── config/                 strategy_params.example.json（策略参数模板，全为 null）
├── config.example.env      全部环境变量的样例与中文说明（复制为 .env 使用）
├── CHANGELOG.md            版本更新记录
├── CONTRIBUTING.md
└── LICENSE                 PolyForm Noncommercial 1.0.0
```

运行时会生成、但**不进仓库**的目录：`api/data/`（SQLite 库）、`data/odds-data/`（采集原始 JSON、staging、日志、补救队列）、`backups/`，均已在 `.gitignore` 中排除。

## 首次运行（三步：复制配置 → 填自己的 → 健康检查 + 一次 dry-run）

### 0. 环境要求

- **Python 3.12 及以上**（`numpy 2.5` / `scipy 1.18` 要求 ≥3.12）
- Git；`curl`（可选，用于健康检查）
- 操作系统：Windows、macOS 与 Linux 均可运行。从 v0.1.0 起，采集脚本使用跨平台文件锁（`api/app/portable_lock.py`：Linux 与 macOS 使用 fcntl，Windows 使用标准库 msvcrt），在 Windows 上也能运行 `python scripts/live/live_capture.py check-config`。
- 运行网页界面另需 **Node.js 20.19 或更高版本**（附带 npm）。

### 1. 克隆并安装依赖

```bash
git clone https://github.com/Dugege007/football-analyze-tool.git
cd football-analyze-tool
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r api/requirements.txt
pip install pytest                 # 可选：跑测试用
```

### 2. 复制配置并填写自己的值

```bash
cp config.example.env .env         # Windows: copy config.example.env .env
```

用编辑器打开 `.env`，按每行注释填写（详见 `docs/SENSITIVE.md`）：

- **数据库路径**：`APP_DB_PATH` 可以直接指向你自己电脑上每日备份出来的 `.db` 文件（建议写绝对路径，如 `D:/football/backups/app_2026-10-09.db`），同时设 `APP_READONLY=1` 以只读方式打开，避免误写备份；
- **研究副本**：采集只写 `V2D3_DB_PATH` 指向的副本库，请复制一份**单独**的文件，不要与 `APP_DB_PATH` 相同；
- **API Key**：跑实时采集需要 `FIVEDOLLAR_FOOTBALL_API_KEY`；只做健康检查可以先不填；
- **`DUAL_WRITE_ODDS_ASIAN` 默认 0（关）**，请保持关闭。打开后采集入库脚本会直接拒绝运行。
- **策略参数（可选）**：只做 API 健康检查和采集不需要。要跑影子预测或 Dixon-Coles 模型时，`cp config/strategy_params.example.json config/strategy_params.json` 后填入你自己验证得到的数值（见 `docs/STRATEGY_PARAMS.md`）。

所有 Python 入口启动时会自动读取仓库根目录的 `.env`（已存在的系统环境变量优先）。`.env` 已被 `.gitignore` 排除，**永远不要提交**。

> 还没有自己的库？可以生成一个演示库（两场虚构比赛）：
> `cd api && python scripts/seed.py`（默认写 `api/data/app.db`；若该文件已存在需加 `--reset`，**切勿对自己的备份库执行**）。
> 生成演示库时请把 `.env` 里的 `APP_READONLY` 临时设为 0。

### 3. 健康检查 + 采集 dry-run

**API 健康检查**（不需要任何 key，库文件不存在也能通过）：

```bash
cd api
uvicorn app.main:app --host 127.0.0.1 --port 8787
# 另开一个终端：
curl -s http://127.0.0.1:8787/health
# → {"ok":true,"version":"0.3.22","dual_write_odds_asian":false,"meta":{"db":"live","promoted":true,"readonly":true}}
```

**采集 dry-run**（不调任何 API、不写任何文件，只检查 key 是否配置、库路径与双写开关）：

```bash
python scripts/live/live_capture.py check-config
```

- 退出码 `0` = 可以开始采集；`2` = 尚未就绪，输出的 `hints` 会说明缺什么（例如「缺少 FIVEDOLLAR_FOOTBALL_API_KEY」）；
- key 只显示 `set` / `missing`，**不会打印值**；
- 未配置 key 时运行真正的采集命令（如 `plan`）会给出中文提示后退出，不会崩溃。

配置好 key 与副本库之后：

```bash
python scripts/live/live_capture.py plan            # 刷新今天竞彩日的场次与目标时点（约 1–2 次 API 调用）
python scripts/live/live_capture.py status          # 列出目标（不调 API）
python scripts/live/live_capture.py ingest --dry-run  # 入副本前预演（只读打开副本库）
python scripts/live/live_capture.py ensure-daemon   # 常驻采集（每 60 秒检查一次，仅到点才调 API）
```

补救与缺口扫描见 `integrations/5dollar/live-capture.md` 与 `docs/schema/v2_0-live-miss-rescue-rules.md`。

### 4. 跑测试（可选）

```bash
cd api && python -m pytest -q
```

以下测试会被自动标记为 skipped，并写明原因：

- 依赖真实库的回归测试（原因：「需要私有真实库/回归库」）：把你自己的库放到 `api/data/app.db`、`api/data/v2d3/app.db` 后会正常执行；
- 依赖调好参数的测试（原因：「strategy params not configured」）：填好 `config/strategy_params.json` 后会执行。

### 5. 由你自己的数据生成的配置（可选）

公开仓不附带由作者真实数据生成的两份配置，只附带模板：

| 文件 | 公开仓附带 | 怎么得到你自己的 |
|---|---|---|
| `api/config/kickoff_drift.json`（开赛时间漂移清单，仅提示用） | `kickoff_drift.example.json`（空清单） | 先对自己的库做改期扫描得到 CSV，再运行 `python api/scripts/build_kickoff_drift.py --csv <你的 postpone_scan_all.csv>` |
| `api/config/leak_suspect.json`（疑似泄漏的旧策略清单） | `leak_suspect.example.json`（合成示例） | 手工维护：按示例格式列出你认为回测数字不可信的策略编码 |

真实文件不存在时，API 自动读取对应的 `*.example.json`，接口照常工作。两份真实文件均已在 `.gitignore` 中。

## 在本机运行网页界面

网页界面需要同时运行两个程序：后端接口（在 `api/` 目录下用 uvicorn 启动，端口 8787）和网页界面开发服务器（在 `web/` 目录下用 npm 启动，端口 5173）。网页界面开发服务器会把浏览器发往 `/api` 的请求转发给后端接口，因此一般不需要额外配置跨源访问。

### 第一步：准备数据库配置

在仓库根目录的 `.env` 文件中设置 `APP_DB_PATH`，让后端读取你自己的数据库：

- 可以指向你每日备份出来的数据库文件，例如 `APP_DB_PATH=D:/football/current/app_latest.db`（Windows 路径建议使用正斜杠）。
- 指向备份文件时，请同时设置 `APP_READONLY=1`。这样数据库会以只读方式打开，所有写入请求都会被拒绝，不会改动你的备份。
- 如果暂时没有自己的数据库，可以先运行 `python api/scripts/seed.py` 生成一个演示数据库（默认位置为 `api/data/app.db`）。

### 第二步：启动后端接口（终端一）

Windows PowerShell，或者在 Cursor 中按 `` Ctrl+` `` 打开的终端（Cursor 在 Windows 上默认使用 PowerShell）：

```powershell
cd D:\你的路径\football-analyze-tool
.venv\Scripts\Activate.ps1
cd api
uvicorn app.main:app --host 127.0.0.1 --port 8787
```

如果激活虚拟环境时提示禁止运行脚本，请先运行一次 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`，然后重新激活。

macOS 或 Linux 终端：

```bash
source .venv/bin/activate
cd api
uvicorn app.main:app --host 127.0.0.1 --port 8787
```

启动后，在浏览器打开 <http://127.0.0.1:8787/health>，看到 `"ok":true` 即表示后端正常。接口调试页面在 <http://127.0.0.1:8787/docs>。

### 第三步：启动网页界面（终端二）

在 Cursor 中可以点击终端面板右上角的加号新建第二个终端，然后运行：

```powershell
cd D:\你的路径\football-analyze-tool\web
npm install
npm run dev
```

`npm install` 只需要在第一次运行或者依赖更新后执行。如果希望严格按照 `package-lock.json` 安装，可以用 `npm ci` 代替。

### 第四步：在浏览器中打开

打开 <http://127.0.0.1:5173>，即可看到比赛列表等页面。在比赛详情页中可以查看赛前盘口与水位折线图。

### 修改接口地址或端口（可选）

把 `web/.env.example` 复制为 `web/.env.local`，然后修改：

- `VITE_API_BASE`：后端接口地址，默认 `http://127.0.0.1:8787`。如果后端换了端口，请同步修改这里。
- `VITE_API_REPLICA_BASE`：可选的只读副本实例地址，默认 `http://127.0.0.1:8788`。没有副本实例时不需要修改。
- `VITE_DEV_PORT`：网页界面开发服务器端口，默认 5173。

如果你让浏览器直接访问后端接口（不经过网页界面开发服务器的转发），需要在仓库根目录的 `.env` 中用 `APP_CORS_ORIGINS` 列出允许访问的网页地址。跨源资源共享（Cross-Origin Resource Sharing，简称 CORS）默认只允许 `http://127.0.0.1:5173` 和 `http://localhost:5173`。

### 停止

在两个终端中分别按 `Ctrl+C`。

## 关键口径文档

| 主题 | 文档 |
|---|---|
| 盘口阶段术语（初/中/临、规则/真实） | `docs/schema/v2_0-odds-phase-terminology.md` |
| 漏点补救规则 | `docs/schema/v2_0-live-miss-rescue-rules.md` |
| as-of 补中盘 | `docs/schema/v2_0-mid-rule-asof-backfill.md` |
| 赛果入库节奏 | `docs/schema/v2_0-result-ingest-schedule.md` |
| 场次消歧 | `docs/schema/v2_0-fixture-match-disambiguation-draft.md` |
| 防泄漏铁律 | `docs/schema/v2_0-as-of-betting-iron-rules.md`、`research/shadow-ledger/acceptance-leakage-checklist.md` |
| 本地 API 接口速查 | `api/README.md` |

## 许可证

本项目采用 **[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/)**（全文见 [`LICENSE`](LICENSE)）。

- **个人使用、学习研究、非营利组织使用**：可以自由使用、修改和分发（分发时需保留 `LICENSE` 及其中的 `Required Notice` 行）。
- **商业使用**（包括但不限于付费服务、商业产品、为营利目的提供分析结果）：需要另行取得作者的**商业授权**。请通过本仓库的 [GitHub Issue](https://github.com/Dugege007/football-analyze-tool/issues) 或作者 GitHub 主页（[@Dugege007](https://github.com/Dugege007)）联系。

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。提交前务必确认没有带入任何 key、`.env` 或数据库文件。

### Windows 一键启动与关闭

- 双击仓库根目录的 `start.bat`：在两个最小化窗口里分别启动后台接口（端口 8787）和网页界面（端口 5173），等网页可以访问后自动打开浏览器。第一次运行时如果没有 `web\node_modules`，会先自动执行 `npm install`。使用前需要已经按上文创建好 `.venv` 虚拟环境。
- 双击 `stop.bat`：不弹出任何提示，直接关闭这两个窗口和其中的服务进程，并释放 8787 和 5173 端口。
