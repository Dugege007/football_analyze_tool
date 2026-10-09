# shadow-schemes.csv 字段说明（模板）

公开仓里的 `shadow-schemes.csv` 只有表头，不含任何方案、阈值或验证结果。
你可以按下面的字段登记自己的影子方案，再用 `python api/scripts/seed_shadow_strategy_defs.py` 写入数据库。
如果不想把自己的方案提交到 git，请把文件改名（例如 `shadow-schemes.local.csv`）并通过脚本参数指定，或者不要提交它。

| 字段 | 含义 |
|---|---|
| id | 方案编号，例如 `S1`、`N4`。全表唯一 |
| source | 方案来源（论文、自己的想法、某份研读笔记等） |
| rule_one_liner | 一句话规则描述。**不建议写具体阈值**，阈值放到 `config/strategy_params.json` |
| data_deps | 依赖的数据（例如「澳门亚盘初/终盘」「欧赔 1X2」） |
| settle_default | 默认结算口径（例如 `macau_ah`） |
| status | 状态：`stub`（只登记）/ `active`（跟踪中）/ `retired`（已停） |
| report_date | 最近一次复核日期（YYYY-MM-DD） |
| notes | 备注；含 `stub` 字样时视为占位方案 |
