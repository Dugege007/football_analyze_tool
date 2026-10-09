# 5DollarFootballAPI（5DF）接入说明

- 基础地址：`https://api.5dollarfootballapi.com/v1/`
- 认证：请求头 `Authorization: Bearer <FIVEDOLLAR_FOOTBALL_API_KEY>`，key 只从环境变量 / 仓库根 `.env` 读取。
- 本目录内容：
  - `live-capture.md`：临场赔率采集（`scripts/live/live_capture.py`）的设计说明
  - `rescue-queue.md` + `rescue_queue_schema_example.json`：漏采补救队列的规则与字段示例（示例为合成数据）
  - `prematch-probe/call.sh`、`probe/probe.py`：手动调用某个端点并记录响应头（调试用）
  - `schema_example/*.json`：各端点响应的**合成样例**，结构与真实响应一致，所有值（ID、队名、赔率、时间）均为虚构；不含账户、订阅档位或额度信息

各端点能否调用、频率上限是多少取决于你自己的 5DF 账户，请以 5DF 官方文档和响应头 `X-RateLimit-*` 为准。
