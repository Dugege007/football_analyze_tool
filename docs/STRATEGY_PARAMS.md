# 策略参数（config/strategy_params.json）

公开仓**只公开方法和代码框架，不公开调好的参数**。凡是“调参得到、验证过有效”的数值（阈值、窗口、半衰期、最小样本量、边际阈值等），都已从代码里移到 `config/strategy_params.json`，公开仓只提供全为 `null` 的模板 `config/strategy_params.example.json`。

## 怎么用

1. 复制模板：`cp config/strategy_params.example.json config/strategy_params.json`（该文件已在 `.gitignore`，不会被提交）。
2. 用你自己的数据回测 / 验证后，把各项 `null` 换成你的数值。
3. 也可以用环境变量 `STRATEGY_PARAMS_PATH` 指向任意位置的参数文件。

查找顺序：`STRATEGY_PARAMS_PATH` → `config/strategy_params.json` → `config/strategy_params.example.json`。

## 没填参数时会怎样

- API、采集、补救、库表等框架功能**不依赖**这些参数，照常运行。
- 只有用到对应策略时（例如生成影子预测、拟合 Dixon-Coles 模型），代码会抛出 `StrategyParamsMissing`，并提示缺哪一项。它不会悄悄用某个默认值跑出结果。
- 测试里依赖真实参数的用例会被 **skip**，原因写明 "strategy params not configured"。

## 参数分组

| 分组 | 用在哪里 | 键 |
|---|---|---|
| S1 | `api/scripts/generate_shadow_predictions.py`、`generate_shadow_s1_v2.py` | `line_dev_max`、`upper_water_lo`、`upper_water_hi` |
| S2 | `generate_shadow_predictions.py` | `dev_abs`、`close_upper_water_min` |
| N1 | `generate_shadow_predictions.py` | `home_mid_open_lo`、`home_mid_open_hi`、`home_high_close` |
| N4 | `generate_shadow_predictions.py` | `dev_abs`、`open_upper_water_min` |
| S8 | `generate_shadow_predictions.py` | `upper_water_change_max`、`euro_flat_rel`、`jc_flat_rel`、`level_rank_diff_max` |
| N5 | `scripts/model/dc_model.py`、`api/app/shadow_evaluable.py` | `window_days`、`half_life_days`、`min_team_n`、`min_league_n`、`min_team_wn`、`min_league_wn`、`edge_delta` |

每个键的含义见对应脚本里的注释和 `docs/schema/` 下的方法论文档。文档里出现的具体数字已被替换为“…”。
