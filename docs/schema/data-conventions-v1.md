# 分析工具 · 数据约定 v1（2026-10-06）

> 仅约定**数据精度、命名、映射与清理**；不定义预测算法。  
> 对齐：作者的私有分析仓 `00_数据集/YYYY/YYMM.json`、`v1_sqlite.sql`、`openapi-v1.1.yaml`、`../backfill/schema/monthly-json-schema.md`、`../backfill/schema/daily-match-scope-and-json-v2.md`、`analysis-tool-roadmap-20261006.md`。  
> 不确定处标 **TODO**，不臆造仓库行为。

---

## 1. 日期与开赛时间

### 1.1 时区

- 业务默认时区：**UTC+8 / Asia/Shanghai**。
- 写入数据库与 API 的绝对时间，优先使用 **ISO-8601 带偏移**，例如：`2026-10-07T03:15:00+08:00`。
- 不要把「竞彩日」与「自然开赛日」混用（见下）。

### 1.2 开赛时间字段

| 字段 | 含义 | 约定 |
|---|---|---|
| `kickoff_hour` | 开赛小时 0–23（仓库既有） | **保留**，供 `jc_day_hour_order` 排序与旧脚本兼容 |
| `kickoff_at` | 完整开赛时刻 | **优先**；ISO-8601 + `+08:00`；精确到**分钟**（秒可为 `00`） |
| `kickoff_minute_known` | 是否已知到分钟 | `true`：`kickoff_at` 的分钟可信；`false`：仅来自历史整点记录（分钟为占位，通常 `:00`） |

**规则**

1. 新采集 / 新导入：必须写 `kickoff_at`，且 `kickoff_minute_known = true`。
2. 旧手工仅整点：可写 `kickoff_at = …T{HH}:00:00+08:00`，同时 `kickoff_minute_known = false`；`kickoff_hour` 与之一致。
3. SQLite `matches` 表当前草案仅有 `kickoff_hour`（见 `v1_sqlite.sql`）；`kickoff_at` / `kickoff_minute_known` 的正式列或 JSON 扩展 —— **TODO：后端 migration**（OpenAPI Match 已预留可空 `kickoff_at`）。
4. 回填 JSON 扩展层已允许 `kickoff_at`（见 `daily-match-scope-and-json-v2.md`）；兼容层仍保留 `kickoff_hour`。

### 1.3 竞彩日 `jingcai_date`（又称 match.date）

- 含义：**竞彩日**，不是必然等于自然日。
- 仓库规则（学习笔记 / 日清单草案）：凌晨 **0–10** 点场次挂在**前一个竞彩日**下；排序用 `jc_day_hour_order`（0–10 → 24–34）。细节以仓库 `README_数据说明` / 核心脚本为准 —— 若本地未挂仓库副本，实现时 **TODO：对照仓库再核对边界小时**。
- API / DB 字段名：现有 schema 用 `jingcai_date`；JSON 兼容层用 `match.date`。二者同义。
- 存储格式建议：**`YYYY-MM-DD`**（与 OpenAPI `format: date` 一致）。  
  - 若某处仍见 `YYMMDD` 或仅日序号，导入时规范化为 `YYYY-MM-DD` —— **TODO：importer 校验规则**。

### 1.4 月度文件命名与 `jingcai_day`

| 概念 | 约定 |
|---|---|
| 月度文件 | `00_数据集/YYYY/YYMM.json`（数组；`YYMM` 如 `2607` = 2026-07） |
| `meta.month` / import `month` | 与文件名 **`YYMM`** 一致（两位年 + 两位月） |
| 日清单 / 竞彩日目录 | 按 **`YYYY-MM-DD`（竞彩日）** 组织作业；不按自然日拆凌晨场 |
| 附清单 | 可选 `YYMM_extra.json`（`scope=extra`）；默认分析不含 |

「jingcai_day」若出现在脚本参数中，一律解释为**竞彩日 `YYYY-MM-DD`**，与文件名 `YYMM` 是不同粒度，禁止混写进同一字段。

---

## 2. 队名与联赛（规范名 + 别名）

### 2.1 原则

1. 每个实体一个**中文规范名**（`name_zh_canonical`）；消息、列表、报告**只展示规范名**。
2. 各平台 / 手工录入的其他叫法进入**别名表**，多对一映射到同一 `team_id` / `league_id`。
3. **以当前数据集中已出现的中文名为准**固化规范名；新增队伍查**较常用中文名**后固定，后续别名只增不改规范名（改名需人工审批 —— **TODO：审批流程**）。
4. 不清的名称：先查公开资料，再按同场对手 / 联赛 / 开赛时间匹配后挂映射；无法确认则标记 `unresolved`，**禁止**在消息里用临时英文乱码顶替。

### 2.2 建议表结构（草案；正式 DDL TODO）

```sql
-- 队
CREATE TABLE IF NOT EXISTS teams (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  name_zh_canonical  TEXT NOT NULL UNIQUE,
  created_at         TEXT NOT NULL DEFAULT (datetime('now')),
  note               TEXT
);

CREATE TABLE IF NOT EXISTS team_aliases (
  alias      TEXT NOT NULL,
  team_id    INTEGER NOT NULL REFERENCES teams(id),
  source     TEXT,          -- 如 macau / crown / excel / api-football / manual
  PRIMARY KEY (alias, source)  -- TODO: 是否全局 alias 唯一待定；若全局唯一可改 UNIQUE(alias)
);

-- 联赛 / 赛事
CREATE TABLE IF NOT EXISTS leagues (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  name_zh_canonical  TEXT NOT NULL UNIQUE,
  created_at         TEXT NOT NULL DEFAULT (datetime('now')),
  note               TEXT
);

CREATE TABLE IF NOT EXISTS league_aliases (
  alias      TEXT NOT NULL,
  league_id  INTEGER NOT NULL REFERENCES leagues(id),
  source     TEXT,
  PRIMARY KEY (alias, source)
);
```

**与现有 `matches` 的关系（过渡）**

- 现状：`matches.home_team` / `away_team` / `competition_name` 为文本。
- 目标：写入时解析为 `home_team_id` / `away_team_id` / `league_id`，文本列可保留规范名冗余便于列表 —— **TODO：migration 与导入钩子**。

### 2.3 消息侧

- 预测通知消息中的主客队 / 联赛：**只用规范中文名**。
- 别名仅用于匹配入库，不出现在用户可见文案（除非调试模式 —— 默认关闭）。

---

## 3. 遗留手工数据字符清理清单

导入或回填前建议跑检查（脚本 **TODO**）。发现即修，精确度优先。

| 类别 | 典型问题 | 处理建议 |
|---|---|---|
| 全角标点 | `，` `。` `：` `（）` `－` 混入队名/联赛 | 规范名内改为半角或去掉；别名可保留原样映射 |
| 空白 | 首尾空格、连续空格、不间断空格 `\u00a0` | trim + 折叠空格 |
| 错别字 / 旧译名 | 同一队多种中文 | 选规范名，其余进 `team_aliases` |
| 简繁混用 | 同一实体简繁各一 | 统一为规范简体（若数据集已是简体） |
| 英文 / 缩写混入 | `Man Utd` 与「曼联」并存 | 英文作 alias，消息用中文规范名 |
| 全角数字 | `０１２` | 转半角 |
| Excel 伪数字 | 队名被当成数字或日期 | 强制文本；修复已坏单元格 |
| 不可见字符 | BOM、零宽字符 | 剥离 |
| 联赛别名 | 「英超」/「英格兰超级联赛」等 | `league_aliases` |
| 开赛时间 | 仅整点、缺日期、时区不明 | 补 `kickoff_at` + `kickoff_minute_known=false`；竞彩日按仓库规则重算 |
| JSON 键 | 手工改键名、缺五段 | 对照 `monthly-json-schema.md` 五段：`match/result/stats/odds/meta` |

清理原则：**不静默丢场**；无法自动修的写入 `gap` / `needs_review` 清单人工处理。

---

## 4. 预测入库与消息

### 4.1 必须入库

- **每一个策略版本**、每一次对某场某玩法的预测，写入 DB（亚盘可走既有 `predictions`；多玩法走 `prediction_legs`）。
- 唯一性：`prediction_legs` 已约定 `UNIQUE (match_id, market, strategy)`；同策略覆盖写入或版本化 —— **版本字符串是否含 git/tag TODO**。
- 字段要点（见 `v1_1_prediction_legs.sql`）：`market`（`ah|ou|1x2|jc_hhad`）、`side`、`line`、`stake`（份，可空）、`settle_book`（默认 `macau_close`）、`rationale_json`、`gap_json`（缺字段禁止硬编）、`produced_at`。
- 发消息**不替代**入库；先入库再派单（或同事务）。便于回测对比。

### 4.2 日用消息范围（当前）

- 仅 **亚盘方向 + 简化份数**（`|s|=5→2`，`|s|=3→1`；细则见路线图 §5 / 调研 §6）。
- OU / 1X2 / 竞彩让球：数据与验证不足时可只入库、不进消息。

### 4.3 消息格式提醒

- 渠道：通知维护者；固定格式（逐行模板 **TODO：分析师定稿**）。
- 队名 / 联赛：规范中文名。
- 建议至少可追溯：`match_uid` 或竞彩编号、策略名、市场、方向、份数、结算口径。
- 临盘窗口：约开赛前 1h；`/dispatch/pending` 为近似待发查询（见 OpenAPI）；凌晨场按仓库时间窗 —— **TODO：与仓库 cutoff 一句对齐后写入消息 SOP**。

### 4.4 注额与本金（数据侧）

- `stake` 存**份**，不写死金额；1 份对应金额由资金配置 / 计算器按本金算出。
- 统计用固定初始本金；用户可调本金仅影响计算器与建议金额，不篡改历史「份」记录（除非显式重算并写新版本行）—— **TODO：是否保留重算审计表**。

---

## 5. 与 JSON 五段的兼容

月度 JSON 顶层仍为数组元素五段：`match` / `result` / `stats` / `odds` / `meta`。

- 扩展字段（`scope`、`kickoff_at`、机构字典等）走扩展层，**不删旧键**（见 `daily-match-scope-and-json-v2.md`）。
- 分析工具权威输入：导入的 `YYMM.json` + 方案产物；`$ODDS_DATA_DIR/` 各 API 源用于补数 / 核验，不另起一套主清单字段（见 `backend-build-eval-20261005.md（未公开）`）。

---

## 6. TODO 清单（本约定文档）

1. `matches` 增加 `kickoff_at`、`kickoff_minute_known`（及可选 team/league id）的 SQL migration。  
2. `teams` / `team_aliases` / `leagues` / `league_aliases` 正式 DDL 与 alias 全局唯一策略。  
3. 字符清理脚本与 `needs_review` 输出格式。  
4. 竞彩日 0–10 边界与仓库脚本逐行对照确认。  
5. 预测消息固定格式模板。  
6. 策略版本字符串规范（是否含日期 / git）。  
7. 注额重算审计表是否需要。

---

## 7. 修订记录

| 日期 | 说明 |
|---|---|
| 2026-10-06 | v1 初稿：开赛到分钟、竞彩日与 YYMM、队名映射、字符清理、预测入库 |
