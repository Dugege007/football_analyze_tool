# 验收清单：模拟下注 / 影子 / 灌库防泄漏

> 2026-10-07。配合 [`as-of-betting-iron-rules.md`](./as-of-betting-iron-rules.md)、[`mutex-buckets-min-n.md`](./mutex-buckets-min-n.md)、[`shadow-walkforward-twdecay-v1-cli-fingerprint.md（未公开）`](./shadow-walkforward-twdecay-v1-cli-fingerprint.md（未公开）)、[`../schema/v2_0-jingcai-day-early-kickoff.md`](../../schema/v2_0-jingcai-day-early-kickoff.md)。  
> **用途**：纸面下注、影子验证、ingest 前逐项打勾；任一硬闸 FAIL → 禁止灌库、禁止宣称「优于基线」。

---

## 1. As-of 铁律（必读）

全文：[`as-of-betting-iron-rules.md`](./as-of-betting-iron-rules.md)（schema 副本：`schema/v2_0-as-of-betting-iron-rules.md`）。

| # | 检查项 | Pass 条件 |
|---|--------|-----------|
| A1 | 钉在下注时刻 | 特征／权重／参数／方向仅用当时 cutoff 前信息；本场及之后赛果未进入 |
| A2 | 长跑有效性 | 报告窗从约定起点一路往今；不得只挑好看子段当主结论 |
| A3 | 权重钉在当时 | 每场按该场 cutoff 往回重算 Δ／权重；无「今天视角」一次定死全表权重 |
| A4 | 预测快照冻结 | 已灌／已发布 uid 方向未因全量重算被覆盖 |
| A5 | 新衰减新码 | 新 `weight_profile` 用新 `strategy_code`；现网 V3 主列未静默替换 |

---

## 2. 泄漏族（Leakage families）

每族至少抽查 ≥3 场或跑自动化 guard；记 FAIL 原因与场次 uid。

### 2.1 标签泄漏（label）

- [ ] 未用终场比分、赛后 xG、赛后评级、赛后盘作**当时**特征或筛参
- [ ] 训练／调参标签时点 ≤ 该样本决策 cutoff
- [ ] 结算字段与特征字段分列；脚本未把 settle 列喂回方向计算

### 2.2 盘口时间线泄漏（odds-timeline）

- [ ] **临盘（规则）**（`close`）取自决策前快照，非赛后盘；不得「用临盘代替即时」
- [ ] 特征阶段与指纹一致（`open`／`mid`／`close`／`mid_real`／`close_real`／`live@…`）；例外场若用真实列须另指纹
- [ ] 未使用当时尚未开出的机构盘／水位
- [ ] 澳门／平博等 settle_book 与特征 book 声明一致；换 book 须新对照行
- [ ] CLV 三列（平博）：规则=`close`、真实=`close_real`、收盘=`last_prematch`；收盘列无记录为 null 且不顺延；`stale`（>15 分钟；新鲜度暂定 15 分钟，待平博快照密度统计复核）与开赛后快照不进 CLV（收盘）；三列互比仅用齐备子集

### 2.3 样本选择偏差（sample selection）

- [ ] 回测分母含当时可决策场（含无盘／腰斩／空方向），非只留「后来有信号」的场
- [ ] `n_eligible` / `hits` / `coverage` 按 [`mutex-buckets-min-n.md`](./mutex-buckets-min-n.md) 定义；缺数记 `null` 不记假 0
- [ ] 扩库前后底座缺口与追加 uid 对齐说明已归档

### 2.4 参数偷看（param peeking / nested walk-forward）

- [ ] 外层测试月**未**参与选 θ；内层 train+valid 选参（见 TW-DECAY-V1 CLI）
- [ ] 未用全样本调门槛后再在同一段报 ROI
- [ ] `config_fingerprint` 与报告窗、profile、结算口径一致且可复现

### 2.5 结算口径混用（settlement mix）

- [ ] 主对比固定一种结算（默认 `fixed_095`／约定 settle_book）；真水／多 book 仅作敏感性另表
- [ ] 走水单独计，不进胜负分母（与互斥页一致）
- [ ] 禁止主结论曲线偷偷换抽水或 book

### 2.6 队名／赛事错配（team/comp mismatch）

- [ ] `match_uid`／队名／赛事映射抽查无错队、错轮
- [ ] 竞彩编号日 vs 日历开赛日口径差已标注（见 kickoff-minute 笔记），非静默错映

### 2.7 时区／竞彩日（timezone / jingcai-day）

- [ ] 轴为竞彩日 `(match.date, kickoff)`，勿用「自然日−1」偷换
- [ ] 早场特殊 cutoff 带与下文 §3 一致（拓宽后：**约至 11:30**；代码待改前以仓库现状为准并标注）
- [ ] 时区统一 UTC+8／Asia/Shanghai；跨日场归属与 `jingcai_date` 一致

---

## 3. 竞彩日早场开赛窗（cutoff 特殊带）

| | 内容 |
|--|------|
| **旧规则（代码／`.cursor/rules/analysis_window_cutoff.mdc` 现状）** | 连续窗：`0<=h<=10` → `cutoff_hour=21`；否则 `cutoff_hour = h-4` |
| **用户 2026-10-07** | 前一天竞彩序号可在**次日自然日约 12:00 前**开赛；须拓宽覆盖至约 **11:30** |
| **文档工作口径（PENDING CODE）** | 早场特殊带：开赛时刻 ∈ **[00:00, 11:30]**（含端点）→ 仍用 `cutoff_hour=21`；**仅改特殊带宽度，不改晚场 `h-4`** |
| **分钟精度** | 有 `kickoff_minute`／`kickoff_at` 时用分钟判定；仅有整点 `h` 时暂用 `h<=11`，并注明 **11:31–11:59 需分钟字段才能排除** |
| **状态** | 口径已定，**代码待改**；改码后须影子重跑；**禁止**用新 cutoff 整表重写已冻结现网 V3（177） |

决策卡：[`schema/v2_0-jingcai-day-early-kickoff.md`](../../schema/v2_0-jingcai-day-early-kickoff.md)。

**提议代码谓词（待合入仓库）**：见该决策卡 §「Proposed predicate」。

验收：

- [ ] 文档与决策卡一致
- [ ] 代码合入后：抽查「前一日序号、次日 10:00–11:30 开赛」场，cutoff 为 21；12:00 及更晚仍走 `h-4`
- [ ] cutoff 变更后：新影子包重跑；旧 V3 uid **splice／skip-existing**，不整表覆盖

---

## 4. 快照冻结 / splice / `--skip-existing`

| 检查项 | Pass |
|--------|------|
| 扩库／重跑对照旧 uid | 方向／confidence／rationale／settle／produced_at（约定字段）0 差异，或明确隔离失败场 |
| 追加策略 | 仅插入新 uid；`inserted=N` / `skipped=旧集合` 有验收笔记 |
| 禁止 | 全量 rebuild CSV 直接替换现网；无 `--skip-existing` 的 upsert 盖旧方向 |
| 备份 | ingest 前 CSV／DB bak 路径已记 |

参考：今晚 V3 splice 138 验收笔记（`backfill/v3-expand-2026-10-07/`）。

---

## 5. 新权重 profile 与 TW-DECAY-V1

| 检查项 | Pass |
|--------|------|
| profile 集合 | `none` / `tw182`（基线）+ `tw_exp_hl91` / `tw_exp_hl182` / `tw_tri_h182`（影子） |
| 双轨列 | `建议方向_<profile>` 等；**主列不动** |
| `strategy_code` | `CFFXDJ_5_V3__twdecay_v1__<profile>__<variant>`；含 `config_fingerprint`、`status=shadow`、`test_only` |
| 灌库 | 新衰减**只**进新 strategy／影子；DUAL_WRITE 关时不写现网 V3 主列 |
| 晋升 | ≥3 个外层月不劣于 tw182 且达 L2（互斥页）；现网主列另拍板 |

CLI／指纹：[`shadow-walkforward-twdecay-v1-cli-fingerprint.md（未公开）`](./shadow-walkforward-twdecay-v1-cli-fingerprint.md（未公开）)。

---

## 6. 灌库／宣称优于基线前的 Pass/Fail 闸

**全部硬闸 Pass 才可 ingest 或对外宣称 beat baseline。**

| 闸 | Fail 则 |
|----|---------|
| §1 铁律 A1–A5 | 停；修口径 |
| §2 任一泄漏族硬 FAIL | 停；修特征／样本／结算 |
| §3 早场口径：代码未改时用旧谓词并标注；代码已改但未影子重跑 | 禁止用新结果盖旧 V3 |
| §4 旧 uid 漂移 | 禁止整表替换；改 splice 或隔离 |
| §5 新 profile 误写入现网主列 | 回滚；只留影子 strategy |
| L0/L1/L2（[`mutex-buckets-min-n.md`](./mutex-buckets-min-n.md)） | 未达宣称级别则只报 hits/coverage，不报「可上生产」 |
| 主结算 | 非约定口径的 ROI 不得当主结论 |

**签字栏（可选）**

| 角色 | 日期 | 结论 (PASS/FAIL) | 备注 |
|------|------|------------------|------|
| 足球分析师 | | | |
| 后端 | | | |
| 算法顾问 | | | |

---

## 修订

| 日期 | 变更 |
|------|------|
| 2026-10-07 | 首版：铁律链接、七类泄漏、早场拓宽至 ~11:30（代码待改）、splice、TW-DECAY-V1、灌库闸 |
