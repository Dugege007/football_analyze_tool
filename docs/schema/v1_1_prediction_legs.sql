-- v1.1 · 多玩法预测腿（不拆坏旧 predictions 亚盘方向）
-- market: ah | ou | 1x2 | jc_hhad
-- 缺盘口字段写入 gap_json，禁止硬编

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS prediction_legs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id       INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  market         TEXT NOT NULL,           -- ah | ou | 1x2 | jc_hhad
  side           TEXT,                   -- 主/客/大/小/胜/平/负/不下注；未知可 NULL
  line           REAL,                   -- 亚盘/大小/竞彩让球数值线
  line_text      TEXT,                   -- 可选字符串形态，如竞彩 "-1"
  stake          REAL,                   -- 整数「份」（应用层按 int）；可空=不推荐；1份本金由资金表另定，不写死金额
  strategy       TEXT,                   -- 可空；有则参与唯一键
  settle_book    TEXT NOT NULL DEFAULT 'macau_close',
  rationale_json TEXT,                   -- rationale[] JSON
  confidence     REAL,
  gap_json       TEXT,                   -- 缺字段标记，不硬编
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
