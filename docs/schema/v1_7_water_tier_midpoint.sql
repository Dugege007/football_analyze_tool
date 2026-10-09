-- v1.7 · crown/william 水位档位 → 中点水位（DDL 部分；可重复执行）
-- 数据换算由 scripts/migrate_v1_7_water.py 完成（只换 water_src IS NULL 的行 → 幂等，不重复换算）。
-- 口径：w = 0.70 + 0.05 × t；t=0 → ≤0.70、t=10 → ≥1.20 为截断值（water_censored=1）。
-- 原始档位保存在 extras_json.water_tier_raw，可回退（migrate_v1_7_water.py --rollback）。

ALTER TABLE odds_asian ADD COLUMN water_src TEXT;        -- tier_midpoint | actual | NULL(macau 无水位/未知)
ALTER TABLE odds_asian ADD COLUMN water_censored INTEGER; -- 1=任一侧为截断值；0=否；NULL=未换算
ALTER TABLE odds_asian ADD COLUMN extras_json TEXT;       -- {"water_tier_raw":{"home","away"},"censored":{"home","away"},"conversion"}

INSERT OR IGNORE INTO schema_migrations (version, note)
VALUES ('v1.7', 'odds_asian: water_src / water_censored / extras_json；crown/william 档位→中点水位 w=0.70+0.05t');
