# 敏感信息与数据源配置说明（SENSITIVE）

本仓库**不包含**任何 API Key、Token、Cookie、密码、账号、数据库或原始历史缓存。
克隆后请按本文用**你自己的**账号/密钥配置，所有敏感值只放在本机：

- 仓库根目录的 `.env`（由 `config.example.env` 复制而来，已被 `.gitignore` 排除）；或
- 操作系统环境变量 / 钥匙串（macOS Keychain、Windows 凭据管理器等）。

> 铁律：会话 Cookie、密码、邮箱令牌、API Key **只放本机 `.env` / OS 钥匙串，永不 commit**。
> 提交前建议执行一次：`git grep -nE "(api[_-]?key|token|secret|password|Bearer )" -- . ':!docs' ':!config.example.env'`，
> 或使用 [gitleaks](https://github.com/gitleaks/gitleaks)：`gitleaks detect --source . --no-git`。

验证命令里的 `$VAR` 均来自你本机环境；先执行 `set -a; . ./.env; set +a`（Linux/macOS/WSL）导入。
所有验证命令**只读**，每条只发 1 次请求。

---

## 1. 5DollarFootballAPI（5DF）—— 主赔率源（必需，用于采集）

| 项 | 说明 |
|---|---|
| 用途 | 竞彩场次清单（`/v1/chinasportslottery`）、多公司赔率（`/v1/fixtures/{id}/odds`：澳门/皇冠/威廉/平博/365/马会/竞彩等）、赔率历史（`/odds/history`，as-of 补中盘与漏点补救） |
| 申请 | <https://5dollarfootballapi.com> 注册。可用的公司与接口取决于你的账户，以官方说明为准 |
| 变量 | `FIVEDOLLAR_FOOTBALL_API_KEY` |
| 用到的脚本 | `scripts/live/live_capture.py`、`scripts/live/asof_backfill_mid_rule.py`、`scripts/live/rescue_worker.py`、`scripts/backfill/*`（5df 队列、澳门中盘补水等）、`api/scripts/fill_*` |
| 权限建议 | 只需读权限；不要与他人共用同一把 key（采集脚本按账户窗口限速） |
| 验证 | `curl -s -H "Authorization: Bearer $FIVEDOLLAR_FOOTBALL_API_KEY" https://api.5dollarfootballapi.com/v1/status` → 返回 `plan` 与限额 |
| 不调 API 的自检 | `python scripts/live/live_capture.py check-config`（只报 key 是否 set，不打印值） |
| 常见失败 | `401` key 错/未设置；`403` 当前账户无权访问该公司或接口；`429` 超限（脚本会按 `Retry-After` 退避，并在 `X-RateLimit-Remaining ≤ 8` 时主动暂停） |

## 2. InferSports —— 赛程/赛果/统计补充源（可选）

| 项 | 说明 |
|---|---|
| 申请 | <https://infersports.dev> |
| 变量 | `INFERSPORTS_API_KEY` |
| 用到的脚本 | `scripts/backfill/infersports_daily.py` |
| 验证 | `curl -s -H "Authorization: Bearer $INFERSPORTS_API_KEY" https://api.infersports.dev/v1/usage` |
| 常见失败 | `401` key 无效；`429` 请求过于频繁 |

## 3. API-Football（api-sports.io）—— 赛程/赛果补充源（可选）

| 项 | 说明 |
|---|---|
| 申请 | <https://dashboard.api-football.com> |
| 变量 | `API_FOOTBALL_KEY`（请求头 `x-apisports-key`） |
| 用到的脚本 | `scripts/backfill/apifootball_daily.py` |
| 验证 | `curl -s -H "x-apisports-key: $API_FOOTBALL_KEY" https://v3.football.api-sports.io/status` |
| 常见失败 | 返回 200 但 `errors` 字段非空（key 错或请求次数用尽）；部分赛季可能不可查 |

## 4. football-data.co.uk —— 欧洲联赛历史赔率 CSV（免费，无需 key）

| 项 | 说明 |
|---|---|
| 申请 | 无需注册，直接下载 CSV，如 `https://www.football-data.co.uk/mmz4281/2526/E0.csv` |
| 变量 | 无 |
| 用到的脚本 | `scripts/backfill/football_data_2627_merge.py` |
| 注意 | 请遵守站点使用条款，低频下载；下载的 CSV 属于原始缓存，不进仓库 |

## 5. football-data.org（可选，v0 代码未直接调用）

| 项 | 说明 |
|---|---|
| 申请 | <https://www.football-data.org/client/register> |
| 变量 | `FOOTBALL_DATA_ORG_API_KEY`（请求头 `X-Auth-Token`） |
| 验证 | `curl -s -H "X-Auth-Token: $FOOTBALL_DATA_ORG_API_KEY" https://api.football-data.org/v4/competitions/PL` |
| 常见失败 | `403` 当前账户无权访问该赛事；`429` 请求过于频繁 |

## 6. BSD（Bzzoiro Sports Data）—— xG / 伤停（可选，v0 代码未直接调用）

| 项 | 说明 |
|---|---|
| 申请 | <https://sports.bzzoiro.com> 注册 |
| 变量 | `BSD_BZZOIRO_API_KEY`（请求头 `Authorization: Token <key>`） |
| 相关文档 | `docs/schema/v2_0-fundamentals-ingest-schema-draft.md` |
| 验证 | `curl -s -H "Authorization: Token $BSD_BZZOIRO_API_KEY" "https://sports.bzzoiro.com/api/v2/"` |

## 7. OddsPapi / The Odds API / TheStatsAPI（可选，调研用，v0 代码未直接调用）

| 数据源 | 申请 | 变量 | 鉴权 |
|---|---|---|---|
| OddsPapi | <https://oddspapi.io> | `ODDSPAPI_API_KEY` | query `apiKey=`（基址 `https://api.oddspapi.io/v4`；v5 域名会 401） |
| The Odds API | <https://the-odds-api.com> | `THE_ODDS_API_KEY` | query `apiKey=`，验证：`curl -s "https://api.the-odds-api.com/v4/sports?apiKey=$THE_ODDS_API_KEY"` |
| TheStatsAPI | <https://www.thestatsapi.com> | `THE_STATS_API_KEY` | `Authorization: Bearer <key>`；`403 KEY_REVOKED` 通常表示账户没有生效套餐 |

> 注意：The Odds API / OddsPapi 的 key 走 URL 参数，**不要把带 key 的 URL 写进日志、截图或 issue**。

## 8. 竞彩官网（sporttery.cn）—— 竞彩官方赔率/赛果

| 项 | 说明 |
|---|---|
| 账号/key | 公开接口无需 key；但**必须使用能访问竞彩官网的网络环境**，否则可能返回 HTTP 567 |
| v0 状态 | 仓库**不含**竞彩官网采集代码，只有口径文档（`docs/schema/v2_0-jc-odds-capture-schema-draft.md`）；采集说明计划在 v0.1 提供 |
| 浏览器登录 | 若你在自己电脑用浏览器自动化访问，请使用本机独立浏览器 profile；profile 目录、Cookie、会话**永不进仓库**（`.gitignore` 已排除常见 profile 目录名） |
| 失败处理 | 网络无法访问竞彩官网时，竞彩字段保持为空，**不要用其他数据源冒充竞彩官方数据** |

## 9. 数据库（SQLite）

| 项 | 说明 |
|---|---|
| 变量 | `APP_DB_PATH`（API 主库）、`V2D3_DB_PATH`（研究副本，采集只写这里） |
| 来源 | 你自己的库；可直接指向你电脑上每日备份出来的 `.db` 文件 |
| 建议 | 指向备份库时设置 `APP_READONLY=1`；副本库请复制一份单独文件，不要与 `APP_DB_PATH` 相同（采集脚本会拒绝写现网库） |
| 为什么不进仓库 | 库内含个人的预测、注单与历史赔率整理结果，属于私人数据；体积也大 |

## 10. GitHub（仅贡献者需要）

克隆公开仓库不需要任何凭证。提交/推送请使用你自己的 `gh auth login` 或 SSH key；**不要**把 Personal Access Token 写进 `.env` 以外的任何文件或 remote URL。

---

## 故意不进仓库的内容及原因

| 内容 | 原因 |
|---|---|
| `.env`、任何 key/token/cookie/密码 | 凭证，泄漏即被盗用额度或账号 |
| `*.db` / `*.sqlite*`、`backups/` | 私人预测、注单、整理后的数据；体积大 |
| 原始历史缓存（`raw/`、`hist/`、`staging/`、各数据源完整响应） | 体积大（数百 MB）；部分数据源条款不允许再分发 |
| 由真实数据派生的配置：`api/config/kickoff_drift.json`、`api/config/leak_suspect.json` | 来自作者私人库；公开仓只附 `*.example.json` 模板。kickoff_drift 用 `python api/scripts/build_kickoff_drift.py --csv <你的改期扫描 CSV>` 生成；leak_suspect 手工维护 |
| 调好的策略参数 `config/strategy_params.json`、方案台账 `shadow-schemes.csv` 的真实内容、影子预测与验证结果 | 属于作者的研究成果；公开仓只附全 `null` 参数模板和仅表头的台账模板，见 `docs/STRATEGY_PARAMS.md` |
| 各数据源的真实响应样例 | 换成同结构的合成样例（`integrations/5dollar/schema_example/`），不含账户、订阅档位、额度信息 |
| 采集运行产物（`plan/`、`state/`、`queue/`、`rescue_queue/`、`logs/`、`call_log.tsv`） | 本机运行状态，每台机器不同，且含调用记录 |
| 测试用回归库（`api/tests/fixtures/regress_db/*.db`）与基于真实库的基线 JSON | 由私人库导出；相关回归测试在公开版中会自动跳过 |
| 私人消息推送节奏/收件人、供应商账单与合同、订阅档位、个人邮箱 | 个人隐私 |
| 前端 WebUI | 计划在 v0.1 发布 |
