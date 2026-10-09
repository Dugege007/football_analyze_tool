-- v1.2 · 正式 migration（2026-10-06）
-- 范围：teams/leagues + 别名表；matches.kickoff_at 到分钟 + 可选队/联赛 id；
--       predictions / prediction_legs 注额（份）字段；bankroll_config + bankroll_snapshots
-- 对齐：data-conventions-v1.md §1.2 / §2.2 / §4.4；analysis-tool-roadmap-20261006.md §5
-- 拍板（已关闭，勿再开）：
--   1. team_aliases / league_aliases：表内 UNIQUE(alias) 全局唯一；source 仅溯源列，不进主键
--   2. matches 过渡：继续存文本 home_team/away_team/competition_name（可冗余规范名）；*_id 可空逐步填
--   3. v1.2 不建注额重算审计表；重算记 bankroll_snapshots + 预测行 produced_at / updated_at
--   4. 统计本金默认（2026-10-06 补拍板）：stats_initial_bankroll.amount = 10000 CNY；
--      单注最低 50；单注最高 ≤ 当前剩余资金 50%（stake_amount_limits）
-- 实体表：**不用**统一 entities 表；teams / leagues 分表为准
-- 原则：旧 predictions（CFFXDJ_5_V3）/ v1.1 prediction_legs 数据不动；不改默认策略
--
-- 幂等说明：
--   CREATE ... IF NOT EXISTS / INSERT OR IGNORE 可重复执行；
--   SQLite 不支持 ADD COLUMN IF NOT EXISTS —— 请用
--   api/scripts/migrate_v1_2.py 应用（逐句执行，忽略 duplicate column）。
--   直接 sqlite3 < 本文件 只适用于从未应用过 v1.2 的库。

PRAGMA foreign_keys = ON;

-- ---------- 1) 队 / 联赛规范名 + 别名 ----------
CREATE TABLE IF NOT EXISTS teams (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  name_zh_canonical  TEXT NOT NULL UNIQUE,      -- 中文规范名；消息只用它
  created_at         TEXT NOT NULL DEFAULT (datetime('now')),
  note               TEXT
);

CREATE TABLE IF NOT EXISTS team_aliases (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  alias      TEXT NOT NULL UNIQUE,              -- 拍板 1：全局唯一（多源同名只能指向同一队）
  team_id    INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  source     TEXT,                              -- 溯源：macau / crown / excel / api-football / manual …
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_team_aliases_team ON team_aliases (team_id);

CREATE TABLE IF NOT EXISTS leagues (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  name_zh_canonical  TEXT NOT NULL UNIQUE,
  created_at         TEXT NOT NULL DEFAULT (datetime('now')),
  note               TEXT
);

CREATE TABLE IF NOT EXISTS league_aliases (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  alias      TEXT NOT NULL UNIQUE,              -- 拍板 1：全局唯一
  league_id  INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
  source     TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_league_aliases_league ON league_aliases (league_id);

-- ---------- 2) matches：kickoff 到分钟 + 可选队/联赛 id ----------
-- kickoff_hour 保留（jc_day_hour_order 排序 / 旧脚本兼容）
-- kickoff_at：ISO-8601 +08:00，到分钟；kickoff_minute_known：1=分钟可信，0=仅整点占位
ALTER TABLE matches ADD COLUMN kickoff_at TEXT;
ALTER TABLE matches ADD COLUMN kickoff_minute_known INTEGER NOT NULL DEFAULT 0;
ALTER TABLE matches ADD COLUMN home_team_id INTEGER REFERENCES teams(id);
ALTER TABLE matches ADD COLUMN away_team_id INTEGER REFERENCES teams(id);
ALTER TABLE matches ADD COLUMN league_id INTEGER REFERENCES leagues(id);

CREATE INDEX IF NOT EXISTS idx_matches_kickoff_at
  ON matches (kickoff_at) WHERE kickoff_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_matches_home_team_id ON matches (home_team_id);
CREATE INDEX IF NOT EXISTS idx_matches_away_team_id ON matches (away_team_id);
CREATE INDEX IF NOT EXISTS idx_matches_league_id ON matches (league_id);

-- 回填 kickoff_at（只填空值，不覆盖）：
--   优先 match_meta.extras_json.kickoff_at（视为分钟可信，minute_known=1）；
--   否则 jingcai_date + kickoff_hour 合成整点（竞彩日规则：0–10 点 → 次日），minute_known=0
UPDATE matches
SET kickoff_minute_known = CASE
      WHEN (SELECT json_extract(mm.extras_json, '$.kickoff_at')
            FROM match_meta mm WHERE mm.match_id = matches.id) IS NOT NULL THEN 1
      ELSE 0 END,
    kickoff_at = COALESCE(
      (SELECT json_extract(mm.extras_json, '$.kickoff_at')
       FROM match_meta mm WHERE mm.match_id = matches.id),
      CASE WHEN kickoff_hour BETWEEN 0 AND 23 THEN
        (CASE WHEN kickoff_hour <= 10 THEN date(jingcai_date, '+1 day') ELSE date(jingcai_date) END)
        || 'T' || printf('%02d', kickoff_hour) || ':00:00+08:00'
      END)
WHERE kickoff_at IS NULL;

-- ---------- 3) predictions：全方案入库 + 份 / 派单痕迹 ----------
-- 唯一键仍 UNIQUE(match_id, strategy)；每个策略版本一行
-- stake：整数「份」，可空（=只给方向/观察）；金额只在 /bankroll/calc 计算，不入库
ALTER TABLE predictions ADD COLUMN stake INTEGER CHECK (stake IS NULL OR stake >= 0);
ALTER TABLE predictions ADD COLUMN stake_rule TEXT;        -- 如 simplified_s5_2_s3_1 / kelly25_pL80_cap3
ALTER TABLE predictions ADD COLUMN message_sent_at TEXT;   -- 通知派单时间（入库≠已发）
ALTER TABLE predictions ADD COLUMN updated_at TEXT;        -- 重算/改份时写；NULL=未改过

CREATE INDEX IF NOT EXISTS idx_predictions_strategy
  ON predictions (strategy, produced_at);

-- ---------- 4) prediction_legs：注额审计 + 影子状态 ----------
-- 注：legs.stake 列在 v1.1 为 REAL（SQLite 无法改列类型）；语义按整数「份」，API 层校验/输出 int
ALTER TABLE prediction_legs ADD COLUMN stake_rule TEXT;
ALTER TABLE prediction_legs ADD COLUMN p_used REAL;
ALTER TABLE prediction_legs ADD COLUMN f_star REAL;
ALTER TABLE prediction_legs ADD COLUMN status TEXT NOT NULL DEFAULT 'active';  -- active|shadow|paper|pilot|prod|retired
ALTER TABLE prediction_legs ADD COLUMN updated_at TEXT;

CREATE INDEX IF NOT EXISTS idx_prediction_legs_strategy
  ON prediction_legs (strategy, market, produced_at);

-- ---------- 5) bankroll ----------
CREATE TABLE IF NOT EXISTS bankroll_config (
  key         TEXT PRIMARY KEY,
  value_json  TEXT NOT NULL,                     -- JSON 对象
  updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
  note        TEXT
);

CREATE TABLE IF NOT EXISTS bankroll_snapshots (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  reported_at  TEXT NOT NULL,                    -- ISO-8601 +08:00
  balance      REAL NOT NULL CHECK (balance >= 0),
  source       TEXT NOT NULL DEFAULT 'user_report',
  note         TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_bankroll_snapshots_at
  ON bankroll_snapshots (reported_at);

-- 种子键（INSERT OR IGNORE：重复执行不覆盖已改值）
-- 拍板 4（补）：统计本金 10000 CNY；周一基数 / 计算器本金仍 null（运行时由 snapshot / 用户设置）
INSERT OR IGNORE INTO bankroll_config (key, value_json, note) VALUES
  ('stats_initial_bankroll', '{"amount": 10000, "currency": "CNY"}',
   '统计用固定初始本金（拍板 2026-10-06：10000 CNY）'),
  ('monday_base_bankroll',   '{"amount": null, "as_of": null, "snapshot_id": null}',
   '周一资金基数 B_week；1 份 = amount × unit_fraction'),
  ('calculator_bankroll',    '{"amount": null}',
   '注额计算器默认本金（用户自设，可空）'),
  ('unit_definition',        '{"unit_fraction": 0.005, "base_key": "monday_base_bankroll"}',
   '1 份 = 周一基数 × 0.5%'),
  ('stake_caps',             '{"per_match": 3, "per_day": 10, "mode": "clamp"}',
   '单场≤3 份、单竞彩日≤10 份；clamp=截断并标 capped，warn=只告警不改'),
  ('stake_amount_limits',    '{"min_stake_amount": 50, "max_stake_pct_of_remaining": 0.5, "currency": "CNY"}',
   '单注金额下限 50（不足抬到 50，标 raised）；单注上限 ≤ 当前剩余资金 50%（标 capped_amount）');

-- 早先已应用 v1.2（stats 本金占位 amount=null）的库：仅在仍为 null 时补 10000，不覆盖人工改值
UPDATE bankroll_config
SET value_json = '{"amount": 10000, "currency": "CNY"}',
    updated_at = datetime('now'),
    note = '统计用固定初始本金（拍板 2026-10-06：10000 CNY）'
WHERE key = 'stats_initial_bankroll'
  AND json_extract(value_json, '$.amount') IS NULL;

INSERT OR IGNORE INTO schema_migrations (version, note)
VALUES (
  'v1.2',
  'teams/leagues+aliases(UNIQUE alias)；matches.kickoff_at+minute_known+*_id；predictions/legs 份字段；bankroll_config/snapshots（统计本金 10000 CNY；单注 50 ~ 剩余 50%）'
);
