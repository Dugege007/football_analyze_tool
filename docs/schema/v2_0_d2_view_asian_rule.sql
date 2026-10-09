-- D2：对照 VIEW 仅 open/mid/close（t8/t1 不投影进 odds_asian 语义）
DROP VIEW IF EXISTS v_odds_asian_rule;
CREATE VIEW v_odds_asian_rule AS
SELECT
  match_id,
  book,
  point AS phase,
  line AS handicap,
  water_home AS home_water,
  water_away AS away_water,
  water_src,
  water_censored,
  channel,
  point,
  recorded_at,
  stale_gap,
  target_at,
  source
FROM odds_snapshot
WHERE market = 'asian' AND channel = 'rule'
  AND point IN ('open', 'mid', 'close');
