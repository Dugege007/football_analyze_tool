# 方案工场 M2（后端 · schema v1.3 · 2026-10-06）

> 状态：**正式**。DDL：`v1_3_strategy_workshop.sql`（原草案 `v1_3_strategy_workshop_m2_draft.sql` / `v1_3-strategy-workshop-m2-draft.md` 指向本文）。
> OpenAPI 切片：`openapi-v1.3-strategies-draft.yaml`（路由可 stub；完整 validate 逻辑 OUT OF SCOPE）。

## 表

| 表 | 用途 |
|---|---|
| `strategy_defs` | 方案定义，`(strategy_key, version)` 唯一；`markets_json` / `config_json` 配置驱动 |
| `strategy_validation_runs` | 一次验证/回测批次（允许纯影子） |
| `strategy_validation_cache` | 场×玩法缓存，供 M3 编排 / M4 折线；含 `row_fingerprint` |

## 指纹

| 字段 | 表 | 用途 |
|---|---|---|
| `config_fingerprint` | `strategy_defs` | `config_json` 规范化哈希 |
| `run_fingerprint` | `strategy_validation_runs` | def指纹+params+settle+scope；同指纹可复用整次 cache（**UNIQUE WHERE NOT NULL**） |
| `row_fingerprint` | `strategy_validation_cache` | 单场输入审计（可选） |

哈希算法落地时再定（建议 SHA-256 hex）；**本 migration 只留列，不写死算法实现**。

## `config_json` 首版最小键（分析师确认）

```json
{
  "settle_book": "macau_close",
  "juice": 0.95,
  "vote": {"members": [], "threshold": 3},
  "gates": [],
  "stake_rule": "fractional_quarter_kelly_units",
  "state_machine": null,
  "feature_refs": [],
  "extras": {}
}
```

- **未知键一律放 `extras` 对象**；表结构不因新键改列。
- 改规则必须新 `version` 行；不覆盖历史验证缓存。

## 影子验证

- 验证 run **允许纯影子**：只写 `strategy_validation_cache`，**不必先绑入库** `predictions`。
- 若有入库预测批次可关联，把可选字段 `linked_prediction_batch` 放进 `params_json`（例如 `{"linked_prediction_batch": "batch_uid_or_label", ...}`）。

## 其它约定

- 不硬编码玩法枚举；`market` 字符串与 `prediction_legs` 对齐。
- `is_default` 只影响工具展示，不擅自改仓库 `CFFXDJ_5_V3` 默认。
- 预测入库仍走现有 `predictions` / `prediction_legs`；本表管「方案元数据 + 验证结果」。
- 现网亚盘 / bankroll API **不因本迁移改语义**。

## API（OpenAPI 切片）

见 `openapi-v1.3-strategies-draft.yaml`：

- `GET/POST /strategies`
- `GET /strategies/{key}/versions`
- `POST /strategies/{id}/validate` → 创建 run（可影子）
- `GET /strategies/runs/{run_id}`
- `GET /strategies/runs/{run_id}/cache`

实现侧：可只挂 stub（空列表 / 占位 run）；**完整 validate 编排 OUT OF SCOPE（M3）**。
