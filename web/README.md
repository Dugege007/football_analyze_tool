# 网页界面（web/）

本目录是 football-analyze-tool 的网页界面，使用 Vite、React、TypeScript、Ant Design、AG Grid 与 ECharts 构建，通过本地 API（`api/`）取数。

## 页面

1. 比赛列表：按竞彩日和范围（竞彩或非竞彩）列出当日赛程。
2. 单场详情：亚洲让球盘口、交锋与近况、赛前盘口与水位折线图（默认澳门亚洲让球盘，可选择其他博彩公司，横轴为对数刻度的距开赛时间）。
3. 预测结论：方向、方案、结算依据与说明。
4. 方案对比：多个方案的回测对比与叠加曲线。
5. 方案工坊、验证、资金计算与设置页面。
6. 数据表：按日期区间批量展示各公司初盘、中盘、临盘与自动高亮。高亮规则见 `docs/schema/v2_0-data-table-highlight-rules.md`。

## 运行

先按仓库根目录 README 的「在本机运行网页界面」一节启动后端接口，然后：

```bash
cd web
npm install
npm run dev
```

- 网页界面开发服务器地址：http://127.0.0.1:5173
- 接口转发：浏览器访问的 `/api` 会被转发到 `VITE_API_BASE`（默认 http://127.0.0.1:8787），`/api-replica` 会被转发到 `VITE_API_REPLICA_BASE`（默认 http://127.0.0.1:8788）。
- 配置方法：复制 `.env.example` 为 `.env.local` 后修改。
- 后端不可用时，界面会自动回落到内置的虚构示例数据；如果只想使用真实接口，在 `.env.local` 中设置 `VITE_USE_MOCK=0`。

## 其他命令

- `npm run build`：类型检查并构建生产版本，输出到 `dist/`（不提交）。
- `npm run preview`：在 http://127.0.0.1:4173 预览构建结果（预览模式不转发接口，仅用于检查页面）。
- `npm run lint`：代码检查。
- `npm run check:licenses`：检查依赖的许可证。
