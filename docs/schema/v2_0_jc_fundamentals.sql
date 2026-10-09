-- v2.0 竞彩完整盘 + 基本面观测（副本 v2d3；DUAL_WRITE 关；不写现网）
-- 幂等：CREATE IF NOT EXISTS；ADD COLUMN 由迁移脚本按列探测（或 apply_sql_idempotent 吞 duplicate）
-- 日期：2026-10-08
-- 依据：v2_0-jc-odds-capture-schema-draft.md / v2_0-fundamentals-ingest-schema-draft.md

-- A) 竞彩胜平负完整表（旧 odds_jc_home 冻结只读，不扩写）
CREATE TABLE IF NOT EXISTS odds_jc_had (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id        INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  phase           TEXT NOT NULL,
  home_odds       REAL,
  draw_odds       REAL,
  away_odds       REAL,
  jc_1x2_incomplete INTEGER NOT NULL DEFAULT 0,
  source          TEXT NOT NULL,
  captured_at     TEXT,
  target_at       TEXT,
  usable_at_mid   INTEGER,
  usable_at_close INTEGER,
  sporttery_match_id TEXT,
  update_date     TEXT,
  update_time     TEXT,
  extras_json     TEXT,
  UNIQUE (match_id, phase, source)
);

CREATE INDEX IF NOT EXISTS idx_jc_had_match_phase
  ON odds_jc_had (match_id, phase);

-- A) 让球胜平负元数据列（主表 UNIQUE(match_id, phase) 仍表示当前有效线）
ALTER TABLE odds_jc_hhad ADD COLUMN source TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN captured_at TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN target_at TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN usable_at_mid INTEGER;
ALTER TABLE odds_jc_hhad ADD COLUMN usable_at_close INTEGER;
ALTER TABLE odds_jc_hhad ADD COLUMN jc_1x2_incomplete INTEGER NOT NULL DEFAULT 0;
ALTER TABLE odds_jc_hhad ADD COLUMN goal_line_raw TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN sporttery_match_id TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN update_date TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN update_time TEXT;
ALTER TABLE odds_jc_hhad ADD COLUMN line_rev INTEGER NOT NULL DEFAULT 0;
ALTER TABLE odds_jc_hhad ADD COLUMN extras_json TEXT;

-- A) 让球换盘历史（旧线进 hist；主表一行当前线）
CREATE TABLE IF NOT EXISTS odds_jc_hhad_line_hist (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id    INTEGER NOT NULL,
  phase       TEXT NOT NULL,
  goal_line   REAL,
  home_odds   REAL,
  draw_odds   REAL,
  away_odds   REAL,
  captured_at TEXT,
  superseded_at TEXT NOT NULL,
  source      TEXT,
  extras_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_jc_hhad_hist_match_phase
  ON odds_jc_hhad_line_hist (match_id, phase);

-- B) 基本面观测表
CREATE TABLE IF NOT EXISTS stats_obs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,
  as_of         TEXT NOT NULL,
  source        TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  provider_ref  TEXT,
  fetched_at    TEXT,
  quality       TEXT,
  extras_json   TEXT,
  UNIQUE (match_id, kind, as_of, source)
);

CREATE INDEX IF NOT EXISTS idx_stats_obs_match_kind_asof
  ON stats_obs (match_id, kind, as_of);
CREATE INDEX IF NOT EXISTS idx_stats_obs_source
  ON stats_obs (source, kind);

-- B) stats 投影出处列
ALTER TABLE stats ADD COLUMN source_recent TEXT;
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
ALTER TABLE stats ADD COLUMN extras_json_meta TEXT;
