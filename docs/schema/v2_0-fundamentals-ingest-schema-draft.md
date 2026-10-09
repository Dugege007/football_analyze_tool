# 基本面入库 · 表结构与 BSD 最小集草案（副本优先）

> **状态**：方案草案，供后端落地。先落 **v2d3 副本**，`DUAL_WRITE` **关**，不写现网。  
> **日期**：2026-10-08 18:56 UTC+8  
> **口径来源**：[`v2_0-jc-and-fundamentals-status-20261008.md`](./v2_0-jc-and-fundamentals-status-20261008.md)、[`v2_0-as-of-betting-iron-rules.md`](./v2_0-as-of-betting-iron-rules.md)、BSD 冒烟 [`../bsd-bzzoiro/notes.md`](../bsd-bzzoiro/notes.md) / [`../bsd-bzzoiro/prematch-probe-2026-10-06/README.md`](../bsd-bzzoiro/prematch-probe-2026-10-06/README.md)

---

## 0. 现状（v2d3 `stats`，177 场手工）

| 字段 | 齐备度 | 备注 |
|---|---|---|
| `recent_json` | 177/177 | 近 10／近 6 进失等 |
| `h2h_json` | 177/177 | 交锋 |
| `rank_home` / `rank_away` | 175/177 | 排名 |
| `popularity_diff` | 多数有 | 竞彩 vote 样例常空，另议 |
| `injury_json` | **0/177** | 空；旧方案常把 injury 当 0 |
| `weather_json` | 26/177 | 稀疏 |
| xG / 阵容 / 积分榜流水 | **无表** | BSD 仅探测 |

现 `stats` **无** `as_of`、`source` → 手工底座与日后自动采无法在覆盖率上分开。

---

## 1. 设计原则（已钉死）

1. **每条观测带 `as_of`**（伤停、阵容尤甚）。决策时刻之后才公布的，live 特征当 **缺失**，禁止回填「事后才知道的名单」。
2. **xG 训练窗 cutoff 同 N5**：开赛时间 + 3 小时 ≤ cutoff 才可用该场 xG；**赛后 xG 不得进赛前特征**。
3. 手工底座标 `source=manual_seed`，与 `bsd`／其它自动源 **分开**；回测覆盖率 **分母／分子都按 source 拆开算**，禁止混报「齐全率」。
4. 先副本；现网 stats 行不覆盖、不删。
5. 本地采集机／竞彩官方与基本面无关时：基本面缺测允许空；**不用别源冒充竞彩**；BSD 也不得冒充 Sporttery。

---

## 2. 表结构草案

### 2.1 推荐双层：观测表 `stats_obs` + 投影表 `stats`（兼容旧读）

**为什么**：伤停／阵容会随时间变；需要「决策时刻能看见哪一版」。单行 `stats` 不够。

```sql
CREATE TABLE IF NOT EXISTS stats_obs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,   -- recent|h2h|rank|injury|lineup|xg|standings|form|weather|popularity
  as_of         TEXT NOT NULL,   -- ISO+08:00，该观测「已知」时刻
  source        TEXT NOT NULL,   -- manual_seed|bsd|sporttery|inferred|other
  payload_json  TEXT NOT NULL,   -- 结构化载荷（见 §2.3）
  provider_ref  TEXT,            -- 如 BSD event_id
  fetched_at    TEXT,            -- 实际拉取时刻（可 ≥ as_of）
  quality       TEXT,            -- ok|partial|stale|probe
  extras_json   TEXT,
  UNIQUE (match_id, kind, as_of, source)
);

CREATE INDEX IF NOT EXISTS idx_stats_obs_match_kind_asof
  ON stats_obs (match_id, kind, as_of);
CREATE INDEX IF NOT EXISTS idx_stats_obs_source
  ON stats_obs (source, kind);
```

**投影 `stats`（保持现列，供旧 API）** —— 副本 ALTER 加出处，不改业务列语义：

```sql
ALTER TABLE stats ADD COLUMN source_recent TEXT;     -- 投影所用 source
ALTER TABLE stats ADD COLUMN as_of_recent TEXT;
ALTER TABLE stats ADD COLUMN source_h2h TEXT;
ALTER TABLE stats ADD COLUMN as_of_h2h TEXT;
ALTER TABLE stats ADD COLUMN source_rank TEXT;
ALTER TABLE stats ADD COLUMN as_of_rank TEXT;
ALTER TABLE stats ADD COLUMN source_injury TEXT;
ALTER TABLE stats ADD COLUMN as_of_injury TEXT;
ALTER TABLE stats ADD COLUMN source_weather TEXT;
ALTER TABLE stats ADD COLUMN as_of_weather TEXT;
ALTER TABLE stats ADD COLUMN source_popularity TEXT;
ALTER TABLE stats ADD COLUMN as_of_popularity TEXT;
ALTER TABLE stats ADD COLUMN extras_json_meta TEXT;   -- 若与现 extras_json 冲突则只用 extras 内嵌 meta
```

说明：若担心 `extras_json` 已被业务占用，出处优先独立列；`lineup`／`xg`／`standings`／`form` **只进 `stats_obs`**，暂不塞进宽表，避免把空 JSON 当「有数」。

**一次性打标（副本）**：现有 177 行投影字段标：

- `source_*=manual_seed`
- `as_of_*=` 导入批次日或 `jingcai_date` 当日 12:00（若未知）；`fetched_at` 记迁移时刻  
- **禁止**把 `injury_json` 空值改成伪 `[]` 冒充「已采无伤停」

### 2.2 可选：`stats` 内嵌 xG 快捷列（二期）

仅当 xG 稳定进特征后再加：

```sql
-- 二期，非本阶段必须
ALTER TABLE stats ADD COLUMN xg_home REAL;
ALTER TABLE stats ADD COLUMN xg_away REAL;
ALTER TABLE stats ADD COLUMN xg_as_of TEXT;
ALTER TABLE stats ADD COLUMN xg_source TEXT;
ALTER TABLE stats ADD COLUMN xg_scope TEXT;  -- prematch_model|postmatch_actual
```

本阶段：**只写 `stats_obs.kind='xg'`**。

### 2.3 `payload_json` 最小约定

| kind | payload 关键键 |
|---|---|
| `recent` | 与现 `recent_json` 同结构（last10/last6 gf/ga 等） |
| `h2h` | 同现 `h2h_json` |
| `rank` | `{home, away}` |
| `injury` | `{home:[{player,status,as_of?}], away:[…], summary?}`；无名单时 `home/away=[]` 且 `known_empty=true`（仅当源明确「已确认无伤停」） |
| `lineup` | `{home:[…], away:[…], confirmed:bool}` |
| `xg` | `{home, away, scope:prematch_model\|postmatch_actual, model?}` |
| `standings` | `{home_rank, away_rank, table_as_of, pts_home, pts_away, …}` |
| `form` | BSD form 原文精简 |
| `weather` | 同现 `weather_json` |
| `popularity` | `{diff, home_pct?, away_pct?, source_detail?}` |

---

## 3. 防泄漏（硬闸门）

### 3.1 通用 `as_of`

决策时刻 `T_decision`（中盘／临盘／方案 cutoff）取特征时：

```
可用观测 = stats_obs
  WHERE match_id=? AND kind=?
    AND as_of <= T_decision
    AND source IN (允许列表)
  ORDER BY as_of DESC
  LIMIT 1
```

若无行 → 该特征 **缺失**（不是 0）。  
`fetched_at > T_decision` 但 `as_of ≤ T_decision`：允许（事后拉取历史快照），须在 extras 标明 `fetched_after_decision=true`；影子审计可分组。

### 3.2 xG 特则（同 N5）

- `scope=postmatch_actual`：**禁止**进入任何 `T_decision < kickoff+3h` 的赛前／临盘特征。
- 进训练窗／样本场：该场须满足 `kickoff_at + 3h <= cutoff`（cutoff 按竞彩日编号规则算，不用含糊 `match.date`）。
- `scope=prematch_model`：仍须 `as_of <= T_decision`；模型分若在开赛后才更新，按新 `as_of` 处理。

### 3.3 伤停／阵容

- 开赛后才公布的首发：`as_of` 开赛后 → 对临盘决策不可见。
- 禁止用完赛后页面的「赛前名单」回填并把 `as_of` 伪造成赛前。

单测样例（建议后端加）：

1. 伤停 `as_of` 晚于临盘 cutoff → live 特征缺失。  
2. 赛后 xG + 早于 kickoff+3h 的 cutoff → 断言拒绝。  
3. `manual_seed` 与 `bsd` 同行同 kind → 覆盖率接口返回两列，不合并虚高。

---

## 4. BSD 接入最小集与优先级

Base：`https://sports.bzzoiro.com/api/v2/`（Token；约 7500/天 UTC 重置）。  
探测：`bsd-bzzoiro/prematch-probe-2026-10-06/`（事件／form／standings／lineups 等有样例）。

| 优先级 | kind | 动作 | 频率（建议） |
|---|---|---|---|
| P0 | `injury` | 映射 BSD 伤停 → `stats_obs`；投影到 `stats.injury_json` **仅当** as_of 有效 | 赛前 1～2 次／场（如 T−24h、T−2h） |
| P0 | `recent` / `h2h` / `rank` | **维护**：有 BSD／其它自动源则新 `as_of` 行；无则保持 `manual_seed` 投影 | 有赛程变更时；日更一次 |
| P1 | `xg` | 只收 `prematch_model` 进赛前；`postmatch_actual` 单独 kind 行，训练窗闸门 | 完赛 +3h 后拉 actual；赛前拉 model（若有） |
| P2 | `lineup` | 确认阵容；未确认标 `confirmed=false` | T−1h 附近 |
| P2 | `standings` / `form` | 积分榜／近况流水 | 日更或有变时 |
| P3 | `weather` | 低优先级；现 26 场可继续稀疏 | 可选 |
| 另议 | `popularity` | Sporttery vote 常空；不硬依赖 BSD | 有稳定源再开 |

**匹配**：BSD event ↔ `matches` 用队名／开赛时间模糊匹配；失败进 queue，不写错 match_id。  
**配额**：日调用计入 `backfill/quota-status.md`；与 5DF／Sporttery 无关，但仍避开 本地采集机 高峰时的重任务（BSD 可在 box 跑）。

Stub（文档级）：

```
POST /internal/fundamentals/ingest_bsd
  body: { match_ids?, kinds: ["injury"|"xg"|…], as_of_mode: "provider"|"fetched" }
  写入：仅 v2d3.stats_obs；dry_run 支持
```

---

## 5. 覆盖率统计（分源）

接口或脚本日报建议列：

```
coverage[kind][source] = {
  n_matches_with_obs,   # 至少 1 条 obs
  n_matches_total,      # 分母：研究窗内场次数
  n_usable_at_close,    # as_of ≤ 该场临盘 cutoff
  n_usable_at_mid
}
```

规则：

- **禁止** `manual_seed + bsd` 合成一个「齐全 %」对外报。  
- 页面：「手工底座」悬停；自动采另标来源。  
- `injury`：空 payload + 无 `known_empty` ≠ 已覆盖。

---

## 6. 给后端的验收点

1. **副本 only**：DDL／打标／试写入 v2d3；现网 `stats` 行内容与 sha256 不变（或声明未打开现网库）。
2. **`stats_obs` 存在**；UNIQUE(match_id, kind, as_of, source) 生效。
3. **manual_seed 打标**：177 场 recent/h2h/rank 的投影 `source_*=manual_seed`；injury 仍空且不伪造成 `[]`。
4. **as_of 闸门单测**：晚于决策的 injury → 特征缺失。
5. **xG 闸门单测**：postmatch + 过早 cutoff → 拒绝进赛前特征。
6. **覆盖率分源**：同一报表中 `manual_seed` 与 `bsd` 分列；无混算字段。
7. **BSD P0 试写**：至少 1 场探针 `kind=injury` 或 `xg` 写入 obs（`quality=probe` 可）；失败不写现网。
8. **投影一致性**：若更新 `stats.injury_json`，必须同时有对应 `stats_obs` 行且 `as_of_injury` 一致。
9. **验收文档**：`backfill/fundamentals-ingest-<date>/ACCEPTANCE.md`（行数、分源覆盖、泄漏单测、现网未写）。

---

## 7. 开放问题（≤3）

1. **`as_of` 取官网事件时间还是 fetched_at**？推荐：优先提供方事件时间；未知则 `as_of=fetched_at` 并 `extras.as_of_fallback=fetched`。  
2. **injury「确认无伤停」**是否允许 `known_empty=true` 算覆盖？推荐：仅当源显式空名单时，且与「未采到」严格区分。  
3. **队名匹配不上的 BSD 事件**：丢弃 vs 进 `unmatched_events` 表人工映射？推荐后者，避免漏伤停。

---

## 8. 非目标

- 不写现网、不开双写  
- 不在本任务实现完整 BSD 爬虫  
- 不用 BSD／其它源冒充竞彩官方赔率  
- 不把赛后 xG／赛后阵容回填进已冻结预测

### 2026-10-08 18:59 补钉（算法顾问三条，已采纳）
见 v2_0-jc-and-fundamentals-status-20261008.md「算法顾问补三条」。

#### 构成赛闸（必须）
特征提取对 `recent`／`h2h` 载荷内每一场构成赛再滤：`kickoff_at + 3h ≤ T_decision`。不满足的场从该次决策的特征载荷中剔除；剔除后样本不足 → 当缺失（与「未采到」同展示语义），禁止回退到未滤版本。`manual_seed` 粗 `as_of`（如竞彩日 12:00）不豁免本闸。
