# 方案编排器 M3（后端 · schema 切片 v1.4 · 2026-10-06）

> 状态：**MVP / 可测桩**。表结构沿用 M2（`v1_3_strategy_workshop.sql`），本阶段不加新表。
> OpenAPI 切片：`openapi-v1.4-composer-draft.yaml`。
> API 版本：`0.3.3`（在 `0.3.2` list `result` 之后）。

## 目标

1. 从 **config 模板**生成并写入 `strategy_defs`（**新 version 行**，不覆盖历史）。
2. `POST /strategies/{id}/validate`：**真正创建 run**；按 `run_fingerprint` 命中则**复用**已有 run/cache；未命中则写 run + **最小汇总 / 空 cache 占位**。
3. 状态机用**命名模板**字符串：`null`（或 JSON `null`）/ `simple_gate`；其余未知 config 键进 `extras`。
4. **不改**现网亚盘语义与 `CFFXDJ_5_V3` 默认行为。

## 与 M2 表关系

| 表 | M3 用法 |
|---|---|
| `strategy_defs` | compose / POST 写入新 version；写入时规范化 `config_json` 并填 `config_fingerprint` |
| `strategy_validation_runs` | validate 插入；`run_fingerprint` UNIQUE（非空）用于整次复用 |
| `strategy_validation_cache` | 命中则读已有行；未命中 MVP 可空（占位），完整回测后续加厚 |

指纹建议：SHA-256 hex；`config` / `params` 先做键排序规范化 JSON（`sort_keys`，UTF-8，无多余空白）。

## `config_json` 与状态机

首版最小键（同 M2）+ 状态机约定：

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

- `state_machine`：**命名模板** — `null`（JSON `null` 或字符串 `"null"` → 存 `null`）或 `"simple_gate"`。
- 未知模板名：原值放入 `extras.state_machine_unknown`，`state_machine` 置 `null`。
- **未知顶层键**一律并入 `extras`；表结构不改列。

### 内置模板（compose）

| template | 说明 |
|---|---|
| `default` | 上表最小键；`state_machine: null` |
| `simple_gate` | 同 default，但 `state_machine: "simple_gate"` |

`config_overrides` 浅合并进模板后，再跑规范化（未知键 → `extras`）。

## API 表面

| Method | Path | 说明 |
|---|---|---|
| GET | `/strategies/templates` | 列出内置模板名与默认 config |
| POST | `/strategies/compose` | 模板 + overrides → 新 `strategy_defs` 行（201）；`(key,version)` 冲突 409 |
| POST | `/strategies` | M2 已有；M3 起同样规范化 config + 写 `config_fingerprint` |
| POST | `/strategies/{id}/validate` | 算 `run_fingerprint`；命中复用；未命中建 run + placeholder summary |
| GET | `/strategies` / `.../versions` / `.../runs/{id}` / `.../cache` | 同 M2 |

### validate 行为（MVP）

1. 读 `strategy_defs`；若缺 `config_fingerprint` 则按当前 `config_json` 即时计算（不强制回写）。
2. `run_fingerprint = sha256(canonical({config_fingerprint, scope, settle_book, params}))`（`params` 含 `shadow` 等）。
3. **命中**：返回已有 run（响应带 `reused: true`），不新建。
4. **未命中**：插入 run（`status=ok`，`finished_at=now`），`summary_json` 形如：
   ```json
   {"placeholder": true, "pending": true, "note": "backtest logic not fully wired (M3 MVP)"}
   ```
   cache：**空列表占位**（不强制插假 match 行；完整回测 M3+ 再写场×玩法行）。
5. 允许纯影子：`shadow=true` 不必绑 `predictions`；可选 `params.linked_prediction_batch`。

## 非目标

- 不改 `CFFXDJ_5_V3` 作为现网预测默认策略的行为（`DEFAULT_STRATEGY` / 亚盘方向语义）。
- 不改现网亚盘、bankroll、dispatch。
- 不做完整回测 / ROI / 折线（留给后续加厚；cache 可先空）。
- 不新增 DDL 表（继续用 v1.3 三表）。

## 验收（本地）

```bash
curl -s http://127.0.0.1:8787/health   # version 0.3.3
curl -s http://127.0.0.1:8787/strategies/templates
curl -s -X POST http://127.0.0.1:8787/strategies/compose \
  -H 'Content-Type: application/json' \
  -d '{"strategy_key":"DEMO_COMPOSE","version":"2026.10.06a","template":"simple_gate","status":"shadow"}'
# validate 两次：第二次 reused=true，同一 run id
ID=...
curl -s -X POST http://127.0.0.1:8787/strategies/$ID/validate \
  -H 'Content-Type: application/json' \
  -d '{"shadow":true,"params":{"window":"demo"}}'
```
