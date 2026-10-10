# 更新记录

本文件记录 football-analyze-tool 公开仓库每个版本的变化。版本号遵循语义化版本（Semantic Versioning）规则。

## v0.1.0（2026-10-10）

### 新增

- 新增网页界面目录 `web/`（Vite、React 与 TypeScript 工程），包含比赛列表、单场详情、预测结论、方案对比与多方案叠加曲线、方案工坊、验证、资金计算、设置与数据表等页面。
- 单场详情页新增赛前盘口与水位折线图：默认显示澳门亚洲让球盘，可以选择其他博彩公司，横轴为对数刻度的距开赛时间。
- 网页界面的接口地址改为可配置：新增 `web/.env.example`，可通过 `VITE_API_BASE`（默认 `http://127.0.0.1:8787`）、`VITE_API_REPLICA_BASE`（默认 `http://127.0.0.1:8788`）与 `VITE_DEV_PORT`（默认 5173）修改。
- 新增跨平台文件锁模块 `api/app/portable_lock.py`：Linux 与 macOS 使用 fcntl，Windows 使用标准库 msvcrt。
- 新增测试 `api/tests/test_portable_lock.py`，验证非阻塞加锁冲突与解锁后的再次加锁。
- 新增环境变量 `APP_CORS_ORIGINS`，用于配置后端接口允许的跨源资源共享（Cross-Origin Resource Sharing，简称 CORS）来源，说明已写入 `config.example.env`。
- 新增本文件 `CHANGELOG.md`。

### 修改

- `scripts/live/live_capture.py`、`api/scripts/build_jc_match_rows.py` 与 `api/app/shared_api_yield.py` 改用跨平台文件锁，Windows 上可以运行 `python scripts/live/live_capture.py check-config`。
- 后端接口的跨源资源共享默认从「允许任意来源并携带凭据」收紧为只允许 `http://127.0.0.1:5173` 与 `http://localhost:5173`；需要其他来源时通过 `APP_CORS_ORIGINS` 配置。
- 根目录 `README.md` 新增「在本机运行网页界面」一节（包含 Windows PowerShell 与 Cursor 终端的操作步骤），更新目录说明、环境要求与仓库地址（仓库已改名为 football-analyze-tool）。
- `.gitignore` 新增网页界面的依赖目录、构建输出与本机配置文件 `web/.env.local`。

### 未包含

- 网页界面开发过程中的截图、真实数据、调好的参数与验证成绩均未放入公开仓库。
- 后端接口版本号保持 0.3.22 不变，因为本版本没有改动任何接口的请求或响应格式。
