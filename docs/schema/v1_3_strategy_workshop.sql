-- v1.3 · 方案工场 M2：方案定义 + 验证缓存（配置驱动、版本化；不硬编码玩法）
-- 对齐路线图方案工场 M2；正式 migration（CREATE IF NOT EXISTS，可重复执行）
-- 原则：不拆坏 predictions / prediction_legs / bankroll；CFFXDJ_5_V3 默认不变
-- 验证 run 允许纯影子（只写 cache，不必先绑 predictions）；可选 linked_prediction_batch 放 params_json

PRAGMA foreign_keys = ON;

-- 方案定义（一条版本一行；改规则 = 新 version 行）
CREATE TABLE IF NOT EXISTS strategy_defs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_key    TEXT NOT NULL,          -- 如 CFFXDJ_5_V3；与 predictions.strategy 对齐
  version         TEXT NOT NULL,          -- 语义化或日期标签，如 2026.10.06
  display_name    TEXT,
  markets_json    TEXT NOT NULL DEFAULT '["ah"]',  -- 可扩展 ah|ou|1x2|jc_hhad|…
  config_json     TEXT NOT NULL DEFAULT '{}',      -- 闸门/投票/结算等；未知键进 extras，表结构不改
  config_fingerprint TEXT,                         -- config_json 规范化后的哈希，便于去重/缓存命中
  status          TEXT NOT NULL DEFAULT 'draft',   -- draft|active|shadow|archived
  is_default      INTEGER NOT NULL DEFAULT 0,      -- 仅展示默认；写入不改仓库默认语义
  notes           TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (strategy_key, version)
);

CREATE INDEX IF NOT EXISTS idx_strategy_defs_status
  ON strategy_defs (status, strategy_key);

-- 验证运行批次（一次回测/影子验证；可不绑入库 predictions）
CREATE TABLE IF NOT EXISTS strategy_validation_runs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_def_id INTEGER NOT NULL REFERENCES strategy_defs(id) ON DELETE CASCADE,
  run_label       TEXT,                   -- 人工标签
  scope           TEXT NOT NULL DEFAULT 'jingcai',
  settle_book     TEXT NOT NULL DEFAULT 'macau_close',
  params_json     TEXT NOT NULL DEFAULT '{}',  -- 窗口、过滤；可选 linked_prediction_batch
  run_fingerprint TEXT,                   -- strategy_def指纹+params+settle+scope 合成；同指纹可复用 cache
  started_at      TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at     TEXT,
  status          TEXT NOT NULL DEFAULT 'running', -- running|ok|failed
  summary_json    TEXT,                   -- ROI/笔数/回撤等摘要
  error_text      TEXT
);

CREATE INDEX IF NOT EXISTS idx_strategy_validation_runs_def
  ON strategy_validation_runs (strategy_def_id, started_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_validation_runs_fp
  ON strategy_validation_runs (run_fingerprint)
  WHERE run_fingerprint IS NOT NULL;

-- 验证缓存：按场×玩法落指标，便于对比折线（M4）复用；影子 run 只进本表即可
CREATE TABLE IF NOT EXISTS strategy_validation_cache (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          INTEGER NOT NULL REFERENCES strategy_validation_runs(id) ON DELETE CASCADE,
  match_id        INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  market          TEXT NOT NULL,          -- 不枚举死；与 prediction_legs.market 同约定
  side            TEXT,
  line            REAL,
  stake_units     REAL,                   -- 份；可空
  result_code     TEXT,                   -- win|win_half|push|lose_half|lose|skip|…
  pnl_units       REAL,                   -- 以「份」计盈亏
  metrics_json    TEXT,                   -- CLV、公平概率等扩展
  row_fingerprint TEXT,                   -- match+market+side+line+settle 输入指纹（可选审计）
  UNIQUE (run_id, match_id, market)
);

CREATE INDEX IF NOT EXISTS idx_strategy_validation_cache_run
  ON strategy_validation_cache (run_id);
CREATE INDEX IF NOT EXISTS idx_strategy_validation_cache_match
  ON strategy_validation_cache (match_id);

INSERT OR IGNORE INTO schema_migrations (version, note)
VALUES (
  'v1.3',
  '方案工场 M2：strategy_defs + validation_runs/cache（指纹列；影子 run 可只写 cache）'
);
