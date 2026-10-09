-- 分析工具 v1 · SQLite 表结构草案
-- 对齐 作者的私有分析仓 月度 JSON 五段：match / result / stats / odds / meta
-- 主清单：scope=jingcai；可从 00_数据集/YYYY/YYMM.json 导入；可回滚（schema_migrations）

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT PRIMARY KEY,
  applied_at  TEXT NOT NULL DEFAULT (datetime('now')),
  note        TEXT
);

CREATE TABLE IF NOT EXISTS import_batches (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source_file TEXT NOT NULL,          -- meta.source_file
  month       TEXT,                   -- meta.month，如 2607
  imported_at TEXT NOT NULL DEFAULT (datetime('now')),
  row_count   INTEGER,
  status      TEXT NOT NULL DEFAULT 'ok'  -- ok | rolled_back
);

-- ---------- match ----------
CREATE TABLE IF NOT EXISTS matches (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  match_uid       TEXT NOT NULL UNIQUE,  -- 稳定键：jingcai_date|jc_id 或外部生成
  scope           TEXT NOT NULL DEFAULT 'jingcai',  -- jingcai | extra
  jingcai_date    TEXT NOT NULL,         -- match.date（竞彩日，非自然日）
  weekday         TEXT,
  kickoff_hour    INTEGER,               -- 0–23；排序用 jc_day_hour_order 映射
  jc_id           TEXT,                  -- 如 "日004"
  jc_no           INTEGER,               -- 从 jc_id 解析的序号，便于排序
  competition_name TEXT,
  competition_type TEXT,
  competition_stage TEXT,
  home_team       TEXT NOT NULL,
  away_team       TEXT NOT NULL,
  import_batch_id INTEGER REFERENCES import_batches(id),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_matches_list
  ON matches (scope, jingcai_date, kickoff_hour, jc_no);

CREATE INDEX IF NOT EXISTS idx_matches_jc
  ON matches (jc_id) WHERE jc_id IS NOT NULL;

-- ---------- result（无赛果时整行可缺或字段为 NULL）----------
CREATE TABLE IF NOT EXISTS results (
  match_id     INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
  home_goals   INTEGER,
  away_goals   INTEGER,
  total_goals  INTEGER,
  wdl          TEXT                  -- 胜/平/负
);

-- ---------- stats（嵌套结构 v1 用 JSON 存，避免过早打散）----------
CREATE TABLE IF NOT EXISTS stats (
  match_id         INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
  recent_json      TEXT,   -- last10 / last6 主客 gf/ga 等
  home_streak_last6 REAL,
  h2h_json         TEXT,
  rank_home        INTEGER,
  rank_away        INTEGER,
  popularity_diff  REAL,
  injury_json      TEXT,
  weather_json     TEXT,
  extras_json      TEXT    -- 预留扩展
);

-- ---------- odds：亚盘（兼容旧键 macau/crown/william）----------
-- book: macau | crown | william |（可扩展 pinnacle 等）
-- phase: open | mid | close
CREATE TABLE IF NOT EXISTS odds_asian (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id    INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  book        TEXT NOT NULL,
  phase       TEXT NOT NULL,
  handicap    REAL,           -- 主队视角：主让为正、主受让为负，0.25 离散
  home_water  REAL,           -- 澳门 open/close 仓库里可能无水位 → NULL
  away_water  REAL,
  UNIQUE (match_id, book, phase)
);

CREATE INDEX IF NOT EXISTS idx_odds_asian_match ON odds_asian (match_id);

-- 欧赔主胜（仓库口径：只有主胜，非完整 1X2）
CREATE TABLE IF NOT EXISTS odds_euro_home (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id    INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  book        TEXT NOT NULL,   -- macau | william | ...
  phase       TEXT NOT NULL,   -- open | close
  home_win    REAL,
  UNIQUE (match_id, book, phase)
);

-- 竞彩主胜
CREATE TABLE IF NOT EXISTS odds_jc_home (
  match_id    INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  phase       TEXT NOT NULL,   -- open | close
  home_win    REAL,
  PRIMARY KEY (match_id, phase)
);

-- 竞彩让球胜平负（HHAD）预留；缺数时也可只靠 odds_raw
CREATE TABLE IF NOT EXISTS odds_jc_hhad (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id    INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  phase       TEXT NOT NULL,   -- open | mid | close
  goal_line   REAL,            -- 竞彩让球线
  home_odds   REAL,            -- 胜
  draw_odds   REAL,            -- 平
  away_odds   REAL,            -- 负
  UNIQUE (match_id, phase)
);

-- 原始 odds 整段备份（导入可逆、对照仓库 JSON）
CREATE TABLE IF NOT EXISTS odds_raw (
  match_id    INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
  odds_json   TEXT NOT NULL
);

-- ---------- meta（按场；批次级另见 import_batches）----------
CREATE TABLE IF NOT EXISTS match_meta (
  match_id     INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
  source_file  TEXT,
  month        TEXT,
  extras_json  TEXT
);

-- ---------- 预测结论（协作中已定字段名）----------
CREATE TABLE IF NOT EXISTS predictions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  strategy      TEXT NOT NULL,          -- 如 CFFXDJ_5_V3
  direction     TEXT NOT NULL,          -- 主 | 客 | 不下注
  settle_book   TEXT NOT NULL DEFAULT 'macau_close',
  rationale_json TEXT,                  -- rationale[] JSON 数组
  confidence    REAL,                   -- 可选
  produced_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (match_id, strategy)
);

CREATE INDEX IF NOT EXISTS idx_predictions_match ON predictions (match_id);

-- 种子迁移标记
INSERT OR IGNORE INTO schema_migrations (version, note)
VALUES ('v1', '初始：五段兼容 + jingcai|extra + predictions + odds_jc_hhad 预留');

-- ---------- prediction_legs（v1.1 多玩法；旧 predictions 不动）----------
CREATE TABLE IF NOT EXISTS prediction_legs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id       INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  market         TEXT NOT NULL,           -- ah | ou | 1x2 | jc_hhad
  side           TEXT,
  line           REAL,
  line_text      TEXT,
  stake          REAL,
  strategy       TEXT,
  settle_book    TEXT NOT NULL DEFAULT 'macau_close',
  rationale_json TEXT,
  confidence     REAL,
  gap_json       TEXT,
  produced_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (match_id, market, strategy)
);

CREATE INDEX IF NOT EXISTS idx_prediction_legs_match
  ON prediction_legs (match_id);

CREATE INDEX IF NOT EXISTS idx_prediction_legs_market
  ON prediction_legs (market);

INSERT OR IGNORE INTO schema_migrations (version, note)
VALUES (
  'v1.1',
  'prediction_legs 多玩法腿 + dispatch 待发查询；旧 predictions 不动'
);
