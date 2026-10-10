# 赛前盘口／水位折线图接口契约（按场点开才拉）

**拍板**：2026-10-09（用户：做赛前盘口／水位折线；点开某场才拉；默认澳门亚盘；库仍只存初盘／中盘／临盘等快照，不存全时序每一跳；横轴用距开赛剩余分钟的对数变换）。  
**状态**：接口契约已定；后端与前端按本文开工。  
**相关**：[`v2_0-odds-timeline-storage-design.md`](./v2_0-odds-timeline-storage-design.md)、[`v2_0-odds-phase-terminology.md`](./v2_0-odds-phase-terminology.md)、[`v2_0-hist-backfill-yield-to-live.md`](./v2_0-hist-backfill-yield-to-live.md)、[`v2_0-table-matches-api.md`](./v2_0-table-matches-api.md)、`shared_api_yield.py`。

---

## 1. 目标与硬边界

| 项 | 约定 |
|---|---|
| 用途 | 赛前约一小时（及更早）人工看盘口／水位随时间的变动，辅助赛前分析。 |
| 触发 | **仅当用户在前端点开某场**时，后端才请求 5DollarFootballAPI 的 `GET /v1/fixtures/{fixture_id}/odds/history`；禁止给全天所有场次预拉或定时刷。 |
| 默认 | `book=macauslot`（澳门）、`market=asian`（亚盘）。 |
| 前端选择 | **须提供机构下拉／选项**：至少澳门、皇冠、威廉希尔、平博、Bet365；切换后用同一 `match_id` 重新请求并带上所选 `book`（可选再选 `market`）。默认选中澳门；切换不算预拉全天场次。 |
| 存储 | 主库 **仍只存** 此前约定的初盘／中盘／临盘等快照；**禁止**把本接口返回的 ticks 写入 `odds_asian`（或等价主表）。 |
| 双写 | 与 `DUAL_WRITE` **无关**；本接口只读研究库或现网库拿开赛时间与对阵编号映射，不因本接口打开或关闭双写。 |
| 额度 | 按场按需；今日实时采集（初盘／规则中盘／临盘／漏点救援）忙或额度不足时，本接口返回忙状态与重试建议，**不硬抢**额度。 |
| 缓存 | history 响应允许短时内存或本地文件缓存（建议生存时间 5～15 分钟）；缓存键含对阵编号、庄家、市场；过期后下次点开再拉。 |

---

## 2. 接口路径与查询参数

### 2.1 路径

```
GET /matches/{match_id}/odds/timeline
```

- `{match_id}`：与现有接口一致，接受 `match_uid`（例如 `2026-10-09|五003`）或内部数字主键。
- 若调用方只有 5DollarFootballAPI 对阵编号、库内尚无该场行：允许改用查询参数 `fixture_id`（见下），此时路径中的 `{match_id}` 传字面量 `by-fixture`，或使用并列路径：

```
GET /odds/timeline?fixture_id={fixture_id}
```

实现须二选一并在 OpenAPI 中固定一种；**推荐主路径**为 `GET /matches/{match_id}/odds/timeline`，`fixture_id` 仅作可选覆盖／调试。

### 2.2 查询参数

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `book` | 否 | `macauslot` | 5DollarFootballAPI 庄家 slug。允许值：`macauslot`、`crown`、`williamhill`、`pinnacle`、`bet365`。为兼容库内 book 码，亦接受 `macau`→`macauslot`、`william`→`williamhill`、`jc`／`chinasportslottery`（仅 `market=1x2` 有意义）。默认澳门。 |
| `market` | 否 | `asian` | 玩法：`asian`（亚盘）、`1x2`（欧盘胜平负）、`goalline`（大小球）。与 5DollarFootballAPI `/odds/history` 的 `market` 一致。 |
| `fixture_id` | 否 | （从库解析） | 若提供，直接用作 5DollarFootballAPI 对阵编号；否则由 `match_id` 查 `matches`／映射表得到。两者都缺则 404。 |
| `as_of` | 否 | 当前时刻（北京时间） | ISO-8601（建议带 `+08:00`）。只返回 `recorded_at ≤ as_of` 且 `recorded_at < kickoff_at` 的赛前 tick；用于回放「当时能看到的曲线」。 |
| `cache_ttl_sec` | 否 | 服务端配置（建议 300～900） | 调用方一般不传；调试时可缩短。服务端有上限，防止把缓存关掉变成刷额度。 |
| `force_refresh` | 否 | `false` | `true` 时绕过短时缓存强制再拉；仍须通过额度忙闲检查，忙则仍返回 503。 |

### 2.3 鉴权与限流

与现有 `match-analysis-api` 其它只读接口相同；本接口额外受「今日实时采集忙闲」闸门约束（见 §5、§6）。

---

## 3. 时间轴与坐标约定

### 3.1 剩余分钟 τ

对每一条赛前 tick：

\[
\tau = \frac{\texttt{kickoff\_at} - \texttt{recorded\_at}}{60}\quad\text{（单位：分钟，浮点允许）}
\]

- 仅保留赛前：`recorded_at < kickoff_at`（即 \(\tau > 0\)）。恰好开赛或赛中／赛后 tick **不进入**本折线。
- 开赛时刻未知（`kickoff_at` 空或占位且不可信）→ 返回 422，说明无法计算横轴。

### 3.2 横轴变换

\[
x = -\log_{10}(\max(\tau,\, 1))
\]

- \(\tau\) 以**分钟**计；小于 1 分钟按 1 分钟处理，避免对数发散。
- \(x\) 随临近开赛增大（例如还剩 480 分钟时 \(x\) 较小，还剩 1 分钟时 \(x=0\)）。
- 前端绘图必须用响应里的 `x` 作为数据横轴坐标；**不要**自己用线性时间当横轴。

### 3.3 预设刻度（标签仍显示人类可读剩余时间）

响应字段 `axis_ticks` 固定给出下列刻度点（服务端算好 `x`，前端只负责画刻度文字）：

| 标签（展示用） | \(\tau\)（分钟） | \(x = -\log_{10}(\max(\tau,1))\) |
|---|---|---|
| 还剩 8h | 480 | \(-\log_{10}(480)\) |
| 还剩 2h | 120 | \(-\log_{10}(120)\) |
| 还剩 60m | 60 | \(-\log_{10}(60)\) |
| 还剩 15m | 15 | \(-\log_{10}(15)\) |
| 开赛 | 0 | 按 \(\tau=1\) 处理，即 \(x = 0\)（刻度锚在「开赛」端；数据点本身不含 \(\tau=0\)） |

说明：「开赛」刻度是轴右端参照，不是一条真实行情点。若某场开赛前总时长不足 8 小时，仍返回完整 `axis_ticks`；前端可将视窗外的左侧刻度淡化或裁切，但契约不删刻度项。

### 3.4 纵轴与分图

| 市场 | 纵轴字段 | 展示建议 |
|---|---|---|
| `asian` | `line`（盘口，**主队视角，正数表示主队让球**，与现网 `odds_asian.handicap`／`GET /table/matches` 统一记法）；`home_water`、`away_water`（港盘水位口径，由欧式小数减 1 得到，与现网水位字段一致） | **推荐分两张图**：上图盘口 `line`～`x`；下图主／客水位～`x`。若单图双轴，左轴 `line`、右轴水位，须在图例写清，避免把盘口与水位画在同一数值轴上造成误读。 |
| `goalline` | `line`（大小球盘口，大球线）；`over_water`、`under_water` | 同上：盘口与大小球水位分图或双轴。 |
| `1x2` | `home`、`draw`、`away`（欧式小数，含本金） | 单图三条水位／赔率曲线即可；无 `line`。 |

5DollarFootballAPI 亚盘原始记法为「负数表示主队让球」时，**本接口必须取反**后写入 `line`，与现网统一；响应 `line_convention` 固定为 `home_give_positive`。

---

## 4. 成功响应 JSON

HTTP **200**。`Content-Type: application/json`。

```json
{
  "match_id": "2026-10-09|五003",
  "match_pk": 123,
  "fixture_id": 987654,
  "kickoff_at": "2026-10-09T19:35:00+08:00",
  "kickoff_minute_known": true,
  "book": "macauslot",
  "book_label": "澳门",
  "market": "asian",
  "line_convention": "home_give_positive",
  "water_convention": "hk_from_decimal_minus_one",
  "as_of": "2026-10-09T18:40:00+08:00",
  "source": "5dollar_odds_history",
  "cached": false,
  "cache_expires_at": null,
  "calls_used": 1,
  "pages_fetched": 1,
  "axis": {
    "x_transform": "neg_log10_tau_minutes",
    "x_formula": "x = -log10(max(tau_min, 1))",
    "tau_unit": "minutes_before_kickoff"
  },
  "axis_ticks": [
    { "label": "还剩 8h", "tau_min": 480, "x": -2.681241237 },
    { "label": "还剩 2h", "tau_min": 120, "x": -2.079181246 },
    { "label": "还剩 60m", "tau_min": 60, "x": -1.77815125 },
    { "label": "还剩 15m", "tau_min": 15, "x": -1.176091259 },
    { "label": "开赛", "tau_min": 0, "x": 0.0 }
  ],
  "ticks": [
    {
      "recorded_at": "2026-10-09T11:12:03+08:00",
      "tau_min": 502.95,
      "x": -2.70105,
      "line": 0.25,
      "home_water": 0.92,
      "away_water": 0.94,
      "over_water": null,
      "under_water": null,
      "home": null,
      "draw": null,
      "away": null
    }
  ],
  "display": {
    "recommended_layout": "split_line_and_waters",
    "charts": [
      {
        "id": "line",
        "title": "盘口（主队视角）",
        "y_fields": ["line"]
      },
      {
        "id": "waters",
        "title": "主／客水位",
        "y_fields": ["home_water", "away_water"]
      }
    ]
  },
  "notes": []
}
```

### 4.1 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `match_id` | string \| null | `match_uid`；仅 `fixture_id` 查询且库无映射时可为 null。 |
| `fixture_id` | integer | 5DollarFootballAPI 对阵编号。 |
| `kickoff_at` | string | 开赛时间，带时区；用于算 \(\tau\)。只读自研究库或现网库（或对阵详情），不因本接口改库。 |
| `book` / `market` | string | 实际用于拉取的 slug／市场（归一化后）。 |
| `source` | string | 固定 `5dollar_odds_history`（本版）；若将来有别的源再扩枚举。 |
| `cached` | boolean | 本次是否命中短时缓存。 |
| `cache_expires_at` | string \| null | 缓存过期时刻；未命中可为 null。 |
| `calls_used` | integer | 本次为满足请求实际打出的 5DollarFootballAPI 次数（含翻页；命中缓存则为 0）。 |
| `pages_fetched` | integer | history 翻页页数。 |
| `axis_ticks` | array | 见 §3.3；顺序从早到晚（左→右）或按 \(x\) 升序，实现固定一种并在此写死为 **按 \(x\) 升序**。 |
| `ticks` | array | 赛前变化点，按 `recorded_at` 升序。 |
| `ticks[].recorded_at` | string | 该报价首次记录时刻（变化点起点）。 |
| `ticks[].tau_min` | number | 距开赛剩余分钟。 |
| `ticks[].x` | number | \(-\log_{10}(\max(\tau,1))\)。 |
| `ticks[].line` | number \| null | 亚盘／大小球盘口；欧盘为 null。 |
| `ticks[].home_water` / `away_water` | number \| null | 亚盘水位；其它市场为 null。 |
| `ticks[].over_water` / `under_water` | number \| null | 大小球水位。 |
| `ticks[].home` / `draw` / `away` | number \| null | 欧盘赔率。 |
| `display` | object | 给前端的布局提示；前端可覆盖，但不改数据含义。 |
| `notes` | string[] | 人类可读备注（例如「仅一页」「开赛前不足 8 小时」）。 |

`axis_ticks` 中 `x` 的小数位数以实现四舍五入到合理精度为准（建议不少于 6 位小数）；前端以服务端值为准，勿自行重算导致刻度与点错位。

---

## 5. 错误码与忙闲

| HTTP | `error.code`（建议） | 何时 | 响应要点 |
|---|---|---|---|
| 400 | `bad_request` | 参数非法（未知 `book`／`market` 等） | 说明合法取值。 |
| 404 | `match_not_found` / `fixture_unmapped` | 无此比赛，或无 `fixture_id` 映射 | 提示先完成对阵映射；**不要**为找映射在本接口里扫大批 5DollarFootballAPI。 |
| 422 | `kickoff_unknown` | 无法得到可信 `kickoff_at`，算不了 \(\tau\) | 提示补开赛分钟后再试。 |
| 503 | `live_capture_busy` | 今日实时采集需要额度（固定让路时间窗、临近 due、实时采集或救援进程忙、`X-RateLimit-Remaining` 过低等，口径对齐历史补数让路文档） | 见下方 JSON；**不发** history 请求。 |
| 503 | `rate_limit_exhausted` | 已尝试拉取但上游额度见底／被限流 | 带 `retry_after_sec`。 |
| 502 | `upstream_error` | 5DollarFootballAPI 非限流失败 | 可带上游状态码摘要；可重试。 |
| 204 或 200 + 空 `ticks` | （实现二选一，**推荐 200 + 空数组**） | 上游无赛前 history | `notes` 写明「无赛前变盘记录」。 |

### 5.1 忙响应示例（503）

```json
{
  "error": {
    "code": "live_capture_busy",
    "message": "今日实时采集需要额度，赛前折线暂不拉取 5DollarFootballAPI 变盘历史。请稍后重试。",
    "paused_reason": "due_avoid_sec=420",
    "retry_after_sec": 180,
    "hint": "可在实时采集空闲后再点开本场；短时缓存命中时仍可返回旧曲线（若 cached=true 的 200）。"
  }
}
```

约定：

1. **未命中缓存**且判定忙 → **503**，不打上游。  
2. **已命中未过期缓存** → 允许 **200** 返回缓存曲线，并设 `cached=true`；`notes` 可注明「缓存返回；实时采集忙时未刷新」。  
3. `force_refresh=true` 且忙 → **503**，即使有缓存也不假装已刷新（可选择在 `error` 里附带 `stale_available: true` 提示前端改用非强制请求）。

忙闲探测复用与历史补数相同的信号（固定时间窗、due 前 15 分钟、heartbeat、Remaining 下限、救援进程等），详见 [`v2_0-hist-backfill-yield-to-live.md`](./v2_0-hist-backfill-yield-to-live.md)。本接口是**交互只读**，默认阈值应略宽于批量补数（例如 Remaining 下限可与补数相同，但单次最多 1～3 次 history 调用），仍禁止在临盘高峰硬抢。

---

## 6. 后端实现要点

1. **解析场次**：`match_id` → 库内行 → `fixture_id` + `kickoff_at`（研究副本或现网只读）。无映射则 404。  
2. **忙闲闸**：在发 5DollarFootballAPI 之前检查；忙则走 §5。与 `DUAL_WRITE` 无关。  
3. **短时缓存**：键建议 `timeline:{fixture_id}:{book}:{market}`；值含归一化后的 `ticks` 与拉取元数据；生存时间默认 **5～15 分钟**（建议默认 10 分钟）。进程内内存即可；多进程可用本地文件或共享缓存。  
4. **拉取**：`GET /v1/fixtures/{fixture_id}/odds/history?bookmaker={book}&market={market}`；按上游分页约定翻页，直到无下一页或达到服务端页数上限（建议上限写进配置，防止异常场次刷爆额度）。  
5. **过滤**：只留 `recorded_at < kickoff_at`；若传了 `as_of`，再要求 `recorded_at ≤ as_of`。  
6. **归一化**：盘口正负号统一为主队让球为正；水位由欧式小数减 1；时间统一到 `+08:00` 字符串。  
7. **计算**：对每点算 `tau_min`、`x`；生成固定 `axis_ticks`。  
8. **禁止写库**：不得 `INSERT`／`UPDATE` `odds_asian`、`odds_euro_*`、`odds_snapshot` 主路径；原始 JSON 若落盘仅允许**可选**的调试目录且默认关闭，且不得当作正式全时序库。  
9. **额度记账**：每次实际上游调用计入与补数／实时采集共用的账本（如 `shared_api_yield`），便于 Remaining 与每分钟上限协同。  
10. **调用预算**：默认澳门亚盘通常 **1 次**（偶发翻页 +1）；前端不得默认改成五家×三市场连拉。若产品以后要「切换庄家／玩法再拉」，每次切换算一次新的按场请求，同样走忙闲与缓存。

---

## 7. 与前端展示约定

1. **入口**：比赛详情／赛前分析页提供「盘口走势」或等价入口；**点开或展开时才请求**本接口；列表页禁止批量预取。  
2. **默认**：请求不传 `book`／`market`，即澳门亚盘。切换庄家或玩法用明确控件，避免误触连拉。  
3. **横轴**：用返回的 `x` 绑定点坐标；用 `axis_ticks` 画刻度与标签（「还剩 8h／2h／60m／15m／开赛」）。  
4. **纵轴**：亚盘默认 **分图**：上盘口、下水位；不要把 `line` 与 `home_water` 画在同一数值轴上除非用户显式选「双轴」。  
5. **忙**：收到 503 `live_capture_busy` 时展示文案与 `retry_after_sec` 倒计时，提供「稍后重试」；若错误体提示有缓存可用，可提供「查看上次缓存」按钮（再次请求且 `force_refresh=false`）。  
6. **空数据**：`ticks` 为空时说明「暂无变盘历史」，不要画假线。  
7. **时区**：展示开赛与 `recorded_at` 用北京时间（UTC+8）。  
8. **与初中临快照关系**：折线是辅助；表格里的初盘／中盘／临盘仍读原有快照接口，二者并存、互不覆盖。

---

## 8. 验收清单

| 编号 | 验收项 | 通过标准 |
|---|---|---|
| A1 | 点开才拉 | 未打开折线时网络面板无 `/odds/timeline` 与上游 history；打开后才有。 |
| A2 | 默认澳门亚盘 | 不传参时 `book=macauslot`、`market=asian`。 |
| A3 | 不写主表 | 调用前后 `odds_asian` 行数与指纹不变；无对本接口路径的写库。 |
| A4 | 横轴公式 | 抽样点满足 \(x = -\log_{10}(\max(\tau,1))\)（允许浮点误差）。 |
| A5 | 刻度 | `axis_ticks` 含 τ∈{480,120,60,15,0} 五档及约定中文标签。 |
| A6 | 盘口记法 | 亚盘 `line` 与同场快照／表格主队视角一致（正数＝主让）。 |
| A7 | 忙闲 | 在让路时间窗或 Remaining 过低时，无缓存 → 503 且 `calls_used` 不计上游；有缓存 → 可 200 且 `cached=true`。 |
| A8 | 缓存 | 同一场默认参数在生存时间内第二次请求 `cached=true`、`calls_used=0`。 |
| A9 | 赛前过滤 | 响应中无 `recorded_at ≥ kickoff_at` 的点。 |
| A10 | as_of | 传入早于部分 tick 的 `as_of` 后，更晚的 tick 不出现。 |
| A11 | 分图提示 | `display.recommended_layout` 对亚盘为分盘口／水位；前端默认遵循。 |
| A12 | 额度 | 单次默认请求上游调用次数 ≤ 配置上限（默认可验收为 ≤3，含翻页）。 |

---

## 9. 给前端／后端（协作用短摘要）

**做什么**：赛前盘口／水位折线；用户点开某场才拉 5DollarFootballAPI 变盘历史；默认澳门亚盘；库继续只存初盘／中盘／临盘，不把每一跳写入 `odds_asian`。

**接口**：`GET /matches/{match_id}/odds/timeline`  
可选查询：`book`（默认 `macauslot`）、`market`（默认 `asian`）、`fixture_id`、`as_of`、`force_refresh`。

**横轴**：剩余分钟 \(\tau\)，\(x = -\log_{10}(\max(\tau,1))\)；刻度标签为「还剩 8h／2h／60m／15m／开赛」。  
**纵轴**：亚盘用 `line` + `home_water`／`away_water`；默认上图盘口、下图水位。

**忙**：今日实时采集需要额度时返回 503（可带重试秒数）；有短时缓存时可返回旧曲线并标记 `cached=true`。  
**缓存**：建议 5～15 分钟，禁止把 ticks 写入赔率主表。

前端：详情页点开再请求；列表勿预取。后端：只读库拿开赛与对阵编号 → 忙闲检查 → 缓存或拉 history → 归一化盘口正负号与水位 → 算 \(x\) → 返回；不碰 `DUAL_WRITE`。

---

## 10. 修订记录

| 日期（北京时间） | 说明 |
|---|---|
| 2026-10-09 | 首版契约落盘（用户拍板：点开才拉、默认澳门亚盘、对数横轴、不存全时序、忙则 503）。 |


---

## 附录：前端机构选择（2026-10-09 用户补充）

用户确认：**默认仍是澳门亚盘，但前端必须可改机构**。

1. 在折线图区域提供「机构」选择控件（下拉或单选均可）。
2. 选项至少包含：澳门（`macauslot`）、皇冠（`crown`）、威廉希尔（`williamhill`）、平博（`pinnacle`）、Bet365（`bet365`）。展示用中文名，请求用 slug。
3. 首次进入默认选中澳门；用户改选后，带着新的 `book`（及当前 `market`）再次调用 `GET /matches/{match_id}/odds/timeline`，替换图上数据。
4. 玩法（亚盘／欧盘／大小球）若一并做选择，同样走查询参数 `market`；第一版至少保证亚盘＋机构可选。
5. 切换机构仍遵守「点开才拉」：只拉当前这一场当前所选机构，不要预拉其它机构。
