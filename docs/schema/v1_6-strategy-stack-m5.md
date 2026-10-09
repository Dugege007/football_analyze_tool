# 方案叠加 + 验证精算 M5（后端 · schema 切片 v1.6 · 2026-10-06）

> 状态：**可测**。OpenAPI 切片：`openapi-v1.6-stack-draft.yaml`。API 版本：`0.3.7`（0.3.5 首版 + 0.3.6 分析师拍板修订 + 0.3.7 data_rev 含 predictions 签名，见 §8）。
> 表结构沿用 v1.3（`strategy_defs` / `strategy_validation_runs` / `strategy_validation_cache`），**无新 DDL**。
> 引擎：`match-analysis-api/app/backtest.py`（纯函数）；`settlement_version = ah_v3_juice_src_raise`（0.3.5 为 `ah_v2_water_bankroll`）。
> 硬规则：现网亚盘语义、`CFFXDJ_5_V3` 默认与定义**不动**（验证只读 predictions/results/odds）。

## 1. 与 M4 的关系

| 项 | M4 | M5 |
|---|---|---|
| 结算 | 固定 juice 0.95，1 份记 1 | **真实水位**（无则回落 0.95 并计数） |
| 注额 | 1 份 | **资金曲线**：本金 10000，复用 bankroll 份/上下限规则 |
| 指标 | n / pnl(份) / roi | + 金额 pnl、资金序列、**最大回撤（金额+百分比）** |
| compare `stack` | `null` | **实际组合结果**（共用资金，两种 stake_mode，冲突标注） |
| 指纹 | config+scope+settle+params+def_id | + `extra = {settlement_version, stake_rule, bankroll_rules}` |

**指纹**：`run_fingerprint = sha256(canonical({config_fingerprint, scope, settle_book, params, strategy_def_id, extra}))`，
`extra = {settlement_version, stake_rule, bankroll_rules(含 book / water_tier_map / initial_bankroll), data_rev}`（0.3.6；0.3.7 起 data_rev 含方案 predictions 签名）。
口径（结算版本、注额规则、bankroll_config 里的本金/份/上下限）任何一项变化 → 新指纹 → 新 run；
M4 及以前的旧 run 保留在库，但指纹不同，**不会被 M5 误复用**。命中仍 `reused:true`，不重复写。

## 2. 验证精算口径（单方案 validate）

### 2.1 结算（亚盘）
- 盘口：`settle_book`（默认 `macau_close` → `odds_asian.book=macau, phase=close`）主队视角 handicap；四分盘拆半。
- 赢付 `stake × water`，输全损，半赢/半输按半注；走盘 0。
- **水位（0.3.6 口径，见 §8.1）**：`juice_source ∈ fixed_macau | tier_mapped | actual | fallback`。
  - `macau_*`：**仓库固定口径 0.95**（macau 不补采水位，这是口径不是缺陷）→ `fixed_macau`。
  - `crown_* / william_*`：库内 water 是**水档**（1~9）不是赔率；`water_tier_map=true` 时映射，默认关 → `fallback`。
  - 其它书：water ∈ (0,2] → `actual`，否则 `fallback`。
  cache 行 `metrics.juice_source / juice_used / water_raw / reasons`；summary 各来源计数 + `juice_note`。

### 2.2 注额（资金曲线；复用 `/bankroll/calc` 规则）
- 初始本金：`bankroll_config.stats_initial_bankroll.amount`（现为 10000）。
- **1 份金额** = 每个竞彩 ISO 周首个比赛日开盘时的**权益** × `unit_fraction`（默认 0.005 → 起始 50）。对齐「1 份 = 周一基数 × 0.5%」，基数随资金曲线滚动。
- **份数**：`use_prediction_stake=true` 且 `predictions.stake` 非空则用它，否则 `default_units`（默认 …）。
  - 单条 ≤ `stake_caps.per_match`（3）；同场多方案合计 ≤ per_match；同竞彩日合计 ≤ `per_day`（10）。
  - 超限**等比例向下取整，可到 0**（同 `_scale_group`）。
- **金额**：份 × 1 份金额；
  - 单注 ≤ 当前剩余资金 × `max_stake_pct_of_remaining`（0.5）；当前剩余 = 当日开盘权益 − 当日已分配注额；超出时**按整份向下取整**，可到 0 → 不下。
  - 低于 `min_stake_amount`（50）：`below_min=raise`（**0.3.6 默认**，抬到 50，同 `/bankroll/calc`）；
    若 50 > 剩余×50%（即剩余 < 100）→ 不下，reason `bankrupt_guard`（summary `bankrupt_guard_count`），曲线延续；
    `below_min=skip` 仍可显式传 → 不下，reason `below_min_skip`。
- 同日内：先按 (jc_no, match_id, 方案顺序) 分配注额，再逐场结算，权益逐场累加。

### 2.3 run summary 字段
| 字段 | 含义 |
|---|---|
| `n` / `sample_count` / `cache_rows` | 写入 cache 的场数（含不下注） |
| `signal_count` | 方向为主/客的场数 |
| `bet_count` | 实际下了钱的笔数（amount>…） |
| `no_stake_count` | 有方向但因上限/下限导致金额 0 |
| `skip_dir_count` | 不下注 |
| `skipped` / `skipped_detail` | 无赛果 / 无盘口 / 坏方向，未进 cache |
| `stake_total_amount` / `pnl_amount` / `roi` | 金额口径；`pnl` = `pnl_amount` |
| `pnl_units` / `units` | Σ(pnl / 当时 … 份金额) |
| `initial_bankroll` / `final_bankroll` | 本金与期末权益 |
| `max_drawdown` | `{amount, pct, peak_equity, trough_equity, peak_index, trough_index, peak_at, trough_at}`（index … = 起点） |
| `juice_actual_count` / `juice_fallback_count` | 水位来源计数（按有方向的场） |
| `result_counts` | 实际下注的 win/win_half/push/lose_half/lose 计数 |
| `capped_count` / `below_min_skipped_count` | 被上限截断 / 因下限不下 |
| `stake_rule` / `bankroll_rules` / `settlement_version` / `settlement_note` | 口径快照 |

### 2.4 cache 行
- 一场一行（`market=ah`）；`stake_units`=最终份数；`pnl_units`=pnl/…份金额；`result_code ∈ win|win_half|push|lose_half|lose|skip|no_stake`。
- `metrics_json`：`seq, jingcai_date, match_uid, direction, 比分, units_requested, unit_amount, amount, pnl_amount, equity_day_start, equity_after, juice_used, juice_source, settle_code, reasons[], settlement`。

### 2.5 注额规则来源（stake_rule）
- validate：`params.stake_rule`（对象）优先；否则 `config.extras.stake_rule_params`；否则默认 `{default_units:…, use_prediction_stake:true, below_min:"skip"}`。
- 可选键：`default_units`、`use_prediction_stake`、`below_min`、`unit_fraction`。
- 走 `extras` 是因为 config 未知键一律进 extras（不改 `stake_rule` 字符串语义）。

## 3. compare + stack

### 3.1 请求 `POST /strategies/compare`
```json
{
  "strategy_ids": [1, 7],
  "strategy_keys": [],
  "scope": "jingcai",
  "settle_book": "macau_close",
  "auto_validate": true,
  "include_stack": true,
  "stake_mode": "unified",
  "stake_rule": {"default_units": …, "use_prediction_stake": false, "below_min": "skip"}
}
```
- `stake_mode=per_strategy`（默认）：每个方案用自己的 `config.extras.stake_rule_params`（无则默认）；`stake_rule` 被忽略。
- `stake_mode=unified`：所有方案用请求里的 `stake_rule`；**各单方案 item 也按这套规则 validate**（写进 `params.stake_rule`，指纹不同于 per_strategy run）。
- 资金级规则（本金、1 份比例、单场/单日上限、50 下限、50% 上限）两种模式都用 bankroll_config。per_strategy 下 `below_min` 取第一个方案的值。

### 3.2 响应
```json
{
  "stake_mode": "unified",
  "settlement_version": "ah_v3_juice_src_raise",
  "data_rev": "ib:3",
  "items": [
    {"strategy_def_id": 1, "strategy_key": "CFFXDJ_5_V3", "status": "ok", "run_id": 15, "reused": true,
     "summary": {"n": …, "bet_count": …, "pnl_amount": …, "max_drawdown": {"amount": …, "pct": …}},
     "series": {"x": [], "match_ids": [], "pnl": [], "stake": [], "cumulative_pnl": [], "bankroll": [],
                "drawdown_pct": [], "initial_bankroll": 10000}}
  ],
  "stack": {
    "status": "ok",
    "stake_mode": "unified",
    "stake_rule": {"default_units": …},
    "strategies": ["CFFXDJ_5_V3", "M5_DEMO"],
    "bankroll_rules": {"initial_bankroll": 10000, "unit_fraction": 0.005, "per_match": 3, "per_day": 10,
                       "min_stake": 50, "max_pct": 0.5, "below_min": "skip"},
    "conflict_policy": "...",
    "summary": {"n": …, "bet_count": …, "pnl_amount": …, "final_bankroll": …,
                "max_drawdown": {"amount": …, "pct": …},
                "conflict_count": …, "conflict_staked_both_count": …, "conflict_net_pnl": …,
                "per_strategy": {"CFFXDJ_5_V3": {"bet_count": …, "pnl_amount": …}}},
    "series": {"x": [], "match_ids": [], "pnl": [], "stake": [], "cumulative_pnl": [], "bankroll": [],
               "drawdown_pct": [], "conflict": [], "sides": [], "initial_bankroll": 10000},
    "conflicts": [{"match_id": …, "legs": [], "staked_both": true, "net_pnl": …, "net_stake": …}],
    "persisted": false
  }
}
```
- 序列：**按场**一点（同场多方案合并：pnl 求和、stake 求和、bankroll=该场结算后权益）；`drawdown_pct` 为当点相对历史峰值回撤。
- stack 每次请求**实时计算，不落库**（`persisted:false`）；单方案 item 仍来自缓存 run。
- 同 key 多版本同时进 stack 时，key 记为 `KEY@version`。

### 3.3 冲突口径（同场反向）
- 定义：同一场里，有方向的方案中**同时有主和客**。
- 处理：**不对冲、不轧差下注、不合并成一注**；各腿按自己的方向、注额正常结算，共用一个资金池。
- 份数上限照常适用：同场多方案合计 ≤ per_match，同日 ≤ per_day（按比例向下取整，可能把某一腿压到 0 → 该腿 `no_stake`，`staked_both=false`）。
- 每条 `conflicts[]` 带 `match_id, match_uid, jc_id, home_team, away_team, jingcai_date, kickoff_at`；队名优先 `teams.name_zh_canonical`，无则 `matches.home_team/away_team` 文本；`kickoff_at` 取 `matches.kickoff_at`（可空）。
- 标注：序列点 `conflict[i]=true`、`sides[i]={strategy_key: 主|客}`；`stack.conflicts[]` 列各腿 `side/amount/result_code/pnl` 与 `net_pnl / net_stake / staked_both`；汇总 `conflict_count / conflict_staked_both_count / conflict_net_pnl`。
- 注意：对冲两边要付两次水，所以冲突场的净结果通常是小亏（实测 … = … − …）。

## 4. 实测（本地，2026-10-06，demo 已清理）
- `CFFXDJ_5_V3` validate：n=…，signal=…，bet=…，pnl=…（… 注 … × …，赢），final=…，max_drawdown=… / …，juice_fallback=…，actual=…；二次 `reused:true`。
- demo（临时 20 注，match 26 与 V3 反向）：
  - per_strategy（demo 每注 … 份）：stack pnl …，最大回撤 … / …，冲突 …（因单日 … 份上限按比例向下取整，V3 那腿被压到 … → `staked_both=false`）。
  - unified（统一 … 份）：stack pnl …，最大回撤 … / …，冲突 …（`staked_both=true`，net …）。

## 5. 非目标 / 未做
- 不对冲轧差、不做冲突自动消解。
- stack 不落库、无独立指纹缓存。
- 不做 Kelly 概率估计（`stake_rule` 字符串 `fractional_quarter_kelly_units` 只做标签，份数来自 prediction.stake / default_units）。
- crown/william 水位：0.3.6 起提供可选水档映射（默认关），不替代 macau 口径。
- 仅 `market=ah`；OU / 1X2 / 竞彩让球未接。

## 6. 已知边界
- （0.3.6 已修）数据版本 `data_rev` 自动进指纹，见 §8.3。
- （0.3.6 已修）默认 `below_min=raise`，见 §8.2。
- （0.3.7 已修）`data_rev` 含本方案 predictions 内容签名，改预测会自动换指纹；results/odds 的改动仍只靠 import_batches（无批次时靠表计数哈希）感知。

## 7. 测试影子方案 `TEST_V3_MIRROR`（保留，不清理）

- 建/重建：`.venv/bin/python scripts/seed_test_v3_mirror.py`（幂等）。`strategy_defs` id=8，version `2026.10.06-test`，`status=shadow`，`is_default=0`，
  `config.extras = {test_only: true, test_note: "仅用于测试冲突表，不进日用白名单", mirror_of: "CFFXDJ_5_V3", rule: ...}`。
- 预测（`predictions.strategy=TEST_V3_MIRROR`，`stake_rule=test_only`）：V3 主/客 → 取反；V3 不下注且 `match_id % 4 == 0` → `(match_id // 4)` 奇数=主、偶数=客。共 10 条有方向（1 条反向 + 9 条补方向）。
- 实测：bet …，win … / win_half … / lose_half … / lose …，pnl …，最大回撤 … / …；与 V3 冲突 … 场（match … 六… 横滨水手 vs 清水心跳，V3 主赢 …，镜像客输 …，净 …）。

### 现网隔离
| 入口 | 行为 |
|---|---|
| `GET /matches/{id}/prediction`、`/predictions`（ah） | 缺省 `strategy=CFFXDJ_5_V3`，不会取到影子；显式 `?strategy=TEST_V3_MIRROR` 才返回 |
| `GET /matches` 列表 `direction` | 只取 V3 |
| `GET /matches` 列表 `has_prediction` | **M5 改**：计数排除 `strategy_defs.config.extras.test_only=true` 的方案（原先数所有策略） |
| `/dispatch/pending` | 不读 predictions，无影响 |
| `/bankroll/calc` | 缺省 V3 |
| `scripts/import_lib.py` 写预测 | **M5 改**：只删除本条写入策略的旧行（原先删该场全部策略），避免冲掉影子 |
| 级联 | 删场次（rollback / --reset-db）会级联删影子预测，跑 seed 脚本即可恢复 |

验证：改动前后 `/matches` 三个比赛日的 (id, has_prediction, direction) 完全一致；`check_cffxdj5v3_smoke.py` 通过；V3 predictions + strategy_defs#1 哈希不变。

```bash
curl -s -X POST http://127.0.0.1:8787/strategies/compare -H 'Content-Type: application/json' \
  -d '{"strategy_keys":["CFFXDJ_5_V3","TEST_V3_MIRROR"],"stake_mode":"unified","stake_rule":{"default_units":…,"use_prediction_stake":false}}'
```

## 8. 0.3.6 修订（分析师拍板）

### 8.1 水位口径与水档映射

> **已被 v1.7 取代（API 0.3.8）**：库内 crown/william 已迁移为中点水位 `0.70+0.05t`，结算直接用库内水位（`tier_midpoint`），`water_tier_map` 废弃。见 `v1_7-water-tier-midpoint.md`。下文保留为 0.3.6/0.3.7 历史记录。
| settle_book | juice_source | 水位 |
|---|---|---|
| `macau_*` | `fixed_macau` | 固定 0.95（仓库口径，不补采；`water_tier_map` 对 macau 无效） |
| `crown_* / william_*`，`water_tier_map=false`（默认） | `fallback` | 0.95，reason `tier_map_off` |
| `crown_* / william_*`，`water_tier_map=true`，档 ∈ [1,9] | `tier_mapped` | **0.75 + (档−1)×0.05**（6→1.00，3.5→0.875；依据仓库 私有仓特征（已移除） 0.75~1.15 ↔ 1~9 档） |
| 同上，档超出 1~9（库里有 0 / 0.5 / 10 等） | `fallback` | 0.95，reason `tier_out_of_range`，summary `tier_out_of_range_count` |
| 其它 | `actual` / `fallback` | water ∈ (0,2] 用实际，否则 0.95 |

- 开关：`params.water_tier_map`（优先）或 `config.extras.water_tier_map`；生效值 = 开关 ∧ book∈{crown,william}，写进 `bankroll_rules.water_tier_map` → **进指纹**。
- 库内 crown/william 共 1059 行盘口，其中 194 行至少一侧档超出 1~9。

5 场核对（`odds_asian`，档 → 映射水位）：

| match | jc_id | 书 | 阶段 | 盘口 | 主档 | 客档 | 主水位 | 客水位 |
|---|---|---|---|---|---|---|---|---|
| 26 | 六204 | crown | open | 0.25 | 7 | 2.5 | 1.05 | 0.825 |
| 26 | 六204 | crown | close | 0.25 | 6 | 3.5 | 1.00 | 0.875 |
| 26 | 六204 | william | open | 0.5 | 10 | 0 | 超范围→0.95 | 超范围→0.95 |
| 26 | 六204 | william | close | 0.25 | 3 | 1.5 | 0.85 | 0.775 |
| 4 | 一004 | crown | close | 1.0 | 3 | 5.5 | 0.85 | 0.975 |
| 4 | 一004 | william | open | 0.75 | 0 | 5.5 | 超范围→0.95 | 0.975 |
| 8 | 二202 | crown | close | 0.25 | 4 | 5 | 0.90 | 0.95 |
| 8 | 二202 | william | open | 0.5 | 7 | 0 | 1.05 | 超范围→0.95 |
| 12 | 三203 | crown | close | 0.25 | 3 | 6 | 0.85 | 1.00 |
| 16 | 四203 | crown | close | 3.25 | 6 | 3 | 1.00 | 0.85 |
| 16 | 四203 | william | close | 3.0 | 1 | 3.5 | 0.75 | 0.875 |

### 8.2 below_min 默认 raise + bankrupt_guard
- 顺序：份 × 1份金额 → 超剩余×50% 按整份向下取整 → 若 < 50：
  - `raise`（默认）：50 ≤ 剩余×50% → 抬到 50（`min_stake_raised`）；否则不下（`bankrupt_guard`）。
  - `skip`：不下（`below_min_skip`）。
- summary：`min_stake_raised_count`、`bankrupt_guard_count`；`below_min_skipped_count` = below_min_skip + bankrupt_guard。
- 测试参数：`params.initial_bankroll`（覆盖本金，进指纹）。

### 8.3 data_rev（自动数据版本，进指纹；0.3.7 加 predictions 签名）
- 算法：`data_rev = <base>|<pred_sig>`
  - `base`：`import_batches` 有行 → `ib:<MAX(id)>`；否则 `h:<sha256("results:cnt:max|odds_asian:cnt:max|matches:cnt:max")[:12]>`。
  - `pred_sig`：**本次 validate 的方案**的 predictions 签名 `p:<行数>:<sha256(按 match_id 排序的 [match_id, direction, stake, settle_book])[:12]>`。
    不含 id / produced_at → 删后重插同内容（seed 脚本）签名不变；**改 MIRROR 不会让 V3 重算**。
  - `data_rev_source`：`import_batches.max_id+predictions_sig` / `table_counts_hash+predictions_sig` / `explicit`。
- `params.data_rev` 显式传入仍可覆盖（不再需要手动传）。
- 回显：validate 顶层 + summary `data_rev / data_rev_source`；compare 顶层 `data_rev` 为基础版本（无方案签名），各方案完整值见 `items[].summary.data_rev` 与 `stack.data_rev_by_strategy`。
- 实测（0.3.7）：V3 `ib:3|p:39:<指纹已移除>`，MIRROR `ib:3|p:10:<指纹已移除>`。临时改 1 条 MIRROR 预测 → MIRROR 签名 `p:10:<指纹已移除>`、指纹变；V3 指纹不变 reused；恢复后 MIRROR reused 原 run；重跑 seed 脚本签名不变、reused。
- 0.3.6 实测（仅 ib）：临时插 import_batches → `ib:5`、指纹变；删除恢复 → reused。

### 8.4 实测（2026-10-06，data_rev=ib:3）
- V3 validate（raise 默认）：n …，bet …，pnl …，final …，MDD …，fixed_macau …；二次 reused。
- data_rev：临时插 import_batches → `ib:5`、指纹变、新 run；删除恢复 → `ib:3`、reused 原 run；显式 `manual-…` → 新指纹。
- bankrupt_guard（MIRROR，initial_bankroll=…）：首注抬到 … 输半 → …，之后 … 注 `bankrupt_guard`，曲线停在 …；MDD … / …。显式 skip：… 注。
- 水档（MIRROR，crown_close）：开关关 pnl …（fallback …）；开 pnl …（tier_mapped …，如档 …→…、…→…）。macau + 开关 → 生效值 false，fixed_macau。
- compare V3 + MIRROR（两种模式数字相同，因两方案都无 predictions.stake，均为 … 份）：V3 …；MIRROR …（MDD … / …）；stack …，final …，MDD … / …，冲突 …（六… 横滨水手 vs 清水心跳，净 …）。

> **2026-10-06 分析师结论（水档映射暂不可用）**：仓库代码把 crown/william 的 home_water/away_water 当原始水位 0.75–1.15 读，但实际 JSON 存的是 0–10、0.5 步进的数（皇冠主+客恒为 8.5）。`0.75+(档−1)×0.05` 没有仓库依据。`water_tier_map` 保持默认关闭、不要据此出数；结算继续 `fixed_macau` 0.95；`tier_out_of_range` 标记保留。真实含义待用户答复后再定换算。
