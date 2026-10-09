# 贡献指南

欢迎通过 Issue 反馈问题、讨论口径，或提交 Pull Request。

## 基本流程

1. Fork 并新建分支（如 `fix/live-capture-window`）。
2. 按 README「首次运行」配置本地环境；**只用你自己的 key 和库**。
3. 修改后运行：`cd api && python -m pytest -q`，以及 `python scripts/live/live_capture.py check-config`。
4. 提交 PR，说明改了什么、依据哪份口径文档（`docs/schema/` 下），以及验证方式。

## 必须遵守

- **绝不提交** `.env`、任何 API key / token / cookie / 密码、`*.db` / `*.sqlite*`、备份、原始历史缓存（`raw/` 等）、日志。
  提交前自查：`git diff --cached --stat`，必要时运行 `gitleaks detect --source . --no-git`。
- 路径一律通过环境变量或相对路径获取（见 `config.example.env`），不要写死本机绝对路径。
- 新增环境变量时，同步更新 `config.example.env`（逐条中文注释：用途、来源、是否必填、默认值）与 `docs/SENSITIVE.md`。
- 口径变更（盘口阶段、补救规则、防泄漏等）先改 `docs/schema/` 文档，再改代码；不得引入「决策时点之后」的信息。
- `DUAL_WRITE_ODDS_ASIAN` 默认关闭，PR 不得改变这一默认值。

## 许可证

本仓库采用 PolyForm Noncommercial License 1.0.0（见 `LICENSE`）。提交贡献即表示你同意你的贡献按同一许可证发布，并同意作者可将其包含在作者另行提供的商业授权中。
商业使用需另行取得作者授权，请通过 GitHub Issue 或作者 GitHub 主页联系。

## 参数与数据

- 不要在代码里写死调参得到的数值；新增策略参数请放进 `config/strategy_params.example.json`（值为 `null`），并在 `docs/STRATEGY_PARAMS.md` 说明。
- 不要提交由真实数据生成的文件（`config/strategy_params.json`、`api/config/kickoff_drift.json`、`api/config/leak_suspect.json`、预测结果、真实响应样例）。
