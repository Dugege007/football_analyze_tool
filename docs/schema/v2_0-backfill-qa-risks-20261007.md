# 5DF 多庄历史补数 · 数据质量审计与风险（2026-10-07）

> 审计时刻：**2026-10-07T14:38:23+08:00**（队列仍在 supervise 拉取中，数字为该时刻快照）。  
> 硬约束遵守：**未调用 5DF API**；**未触碰现网/生产 DB 写路径**；只读队列根目录与 schema。  
> 对照铁律：[`v2_0-as-of-betting-iron-rules.md`](./v2_0-as-of-betting-iron-rules.md)。  
> 机器可读摘要：[`v2_0-backfill-qa-summary.json（未公开）`](./v2_0-backfill-qa-summary.json（未公开）)。

---

## 0. 一句话结论

P1 raw 落盘质量整体可用（fill 错误率 ~0.04%，抽样 20/20 文件齐全且赛前 tick `recorded_at ≤ kickoff`，**澳门 mid 覆盖约 97.6% / 双庄约 97.5%**），但：

1. **年窗 5034 仅完成约一半 raw**（done≈2407，unmapped=2580）；  
2. **CSL 编号期（≥2025-10-25）仍有 ~2275 场未映射**——主阻塞不是 worker；  
3. **尚未入库研究库**（只写 `raw/`），后端无法直接消费 mid；  
4. neighbor ±1 映射双日条目、hist 分页未翻页、运维空窗/双 worker 等会污染后续入库。

---

## 1. 已检查 / 未检查

### 已检查

| 项 | 方法 | 结果摘要 |
|---|---|---|
| 队列计数 done/pending/unmapped vs 5034 | `queue/*.jsonl` + `state.json` | 5040 行；唯一 `match_uid`=5037；相对 5034 **+6 行 / +3 uid** |
| done 内 `fixture_id` 重复 | Counter | **3** 个 fixture 各写了 2 行（双 worker 同秒 cache_hit） |
| fill_report ok/error | `logs/fill_report.jsonl` | ok=2410 / err=1（`rate_reserve`）；错误率 **0.0415%** |
| 调用形态 | fill calls | odds 200/cache；mac hist 200 / legacy_copy(155) / cache；pin 全 200 |
| raw 文件存在性（全量 done） | 路径约定 `odds/{fid}_macauslot_pinnacle.json` + `hist/{fid}_{book}_asian.json` | **2407/2407 三文件齐全** |
| open/close（odds snap） | asian_handicap.opening/closing | 澳门 2372 有、35 缺；平博 2402 有、5 缺 |
| mid 可推导覆盖（rule 通道） | hist ticks + CSL `kickoff_at` | 澳门 **97.59%**；平博 **98.96%**；双庄 **97.47%**；任一 **99.09%** |
| mid 泄漏（`recorded_at > kickoff`） | 全量扫描 | **0** |
| 抽样 20 done | seed=20261007 | 20/20 文件；20/20 双庄末条赛前 tick ≤ kickoff |
| jc_id 同日碰撞 | csl_fixture_map `(date,jc_norm)` | **0** |
| neighbor ±1 | `join_method=neighbor_jc` | map 内 **1078** 行；同 fixture 双日条目；**队伍冲突 0** |
| unmapped 时代切分 | vs `csl_number_from=2025-10-25` | 前 305（11.8%）；**当日及之后 2275（88.2%）** |
| 运维痕迹 | supervise / p1 日志 / `DUAL_WRITE.off` | rate_reserve、DNS、双 worker exit 143、空 map 停、双写关 |
| 分页 `has_more` | 全 hist 扫描 | **26** 个文件 `has_more=true`（均为 count=500，多为 pinnacle） |
| 现网库只读探针 | `match-analysis-api/data/app.db` RO | matches=177；**macau mid 行=0**；无 multibook 入库迹象 |

### 未检查 / 诚实缺口

| 项 | 原因 |
|---|---|
| 5DF API 实时对账 | 任务禁止调用 |
| 生产 DB 写入校验以外的深挖 | 禁止触碰 prod 写；仅 RO 探针现网 app.db |
| 研究副本 v2d1/v2d2/v2d3 当前文件 | 本机路径下未见对应 `app.db`（文档记载的 v2d3 澳门 158 mid 为既有小批，**非本队列 2400+ 场**） |
| mid 与「规定 15:00/22:00」逐场人工核对 | 用了规则通道启发式；未逐场对齐竞彩日挂载边界的全部边角 |
| 错连（邻日把 A 场接到 B 场）语义抽检 | 仅结构启发式（同 fixture 队伍一致）；**未**人工核对邻居 1078 条是否选对竞彩日 |
| Bet365 / crown / william 等其它庄 | P1 范围仅 macauslot+pinnacle asian |
| hist 第 2+ 页内容 | worker 未翻页；无法知截断后丢失了哪些临盘 tick |
| 入库字段映射端到端 | 尚无 ingest 脚本跑通本队列 → `odds_timeline` |

---

## 2. 抽样数字表

### 2.1 队列 vs 宇宙

| 指标 | 数值 |
|---:|---:|
| sporttery 年窗宇宙 | **5034** |
| done | **2407** |
| pending | **53** |
| unmapped | **2580** |
| 三队列行数之和 | **5040**（Δ=+6） |
| 唯一 `match_uid` 并集 | **5037**（Δ=+3） |
| done 唯一 `fixture_id` | **2404** |
| done 重复 `fixture_id` | **3**（`3634779529`,`2956446577`,`346256746`） |
| 映射完成率（done+pending）/5034 | **~48.9%** |
| raw 完成率 done/5034 | **~47.8%** |

> Δ 解释：3 条 done 重复写入导致行数虚高；另有约 3 个 `match_uid` 相对 5034 宇宙多出（neighbor/建队边界），入库前须去重并对齐 sporttery 主键。

### 2.2 fill_report

| 指标 | 数值 |
|---|---:|
| 行数 | 2411 |
| ok | 2410 |
| error | **1**（`rate_reserve`，fatal） |
| dry_run | 3 |
| 错误率 | **0.0415%** |
| hist_macauslot legacy_copy | 155 |
| hist_macauslot http_200 | 2253 |
| hist_pinnacle http_200 | 2410 |

### 2.3 raw / open·close / mid

| 指标 | 澳门 macauslot | 平博 pinnacle | 说明 |
|---|---:|---:|---|
| hist 文件（随 done） | 100% | 100% | 与 done 一一对应 |
| odds snap open+close 有值 | 2372 | 2402 | 缺 35 / 5 |
| **可推导 mid（rule）** | **2349（97.59%）** | **2382（98.96%）** | 见定义↓ |
| 双庄皆有 mid | **2346（97.47%）** | — | |
| 任一庄有 mid | **2385（99.09%）** | — | |
| 两庄皆无 mid | 22 | — | 多缺 kickoff 或无赛前 tick |
| 无赛前 tick | 37 | 6 | |
| mid `tick_age>2h`（approx） | 2096（占有 mid 的 **89.23%**） | 1228 | 澳门稀 tick，预期内 |
| mid 晚于 kickoff（泄漏） | **0** | **0** | 铁律抽样友好 |
| 缺 kickoff 元数据 | 19（done 内；多来自早期 macau map、不在 csl_map） | | mid 无法按 rule 钉时刻 |

**mid 定义（本审计）**：取 hist 赛前 tick 中 `recorded_at ≤ rule_mid_at` 的最后一条；`rule_mid_at` = 若开赛 BJ 时点 ≥23 或 ＜11 则用竞彩日 **15:00 BJ**，否则 **kickoff−8h**（对齐 `v2_0-odds-timeline-storage-design` 规定通道简化版）。

### 2.4 抽样 20（seed=20261007）

| 检查 | 通过 |
|---|---:|
| odds + hist_mac + hist_pin 三文件存在 | **20/20** |
| 双庄末条赛前 tick `recorded_at ≤ kickoff` | **20/20** |

### 2.5 映射 / 时代

| 指标 | 数值 |
|---|---:|
| csl_fixture_map items | 3516 |
| 其中唯一 fixture_id | 2438 |
| `join_method=neighbor_jc` | **1078**（offset −1:1077，+1:1） |
| state.`neighbor_joined_in_universe` | 19 |
| 同日+jc_norm 碰撞 | **0** |
| 同 fixture 队伍冲突 | **0** |
| unmapped ＜2025-10-25 | **305（11.82%）** |
| unmapped ≥2025-10-25 | **2275（88.18%）** |
| done 全部 ≥2025-10-25 | 是 |
| pending map_source | exact CSL 28 + neighbor 25 |

### 2.6 分页

| 指标 | 数值 |
|---|---:|
| `pagination.has_more=true` 的 hist 文件 | **26** |
| 典型 | pinnacle，`count=500` 截断 |

---

## 3. 发现的问题（按严重度）

### 高

1. **CSL 编号期内大量未映射（~2275）**  
   unmapped 主因标签均为 `no_5df_fixture_id`；**不是**「……… 前无编号」所能解释（前时代仅 …）。覆盖瓶颈在日窗映射命中率 / 缓存缺口 / supervise 空 map 早停，而非拉数 worker。

2. **本队列尚未入库研究库**  
   只落 `raw/odds|hist|csl`；`DUAL_WRITE.off`；现网 `odds_asian` 仍无 macau mid。后端若直接读现网会误判「无数据」。需独立 ingest → `odds_timeline` / 快照表（副本），并带 `recorded_at`、`channel`、`approx`。

### 中高

3. **neighbor ±1 双日条目（1078）**  
   同一 `fixture_id` 在 map 中出现两个 `jingcai_date`（差 1 天、同 `jc_norm`、队伍一致）。结构上未见队伍错绑，但 **选错竞彩日会把特征挂到错误 match_uid**。state 称宇宙内 neighbor 仅 19——其余多为 map 膨胀，建队/入库必须有「权威竞彩日」消歧规则。

4. **done 重复写入（3 fixture）**  
   同 `match_uid` 在数毫秒内写两行（一端 `cache_hit`），与文档记载的 **双 worker / 执行器打断（exit 143）** 一致。幂等键未在 append 时强制唯一。

### 中

5. **澳门 mid 大量 `approx`（~89%）**  
   稀 tick 导致 `tick_age>2h`；应用侧必须保留 `approx` + `tick_age_hours`，禁止与密 tick 庄家无差别宣称。另有 37 场澳门无赛前 tick → 无 mid。

6. **hist 未翻页（26 文件 has_more）**  
   平博密 tick 场次可能丢**临近开赛**的变化；对 mid（较早）影响较小，对 **close / T−1h** 有风险。

7. **运维空窗与误停**  
   - `rate_reserve`（Remaining≤5）干净停：1 次已见于 fill。  
   - DNS `URLError` 中断批次。  
   - supervise：`pending empty` / 连续空 map 后 `DONE`；14:26 重启后曾因 map 无新 pending 误判结束（见 interval 分析）。  
   - 上午结束后 **~5.7h** 无进度（08:41–14:26）。

8. **队列行数 vs 5034 不一致（+6 / uid +3）**  
   入库宇宙必须以 sporttery `match_uid` 为权威去重，不能信任三文件行数之和。

### 低 / 信息

9. **字段命名落差**：API slug=`macauslot`/`pinnacle`；库内习惯 `book=macau`；`phase∈{open,mid,close}` 与 timeline `point`+`channel` 并存——ingest 须显式映射。  
10. **19 场缺 kickoff**：早期 macau map 场次未进 csl_map，mid 时刻无法钉死。  
11. **legacy_copy 155**：从旧 macau-mid 目录复用 hist，需确认与当前 API 字段一致（抽样未见结构性问题）。

---

## 4. Schema / 后端风险（BE）

| 风险 | 说明 |
|---|---|
| **未 ingest** | 研究库无本队列 2400+ 场 timeline；现网仍 177 场底座 |
| **mid 非 API 直出** | odds snap 仅 opening/closing（+inplay）；**mid 必须**从 hist 按 `recorded_at ≤ 目标时刻` 推导（铁律 §1） |
| **book 命名** | `macauslot`→`macau`；勿把 slug 原样进旧 `odds_asian` |
| **channel** | 须区分 `rule` / `actual`（及对照 `rule_legacy`）；禁止用赛后 tick |
| **approx** | 澳门高比例 approx；指纹/验收须计入 |
| **双写** | `DUAL_WRITE.off` 在位；ingest 只许副本；禁止打开现网双写 |
| **主键** | 建议 `(match_uid 或 sporttery_id, book, market, channel, point)`；fixture_id 作外部键 |

---

## 5. 建议后端配合的验收项（checklist）

### A. 入库前门禁

- [ ] 确认读取源为 `5df-multibook-history-queue/raw/**`，**不是**现网 `app.db`
- [ ] `DUAL_WRITE` / `DUAL_WRITE_ODDS_ASIAN` 仍为关；目标库为研究副本
- [ ] 以 sporttery `match_uid` 去重；拒绝 done 内重复 fixture 的双行（保留最新/完整 calls）
- [ ] 三文件齐全：odds snap + hist macauslot + hist pinnacle
- [ ] `success==1` 且 ticks 可解析

### B. as-of / mid 铁律

- [ ] mid/close/open 均满足采用 tick 的 `recorded_at ≤` 目标时刻
- [ ] 赛后 / inplay（`minute`+比分）不得进入 open/mid/close
- [ ] 无 kickoff 的场次：拒绝静默造 mid，或标 `mid_unverifiable`
- [ ] `tick_age_hours>2` → `approx=true`（澳门预期高）
- [ ] 抽样 ≥50 场：人工或脚本核对 mid 未使用本场赛果

### C. 映射正确性

- [ ] 权威竞彩日消歧：neighbor 双日条目只保留一条 `match_uid`
- [ ] 抽检 neighbor 样本（建议 ≥30）：主客队、联赛、开赛与 sporttery 一致
- [ ] 同日 `jc_norm` 不得映射到两个 fixture（本审计为 0，回归保持）
- [ ] unmapped≥2025-10-25 清单不进入「已完成年窗」宣称

### D. 分页与 close

- [ ] `has_more=true` 的 26 场：补翻页或在 close/T−1h 上标 `hist_truncated`
- [ ] close 优先用「≤kickoff 最后赛前 tick」，不要只用 odds snap.closing 冒充 timeline close（可作对照列）

### E. 覆盖率门禁（建议）

- [ ] 宣称「P1 raw 可用子集」：done 且双庄 mid 可推导（当前 ~97.5%）
- [ ] 宣称「年窗完成」：**禁止**（unmapped 2580，CSL 期仍 2275）
- [ ] 入库后：`odds_timeline` 去重场次数 ≈ 唯一 fixture；macau mid 行数 ≈ 有 mid 的 done

### F. 运维配合

- [ ] 单 worker 锁；禁止并行 `run_queue`
- [ ] supervise 空 map 勿一次退出；与 interval 分析一致改为长驻 + 小时看门狗
- [ ] 健康检查：error_rate、pending 下降、`DUAL_WRITE.off`、last_progress mtime

---

## 6. Top 5 风险（给主会话）

| # | ID | 严重度 | 摘要 |
|---|---|---|---|
| 1 | `unmapped_csl_era` | 高 | ≥2025-10-25 仍约 **2275** 场 unmapped，年窗完成度 ~48% |
| 2 | `not_ingested` | 高 | raw 未进研究库；BE 无 timeline；mid 须 as-of 推导 |
| 3 | `neighbor_join_dup` | 中高 | map 内 **1078** neighbor 双日行；错连/重复 uid 风险 |
| 4 | `macau_sparse_approx` | 中 | 澳门 mid **~89%** 需 approx；37 场无赛前 tick |
| 5 | `ops_pagination` | 中 | 双 worker/杀进程/rate_reserve/空 map 停；**26** 场 hist 未翻页 |

### Mid 覆盖率（among done，本快照）

| 范围 | 覆盖率 |
|---|---:|
| 澳门 macauslot | **97.59%** |
| 平博 pinnacle | **98.96%** |
| 双庄皆有 | **97.47%** |
| 任一庄 | **99.09%** |

---

## 7. 参考路径

- 队列根：`$ODDS_DATA_DIR/backfill/5df-multibook-history-queue/`
- 铁律：`schema/v2_0-as-of-betting-iron-rules.md`
- 时间线设计：`schema/v2_0-odds-timeline-storage-design.md`
- 路线/时刻表：`v2_0-history-backfill-roadmap.md`、`v2_0-history-backfill-timetable.md`
- 运维间隔：`v2_0-backfill-interval-analysis-20261007.md（未公开）`
- 摘要 JSON：`schema/v2_0-backfill-qa-summary.json（未公开）`
