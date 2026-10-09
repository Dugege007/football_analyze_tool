-- ========== v2.0 odds timeline (D1) ==========
-- book: macau|crown|william|pinnacle|bet365|jc|betfair
-- market: asian|ou|euro_1x2|jc_spf|jc_hhad
-- channel: rule | rule_legacy | actual
-- point: open|mid|close|t8|t1|api_opening|api_closing
-- 幂等：CREATE IF NOT EXISTS + INSERT OR IGNORE schema_migrations

CREATE TABLE IF NOT EXISTS odds_timeline_seg (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  book          TEXT NOT NULL,
  market        TEXT NOT NULL,
  seg_start_at  TEXT NOT NULL,
  seg_end_at    TEXT NOT NULL,
  line          REAL,
  price_home    REAL,
  price_away    REAL,
  price_draw    REAL,
  price_over    REAL,
  price_under   REAL,
  water_home    REAL,
  water_away    REAL,
  water_over    REAL,
  water_under   REAL,
  tick_count    INTEGER NOT NULL DEFAULT 1,
  compression   TEXT NOT NULL DEFAULT 'change_point',
  is_inplay     INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT '5dollar_history',
  water_src     TEXT,
  extras_json   TEXT,
  UNIQUE (match_id, book, market, seg_start_at, compression)
);

CREATE INDEX IF NOT EXISTS idx_otl_match_book_mkt
  ON odds_timeline_seg (match_id, book, market, seg_start_at);

CREATE TABLE IF NOT EXISTS odds_snapshot (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  book          TEXT NOT NULL,
  market        TEXT NOT NULL,
  channel       TEXT NOT NULL,
  point         TEXT NOT NULL,
  recorded_at   TEXT,
  target_at     TEXT,
  lag_hours     REAL,
  stale_gap     INTEGER NOT NULL DEFAULT 0,
  line          REAL,
  price_home    REAL,
  price_away    REAL,
  price_draw    REAL,
  price_over    REAL,
  price_under   REAL,
  water_home    REAL,
  water_away    REAL,
  water_over    REAL,
  water_under   REAL,
  water_src     TEXT,
  water_censored INTEGER,
  source        TEXT,
  extras_json   TEXT,
  UNIQUE (match_id, book, market, channel, point)
);

CREATE INDEX IF NOT EXISTS idx_osnap_match_ch
  ON odds_snapshot (match_id, channel, point);

CREATE TABLE IF NOT EXISTS odds_fetch_blob (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      INTEGER REFERENCES matches(id) ON DELETE SET NULL,
  fixture_id    TEXT,
  kind          TEXT NOT NULL,
  book          TEXT,
  market        TEXT,
  path          TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  bytes_raw     INTEGER,
  bytes_gz      INTEGER,
  fetched_at    TEXT NOT NULL,
  http_status   INTEGER,
  UNIQUE (path)
);

CREATE TABLE IF NOT EXISTS odds_fetch_queue (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      INTEGER,
  fixture_id    TEXT,
  task          TEXT NOT NULL,
  priority      INTEGER NOT NULL,
  not_before    TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  checkpoint_json TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ofq_poll
  ON odds_fetch_queue (status, priority, not_before);

CREATE VIEW IF NOT EXISTS v_odds_asian_rule AS
SELECT
  match_id,
  book,
  CASE point WHEN 'open' THEN 'open' WHEN 'mid' THEN 'mid' WHEN 't8' THEN 'mid'
             WHEN 'close' THEN 'close' WHEN 't1' THEN 'close' ELSE point END AS phase,
  line AS handicap,
  water_home AS home_water,
  water_away AS away_water,
  water_src,
  water_censored,
  channel,
  point,
  recorded_at,
  stale_gap
FROM odds_snapshot
WHERE market = 'asian' AND channel = 'rule'
  AND point IN ('open','mid','close','t8','t1');

INSERT OR IGNORE INTO schema_migrations (version, note)
VALUES ('v2.0', 'odds_timeline_seg + odds_snapshot(channel=rule|rule_legacy|actual) + odds_fetch_blob/queue；D1 探针导入');
