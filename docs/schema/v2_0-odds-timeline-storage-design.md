# v2.0 · 赔率时间线存储与双通道取数设计（只设计、不落库）

> **2026-10-06 用户决定：必发（Betfair）不纳入。** 5Dollar 的必发只有 1X2 价格，没有成交量/支持率，用处不大。以下凡涉及「第 7 源必发」的设计一律不实施；§1.4 仅作实测存档。存储与调用量以 6 家口径为准：每场约 65 KB、年约 0.6 GB，约 6 000 次/月。`support_proxy_odds` 继续沿用。

> **2026-10-06 补充：规定通道钟点改定。** 开赛 ≥23:00（含次日 0–10）→ 规定中盘 **当天 15:00**、规定临盘 **当天 22:00**（`channel=rule`）；旧 0–10→前一日 16:00/23:00 保留为 `rule_legacy` 对照。真实 T−8h/T−1h 不变。日用预测通知维护者 21:00/22:00，见 （日用通知节奏文档，未公开）。
> **2026-10-06 补充：采集范围 = 竞彩赛事白名单。** 日常与历史赔率只覆盖「中国竞彩足球曾经开过」的赛事（见 `../research/jingcai-competition-whitelist/`）。竞彩以后开了白名单里没有的赛事，先入白名单再补该赛事历史。竞彩选出过的**单场**仍是日用主源；白名单赛事的**全部场次**是否一并采，由用户按该研究里的调用量估算另选。必发仍不纳入。



> 状态：**设计草案**。本文不改生产库、不改定时任务。  
> 日期：2026-10-06（UTC+8）。API 现状：`match-analysis-api` **0.3.8**；库：`$APP_DB_PATH`（约 **1.13 MB**，177 场竞彩）。  
> 依据：现有 schema（`v1_sqlite.sql`、`v1_7-water-tier-midpoint.md`）、数据约定（`data-conventions-v1.md`）、5Dollar 实测（`../5dollar/probe-2026-10-06/README.md` + `raw/` + `betfair/`）、定时任务评估（`../backfill/routine-assessment-2026-10-05.md`）。  
> 硬约束：**不破坏**仓库旧 AH 口径（`CFFXDJ_5_V3`、`macau_close`、固定澳门水位 0.95）；新口径只能**并列对照**。

---

## 0. 一句话结论（推荐方案）

1. **窄表（长表）存「变化点」时间线** + **固定快照表** + **查询视图**对齐现有 `odds_asian` / 欧赔 / 竞彩表。  
2. **主存无损变化点**（盘口或水位一变就新开一段）；**有损死区压缩**只做可选物化视图，默认分析不用。  
3. **双通道** `channel=rule|actual`：凌晨/睡后场两套时刻都记；白天场重合。验证指纹带 channel。旧数据默认 `rule`。  
4. **庄家 6 家真有玩法** （必发已决定不纳入，见文首说明）；**无**成交量/支持率字段可建。竞彩让球胜平负 5Dollar 拿不到，来源标 **待定（sporttery 等）**。  
5. **5Dollar 常驻队列 ≤30 次/分钟**；免费 API 继续日/周补充核验。原始 JSON **按月 gzip 归档**到库外，库内不堆全文。

---

## 1. 背景与现状

### 1.1 现有表（生产库实测）

| 表 | 现状（2026-10-06） | 角色 |
|---|---|---|
| `matches` | 177 场，全 `jingcai`；已有 `kickoff_at` / `kickoff_minute_known` | 场次主表 |
| `odds_asian` | macau … / crown … / william …；`phase∈{open,mid,close}`；v1.7 后 crown/william 水位为**中点水位**，`water_src`/`water_censored`/`extras_json` | 日用 AH + V3 结算 |
| `odds_euro_home` | macau/william 各 354；**只有主胜** | 仓库旧口径 |
| `odds_jc_home` | 352 | 竞彩主胜 |
| `odds_jc_hhad` | **0 行**（表已预留） | 竞彩让球胜平负 |
| `odds_raw` | 177 行，约 570–611 字节/场 | 导入 JSON 备份（手工月度包） |

水位中点规则见 `v1_7-water-tier-midpoint.md`：`w = 0.70 + 0.05 × t`；结算版本 `ah_v4_water_midpoint`；**macau 仍按固定 0.95**。

### 1.2 仓库时间点口径（须可对照）

| 点 | 含义 |
|---|---|
| `open` | 开盘 |
| `mid` | 规定中盘（见下；白天场通常 ≈ 真实 T−8h） |
| `close` | 规定临盘（见下；白天场通常 ≈ 真实 T−1h） |

**规定通道 `rule`（用户 2026-10-06 定，日用默认）**：

- 凡开赛时间 **≥ 23:00**（北京；含挂在前一竞彩日、日历次日 **0–10 点** 的凌晨场）的场次：  
  - 规定中盘 = **开赛所在「竞彩日」当天 15:00**  
  - 规定临盘 = **同一天 22:00**  
  - 例：竞彩日 10-06、开赛 10-07 02:00 → mid=`10-06 15:00`，close=`10-06 22:00`；开赛 10-06 23:30 → 同上。  
- 开赛 **&lt; 23:00** 且非上述凌晨挂日场：规定 mid/close 仍按仓库习惯对齐真实 T−8h / T−1h（与 `actual` 通常重合）。  
- 日用预测通知节奏：每天 **21:00** 一版、**22:00** 睡前终版（差分才发）；详见 （日用通知节奏文档，未公开）。终版与规定临盘 22:00 对齐。

**旧规定通道 `rule_legacy`（对照列，不覆盖、不删除）**：

- 仅针对开赛小时 ∈ **0–10**（竞彩日挂前一日）的旧口径：中盘 **前一日 16:00**、临盘 **前一日 23:00**（仓库 `calc_collection_times` / 旧手工 mid·close）。  
- 用途：与新 `rule`、与 `actual` 对照；旧 177 场手工数据默认映射到本通道语义（导入时 `channel` 可标 `rule_legacy` 或保留 `rule` + 备注旧钟点）。  
- **不得**用新 15:00/22:00 回写覆盖旧快照。

**真实通道 `actual`（始终保留）**：

- T−8h：`recorded_at ≤ kickoff−8h` 的最后一条赛前 tick。  
- T−1h：`recorded_at ≤ kickoff−1h` 的最后一条赛前 tick。  
- 与规定通道独立记账；白天场可两行指向同一 tick。

### 1.3 5Dollar 实测要点（事实）

来源：`probe-2026-10-06`（46 次调用全 200，无 429）。

| 项 | 事实 |
|---|---|
| 多家赔率 | `/fixtures/{id}/odds` **一次**可拿全 20 家 + 竞彩 |
| 变盘 | `/odds/history`：一家 + 一个市场；tick 带秒级 `recorded_at` |
| 报价 | **欧式小数（含本金）**，如澳门 `1.82/2.02`；港盘水位 ≈ 小数 − 1 |
| 竞彩 | 仅 **胜平负**；docs：「other Jingcai markets are not carried」；编号约自 **2025-10-25** 起有 |
| SBO | **不在** bookmakers 列表（21 项无 SBO） |

**目标庄家 6 家 + 必发第 7 源（实测 `/odds`，法国–比利时 / 韩国–乌兹；必发另见 §1.4）**：

| 公司 | slug（API） | 库内 book 建议 | 1X2 | 亚盘 | 大小球 | 竞彩让球 |
|---|---|---|---|---|---|---|
| 澳门 | macauslot | `macau` | ✓ | ✓ | ✓ | — |
| 皇冠 | crown | `crown` | ✓ | ✓ | ✓ | — |
| 威廉希尔 | williamhill | `william` | ✓ | ✓ | ✓ | — |
| 平博 | pinnacle | `pinnacle` | ✓ | ✓ | ✓ | — |
| Bet365 | bet365 | `bet365` | ✓ | ✓ | ✓ | — |
| 竞彩 | chinasportslottery | `jc` | ✓（胜平负） | — | — | **5Dollar 无**；来源 **待定**（sporttery 等） |
| **必发** | betfair | `betfair` | ✓（仅价格） | — | — | —；**无成交量/买卖盘/可用金额**（§1.4） |

不加角球、半场、BTTS 等多余市场（Bet365 虽有也不入库），避免空字段膨胀。

**变盘密度（赛前 tick，法国 4-1 比利时，开赛 2026-10-06 02:45 北京）**：

| 公司·市场 | 赛前 tick（实测） | 无损变化段数 | 死区 &lt;0.02 且盘口不变（实测） |
|---|---|---|---|
| 澳门亚盘 | **8** | 8（已无连续重复） | 8（压不动） |
| 皇冠亚盘 | **69** | 69（无连续相同值） | **30** |
| Bet365 亚盘 | **21** | 21 | 21 |
| 澳门大小 | **6** | 6 | 6 |
| 竞彩 1X2 | **5** | 5 | 4 |

横滨–清水（六204）澳门亚盘赛前仅 **2** 条（T−114h 与 T−1.1h）——稀疏到「T−8h」只能落到 114h 前那条（须在快照上标 `stale_gap`）。

---

### 1.4 必发（Betfair）实测（仅存档，已决定不纳入）（2026-10-06 18:30–18:31 UTC+8）

样例目录：`../5dollar/probe-2026-10-06/betfair/`（README + raw JSON；**14** 次调用，≤15 预算）。文档页 https://5dollarfootballapi.com/docs 博彩公司表：Betfair 仅 **Pre / 1X2**；未见独立「交易所成交量」端点说明。

| 检查项 | 实测结果 |
|---|---|
| `/fixtures/{id}/odds?bookmakers=betfair` | 仅 `odds.1x2.opening|closing|inplay`；三场样例 `inplay=null`；值为欧式小数（含本金） |
| `/odds/history?bookmaker=betfair&market=1x2` | 有时间序列：FRA–BEL **102** tick、KOR–UZB **164**、CHE–TOT **98**；字段仅 `home,draw,away,minute,score,recorded_at` |
| history `market=asian\|goalline` | HTTP 200，**ticks=[]** |
| `/fixtures/{id}/exchange`、`/betfair` | **HTTP 404** `resource_not_found` |
| `?exchange=1` | 响应与不加参数相同，无额外键 |
| 成交量 / 各选项成交占比（支持率） | **响应中不存在**（无 volume/matched/size/back/lay/liquidity 等键） |
| 买卖价、可用金额 | **无** |
| T−8h / T−1h 取价 | 可用 history 做（FRA–BEL 实测：T−8h→`1.5/5/7` lag≈8.35h；T−1h→`1.53/4.9/6.6` lag≈1.03h） |

**设计结论（必发，原草案，已作废，见文首）**：

1. 纳入第 7 数据源，`book=betfair`，**只建 1X2 价格**相关列/行（`market=euro_1x2` 快照 + 变化点时间线）；不建成交量、占比、back/lay、available 列。  
2. **不能**用本 API 的 Betfair **取代**仓库 `support_proxy_odds`（无真实支持率）。支持率若仍需要，须另寻数据源；在此之前继续用既有 proxy，并在文档标明局限。  
3. 调度：`/odds` 已可与其它庄家同次拉取（`bookmakers=...,betfair`）；history 需 **额外 1 次/场**（`bookmaker=betfair&market=1x2`）。按日均 ~30 场粗估，赛后补史 **+约 30 次/天 ≈ 900 次/月（估算）**。  
…. 存储增量（估算）：~… 段/场 × … 字节 ≈ **… KB/场**；年 … 万场约 **… MB**（估算）。gzip 归档另加约 …–… KB/场原文量级（FRA–BEL history 未压约 … KB）。


## 2. 为什么用窄表 + 视图（权威依据）

### 2.1 问题

宽表「一场一行、每家每玩法每时刻一列」在 6 家 × 3 玩法 × 多时间点 × 双通道下会爆炸；且竞彩没有亚盘、澳门有时空水位——宽表必然大量 NULL。

### 2.2 做法

采用 **事务事实表 / 原子粒度**（Kimball：先声明 grain，一行 = 一次可度量事件；稀疏事实表只在有观测时落行）：

- **Grain A（时间线）**：一场 × 一家 × 一个市场 × **一个水位/盘口不变的时间段**（变化点 / 游程）。  
- **Grain B（快照）**：一场 × 一家 × 一个市场 × **一个通道** × **一个命名时间点**。

参考：

- Kimball Group，《Declaring the Grain》/ Transaction Fact Tables：原子粒度、稀疏事实、不同 grain 分表。  
- 工业时序：AVEVA Historian **value deadband**（变化小于阈值不存）；Schneider Geo SCADA **Significant Change** 压缩——作「可选有损」参考，不作主存。  
- 通用无损：游程编码 RLE / 变化点存储（TDengine 等对平稳段的 RLE 说明）。

查询时用 **VIEW** 把窄表投影成现在引擎熟悉的 `book+phase+handicap+water` 形状，V3 / compare **读视图或兼容表**，不强迫改旧结算代码路径。

---

## 3. 存储模型

### 3.1 逻辑分层

```
[原始归档] odds_raw_archive (库外文件 + 索引表)
    ↓ 解析
[时间线]   odds_timeline_seg     ← 主存，无损变化点
[快照]     odds_snapshot         ← open / T-8h / T-1h / 规定中临盘 / close；带 channel
[兼容]     VIEW v_odds_asian_compat → 现有 odds_asian 语义（默认 channel=rule）
[可选]     VIEW / 物化 v_odds_timeline_deadband ← 有损，分析可选
```

**原则**：分析默认走 **快照 + 无损时间线**；死区压缩只用于「看走势图省点、或磁盘极紧」的场景，并在 UI/指纹上标明 `compression=deadband_0.02`。

### 3.2 水位表示（与 v1.7 衔接）

| 来源 | 存什么 | `water_src` |
|---|---|---|
| 5Dollar 小数赔率 | 同时可存 `price_dec`（含本金）与 `water_hk = price_dec − 1` | `actual` |
| 旧手工档位 | 已迁中点水位（v1.7）；原档位在 `extras` | `tier_midpoint` |
| 澳门结算默认 | **不改** V3：结算仍可用固定 0.95；库内另存真实水位供对照口径 | `fixed_macau`（结算侧）vs `actual`（对照侧） |

截断：`water_censored` 仅对手工档位 0/10 有意义；API 真实水位一般不截断。

### 3.3 通道（channel）

| channel | 含义 | 谁用 |
|---|---|---|
| `rule` | **新规定**中盘/临盘（≥23:00 场 → 当天 15:00 / 22:00；其余对齐 T−8h/T−1h） | **日用默认**；日用 21:00/22:00 预测；新回测 |
| `rule_legacy` | **旧规定**（0–10 点场 → 前一日 16:00 / 23:00；旧手工 mid/close） | 对照列；对齐旧 CFFXDJ_5_V3 / 旧回测；**不覆盖** |
| `actual` | **真实**开赛前 8h、1h：对 history 取 `recorded_at ≤ kickoff−8h/1h` 的最后一条赛前 tick | 与规定通道对照；同方案异通道对比 |

规则：

1. 开赛 ≥23:00（含次日 0–10 点挂日场）：同一 `point`（mid/close）可同时有 `rule`、`rule_legacy`（若适用）、`actual` 多行；`recorded_at` / `target_at` 不同。  
2. 白天场（开赛 &lt;23:00 且非凌晨挂日）：`rule` 与 `actual` 可指向同一 tick，**两行都写**（查询简单）。  
3. **旧 177 场**导入：优先 `channel='rule_legacy'`（或 `rule` + `extras_json` 注明旧 16:00/23:00）；`point` 仍为 open/mid/close；**禁止**用新钟点回写。  
4. 预测表 / `strategy_validation_*` 指纹的 `extra` 增加 `odds_channel`（日用默认 `rule`）；compare 页可选多通道曲线。  
5. 快照必须带 `recorded_at` 与 `lag_hours = (kickoff_at − recorded_at)`；若 `lag_hours` 相对目标偏差过大（如目标 8h 实际 114h），标 `stale_gap=1`（横滨–清水实测案例）。  
6. 日用消息须标所用通道（见 （日用通知节奏文档，未公开））。

### 3.4 压缩策略（分析影响）

| 策略 | 做法 | 任意时刻取值 | 推荐 |
|---|---|---|---|
| **A. 无损变化点** | 盘口或任一侧水位变化 → 新段；段内记 `seg_start`/`seg_end`、值、可选 `tick_count` | **可精确还原**任一时刻（取 `t ∈ [start,end]` 的段值） | **主存** |
| **B. 稀疏时间合并** | 澳门等已很稀，再按时段硬合并只会丢变化 | 不可精确 | **不采用**（澳门实测 8 条已是变化点） |
| **C. 死区压缩** | 同盘口且 \|Δhome\|&lt;0.02 且 \|Δaway\|&lt;0.02 则并入上一段；段内保留 hi/lo 与 `tick_count` | **不能**精确还原被吞掉的中间值；只能还原代表值 + 区间 | **可选视图** |
| **D. 固定快照** | open、actual T−8h、actual T−1h、rule mid、rule close、api opening/closing | 点上精确；点间靠时间线 | **必存** |

皇冠亚盘实测：69 → 死区 30 段（约 **43%**），段内最大漂移约 0.02（贴死区边界）——走势形状仍在，细微拉锯会糊掉。对「盘口变动检测 / 精确水位结算」应用 **A**；对「画压缩曲线」可用 **C**。

滚球 tick（`minute` 非空）**首期不入库**（或另表 `odds_timeline_inplay` 后期再开），先保证赛前分析体积可控。

---

## 4. 存储量估算（标明估算 / 实测）

### 4.1 单场实测原料（法国–比利时）

| 对象 | 字节（未压） | gzip 后（实测） |
|---|---|---|
| `/odds` 全家快照 | 9 895 | 1 653（约 17%） |
| history 澳门亚盘 | 10 882 | （同类约 8–15%） |
| history 皇冠亚盘 | 38 624 | **2 943（约 7.6%）** |
| history Bet365 亚盘 | 45 198 | — |
| history 澳门大小 | 10 629 | — |
| history 竞彩 1X2 | 844 | — |
| 上列 5 个 history 合计 | **106 177** | — |

### 4.2 单场入库规模（估算）

假设赛前存 6 家庄家相关市场 + 必发 1X2、不做滚球；变化段数按「实测密度外推」：

| 书 | 亚盘 | 大小 | 1X2 | 依据 |
|---|---|---|---|---|
| macau | 8 | 6 | ~8 | 亚/大实测；1X2 **估算** |
| crown | 69 | ~50 | ~40 | 亚实测；余 **估算** |
| william | ~40 | ~30 | ~30 | **估算**（中等密度） |
| pinnacle | ~50 | ~40 | ~40 | **估算** |
| bet365 | 21 | ~20 | ~25 | 亚实测；余 **估算** |
| jc | — | — | 5 | 实测 |
| betfair | — | — | ~100 | 实测 FRA 102 / KOR 164 / CHE 98 |

- **无损变化段合计 ≈ 482（6 家）+ ~100（必发 1X2）≈ **582 段/场（估算）**  
- **死区视图 ≈ 276 段/场（估算）**（对 dense 书套用皇冠 30/69 比例）  
- **快照**：约 5 个命名点 × (5×3 + jc + betfair) ≈ **85 行/场（估算）**

按 SQLite 约 **120 字节/时间线段**、**100 字节/快照行**（估算）：

| 方案 | 每场（估算） | 每月 ~900 场（估算） | 每年 ~1 万场（估算） |
|---|---|---|---|
| 仅快照 | ~8 KB | ~7 MB | ~76 MB |
| 快照 + 无损时间线（含必发） | ~77 KB | ~68 MB | ~750 MB |
| 再加死区物化（可选） | +~33 KB | +~29 MB | +~320 MB |
| 原始 JSON 全文进库（不推荐） | ~340 KB 量级 | ~300 MB | ~3.4 GB |
| 原始 JSON **分月 gzip 归档**（推荐） | ~50–80 KB/场量级 | ~45–70 MB | ~0.5–0.8 GB |

说明：

- 日均场次按实测竞彩抽样 **12–45 场/天**，月按 **~30×30≈900**、年按 **~1 万**——均为**粗估**，不是精确计数。  
- 现库 177 场只有 open/mid/close 手工点，**1.13 MB**；上表是「接上 5Dollar 时间线之后」的增量量级。  
- **结论**：窄表 + 变化点 + 库外 gzip，一年竞彩主清单仍在 **约 1 GB 以内（估算）**，SQLite 可接受；不要把每场 history 全文塞进 `odds_raw`。

### 4.3 原始 JSON 归档建议

```
$ODDS_DATA_DIR/archive/5dollar/YYYY/MM/
  fixtures/{fixture_id}_odds.json.gz
  history/{fixture_id}_{book}_{market}.json.gz
  manifest.sqlite   # 或 parquet：fixture_id, path, sha256, fetched_at, bytes_raw, bytes_gz
```

- 按月目录；单文件 gzip（实测皇冠 history **约 13:1**）。  
- 库内 `odds_fetch_blob` **只存路径 + 哈希 + 字节数**，不存正文。  
- 保留 ≥ 12 个月热归档；更早可挪冷存储。  
- 回退 / 重解析：用 manifest 重跑 parser，幂等写入 timeline/snapshot。

---

## 5. DDL 草案（不执行）

> 下列 SQL **仅文档**；落地时由后端开 migration `v2.0`，先在副本验证。

```sql
-- ========== v2.0 odds timeline（草案）==========
-- book: macau|crown|william|pinnacle|bet365|jc|betfair
-- market: asian|ou|euro_1x2|jc_spf|jc_hhad
-- channel: rule|actual
-- point: open|mid|close|t8|t1|api_opening|api_closing

CREATE TABLE IF NOT EXISTS odds_timeline_seg (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  book          TEXT NOT NULL,
  market        TEXT NOT NULL,
  seg_start_at  TEXT NOT NULL,          -- ISO-8601 + offset
  seg_end_at    TEXT NOT NULL,
  line          REAL,                   -- AH 主队盘 / OU 球数 / HHAD 让球；1X2 为 NULL
  -- 欧式小数（含本金）；按市场使用对应列，其它为 NULL（窄表允许稀疏列，避免「假宽表」）
  price_home    REAL,
  price_away    REAL,
  price_draw    REAL,
  price_over    REAL,
  price_under   REAL,
  -- 港盘水位（小数-1）；AH/OU 常用；1X2 可空
  water_home    REAL,
  water_away    REAL,
  water_over    REAL,
  water_under   REAL,
  tick_count    INTEGER NOT NULL DEFAULT 1,
  compression   TEXT NOT NULL DEFAULT 'change_point',  -- change_point 主表只允许此值
  is_inplay     INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT '5dollar_history',
  water_src     TEXT,                   -- actual | tier_midpoint | NULL
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
  channel       TEXT NOT NULL,          -- rule | actual
  point         TEXT NOT NULL,          -- open|mid|close|t8|t1|...
  recorded_at   TEXT,                   -- 实际采用的 tick 时间
  target_at     TEXT,                   -- 目标时刻（如 kickoff-8h；新 rule 15:00/22:00；旧 rule_legacy 16:00/23:00）
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

-- 原始包索引（正文在库外 gzip）
CREATE TABLE IF NOT EXISTS odds_fetch_blob (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      INTEGER REFERENCES matches(id) ON DELETE SET NULL,
  fixture_id    TEXT,
  kind          TEXT NOT NULL,          -- odds | history
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

-- 取数队列（常驻脚本用；可与 DB 同库或独立）
CREATE TABLE IF NOT EXISTS odds_fetch_queue (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      INTEGER,
  fixture_id    TEXT,
  task          TEXT NOT NULL,          -- csl_list|odds_snap|history|backfill
  priority      INTEGER NOT NULL,      -- 越小越先
  not_before    TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  status        TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|dead
  checkpoint_json TEXT,                 -- 断点：已完成的 book/market 列表
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ofq_poll
  ON odds_fetch_queue (status, priority, not_before);

-- 兼容视图：默认 rule 通道，映射到旧 phase 名
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
```

**竞彩让球（`jc_hhad`）**：表结构在 `odds_snapshot` / timeline 已用 `market='jc_hhad'` 支持；**5Dollar 无数据**，`source` 待定为 `sporttery` 等，首期可不调度拉取。

**与旧表关系**：

- 过渡期：**继续写** `odds_asian`（从 `odds_snapshot` channel=rule 同步），保证 0.3.8 结算零改动。  
- 稳定后：结算改读视图；`odds_asian` 可变为视图或停写（另开里程碑，需回归 V3 哈希）。

---

## 6. 取数调度（设计；本次不改现网定时）

### 6.1 5Dollar 常驻 Worker

| 项 | 设计 |
|---|---|
| 限速 | **≤ 30 次/分钟**（40，留 25% 余量）；令牌桶；429 → 读 `Retry-After` 指数退避 |
| 进程 | `systemd` user service 或 `supervisord`；崩溃拉起；单实例锁文件防双开 |
| 队列优先级 | ① 临近快照点的竞彩场（T−8h±10min、T−1h±10min、规定 mid/close）→ ② 当日竞彩清单同步 → ③ 完赛补 history → ④ 历史回补（自 2025-10-25）→ ⑤ 更早无编号场 |
| 每分钟批量 | 按剩余令牌取 N 个任务；`/odds` 一次多家；`/history` 拆 book×market |
| 断点 | `odds_fetch_queue.checkpoint_json`：已完成的 (book,market)；进程重启不重跑 done |
| 去重 | UNIQUE(path) / UNIQUE 快照键；history 重拉则先删同 match+book+market 的 `change_point` 段再写入 |
| 幂等 | 同一 `seg_start_at` 冲突则忽略或更新 `seg_end_at` |

**历史回补粗算（与实测 README 一致，估算）**：2025-10-25 至今几千～一万多场 × 每场 4–5+ 次 ≈ 数万次；30/分钟 → **连续约 1–2 天**，宜拆多夜。

**月增量调用（估算）**：日均 30 场 × (清单 1 + 赛前快照 2 + 赛后 odds 1 + history 若干含 **betfair 1x2 +1**) ≈ 原估 **~6 000/月**，加上必发 history 后约 **~7 000/月**；若加密到「每分钟扫队列」、临近窗口多探一次，可达 **2–3 万/月** 量级，仍远低于 40×60×24×30 的理论分钟窗上限。

**必发调度要点（已作废，不实施）**：与 6 家庄家共用 `/odds` 快照（不增加调用）；完赛补史队列增加任务 `history:betfair:1x2`；不调度 asian/goalline（实测空）。

### 6.2 免费 API 分工（补充 / 核验，不替代 5Dollar 主时间线）

沿用 `routine-assessment-2026-10-05.md`，本次**不改任务**，设计上定位如下：

| 源 | 建议节奏 | 分工 |
|---|---|---|
| API-Football | 每天 08:05 | 近窗赛程/赛果对照；额度紧，不做深历史赔率主存 |
| InferSports | 每天 09:05 | 快照归档；`stale` 不得冒充临盘 |
| football-data.org | 周三/周日 21:05 | 赛果与部分赔率核对 |
| OddsPapi | 周日 22:05 | 月配额少；抽平博等开收盘对照 |
| BSD | 按现有评估 | 补充源；写入核验表而非覆盖主时间线 |
| 周一 10:05 交叉核验 | 保持 | 差异有变化才通知 |

5Dollar 暂停中的「Bet365 续拉」**不要原样恢复**；应在用户确认后换成「多庄 odds + history 队列」（见实测 README §8.F）。

---

## 7. 对预测 / 验证 / 前端的影响

| 模块 | 变化 |
|---|---|
| `predictions` / legs | 增加可选 `odds_channel`（默认 `rule`）；消息预览注明通道 |
| validate / compare 指纹 | `extra.odds_channel`；切换通道 → 新指纹，不误复用 |
| 结算 | 默认仍 `macau_close` + 固定 0.95；新增可选 `settle_book`+`channel=actual`+真实澳门水位对照口径（**不改 V3 默认**） |
| 对比页 | 通道切换 + 可选「rule vs actual」双曲线；冲突表不变 |
| `/matches/{id}/odds` | 增加 `channel`、`timeline`（摘要）、`stale_gap`；`?raw=true` 仍回旧 JSON |

---

## 8. 迁移与回退

1. **只加表**：`v2_0_odds_timeline.sql` 进 `MIGRATION_PATHS`；不改 `odds_asian` 旧行。  
2. **回填快照**：从现有 `odds_asian` / euro / jc 生成 `odds_snapshot`（`channel=rule`，`source=legacy_import`）。  
3. **双写**：新采集 → snapshot + timeline，并同步写旧表（过渡）。  
4. **回退**：停 worker；`DROP` 新表或整库恢复备份；旧表未改则 V3 行为不变。  
5. **禁止**：用死区压缩结果覆盖 `odds_asian`；禁止改 V3 默认 `settle_book` / 固定 0.95。

---

## 9. 分期里程碑（可验小包）

| 阶段 | 交付（可验收） | 后端 | 前端 |
|---|---|---|---|
| **D0** | 本文 + DDL 草案 + 存储估算（本文件） | 文档 | — |
| **D1** | 副本库建表；用 `probe` raw **导入 1–2 场**时间线+快照；查询「任意时刻水位」用例测试 | ✓ 解析器 / migration | — |
| **D2** | 兼容视图 / 双写 `odds_asian`；V3 compare 哈希与 0.3.8 **一致** | ✓ | 赔率页只读展示 channel（可先隐藏） |
| **D3** | 双通道快照规则单测（0–10 点场 + 白天场）；validate 指纹带 channel | ✓ | 对比页通道开关 |
| **D4** | 常驻 worker + 队列（staging）；限速 30/min；断点/429 演练；**不动**现网 cron 直至用户点头 | ✓ | — |
| **D5** | 历史回补 2025-10-25→今（夜间）；gzip 归档 + manifest | ✓ | 进度条/缺口列表（可选） |
| **D6** | 死区可选视图；同方案 rule vs actual 影子对比报告 | ✓ | 双通道叠加曲线 |

每阶段验收：给用户一场已知比赛的「时间线行数、快照表、与实测 README 数字一致、V3 未漂」。

---

## 10. 开放问题（不臆造）

1. ~~仓库对 22/23 点、0–10 点「规定中盘/临盘」的精确钟点~~ → **已定（2026-10-06）**：新 `rule` 对开赛 ≥23:00（含次日 0–10）为 **15:00 / 22:00**；旧 `rule_legacy` 仍为 0–10 场 **16:00 / 23:00**。实现时与仓库 `calc_collection_times` 对拍，确认边界（含恰好 23:00、10:00）写入单测。  
2. 竞彩 **让球胜平负** 采集通道（sporttery / 其它）待定。  
3. 平博/威廉/大小/1X2 的 history 密度仅部分实测，表中带「估算」的段数需在 D1 用更多 raw 校准。  
4. 是否永久双写 `odds_asian`，还是 D2 后退化为视图——等 V3 回归稳定再定。  
5. **真实支持率 / 必发成交量**：5Dollar 的 `betfair` **测不到**；若用户仍要成交占比，需另评估官方 Betfair Exchange API 或其它供应商（本次未测）。`support_proxy_odds` 暂不删除。  
6. 日用 21:00 版若部分场次尚无 `rule` close（临盘在 22:00 才齐），消息里用当时已有 mid/`actual` 快照并**标明通道**；是否事后补算「若当时已有终盘」对照列，待用户确认后再加。

---

## 11. 参考

- 本仓库文档：`v1_sqlite.sql`、`v1_7-water-tier-midpoint.md`、`data-conventions-v1.md`、`v1_6-strategy-stack-m5.md`  
- 实测：`../5dollar/probe-2026-10-06/README.md`；必发专项：`../5dollar/probe-2026-10-06/betfair/README.md`  
- Kimball Group：Declaring the Grain；Transaction Fact Tables  
- AVEVA Historian：Time and value deadbands for delta storage  
- Schneider Geo SCADA：Historic Data Compression / Significant Change  
- TDengine：Time series compression（RLE 等概述）

---

## 附录 S · 场次体量与 5Dollar 调度（2026-10-06 补充）

> 研究目录：`../research/fixture-volume-and-scheduling/`。以下数字来自该次拉数，**实数/估算以该目录 CSV `source` 列为准**。

### S.1 竞彩体量（实数）

- 自 **2025-10-25** 起至研究日：约 **4417** 场；日均 **13.55**，最大日 **53**（2025-11-08）。  
- 完整月场次约 **136–616**（淡季 6–7 月，旺季 11 月）。  
- 单日单 30 分钟桶峰值 **19** 场；开赛多在北京 **01:00–04:30** 与 **22:00–23:30**。

### S.2 全部场次

- 全部场次 **未做逐日全量**：近一年部分 Wed/Sat/Sun + 2023-01..2026-09 按月抽周估算；分桶因周末日更多而偏周末（见研究 README）。  
- 周末单日全库可达 **~1000–1400** 场（UTC 日窗实拉页数 10–14）。  
- **不建议**对全部联赛做逐场赔率常驻；日用以竞彩为主。

### S.3 限速与冗余

- **必发不纳入**调用规划与常驻队列。
- ：**（账户上限）**；常驻目标 **≤30（≥25% 冗余）**。  
- T−1h 快照在 T−65…T−55 均摊时，按历史峰值桶约 **1.90 次/分钟**，远低于预算。  
- 月调用（竞彩日用按 6 次/场）约 **2439**。  
- 历史回补竞彩约 **22085** 次，25/分钟约 **883** 分钟，只用空闲分钟。

### S.4 与双通道关系

- 规定通道新口径（≥23:00 → 15:00/22:00）、旧口径对照（0–10 → 16:00/23:00）、真实 T−8h/T−1h 分队列；白天重合可合并写入。  
- 详细表结构仍见正文 §3–§5；本附录只约束 **何时打满多少 QPS**。

### S.5 采集范围（竞彩赛事白名单，2026-10-06）

- 白名单研究目录：`../research/jingcai-competition-whitelist/`（`whitelist.csv`、`README.md`）。
- **范围**：只记竞彩网历史上开过的赛事；新开赛事日检设计见同目录 `daily_new_competition_check_design.md`（未上线）。
- **别名**：规范名优先用用户旧手工数据写法；种子见 `competition_alias_seed.*`；DDL 提议 `proposed_competition_alias_schema.sql`（**未执行**）。
- **调用量**：竞彩选出过的场次 ≈ 日均 13.6 × 30 × 6 ≈ **2400 次/月**（实数规划）；白名单赛事**全部场次**的月量见白名单 README §5（标估算/实数）。

