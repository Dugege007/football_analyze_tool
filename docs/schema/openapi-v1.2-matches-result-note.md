# OpenAPI 补丁说明 · GET /matches list `result`（v0.3.2）

- **变更**：`MatchListItem` 增加可空字段 `result`，结构与详情 `MatchDetail.result` / `Result` 一致（`home_goals` / `away_goals` / `total_goals` / `wdl`）。无赛果为 `null`。
- **实现**：`build_result()` 与详情共用；不另造字段语义。
- **文档**：已写入 `openapi-v1.2.yaml` 的 `components.schemas.MatchListItem`。
- **非目标**：不改亚盘 / 预测 / bankroll / dispatch 语义；`CFFXDJ_5_V3` 默认不变。
- **API 版本**：health `0.3.2`。
