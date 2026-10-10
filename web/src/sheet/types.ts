/**
 * 数据表页类型。
 *
 * - `Be*`：后端批量接口 GET /table/matches 的真实返回（API 0.3.15，nested）。
 *   字段对照：docs/schema/v2_0-table-matches-api.md §13（以实际响应为准；早先草稿名也兼容）。
 * - `Api*`：前端内部的中间嵌套结构（批量接口先转成它）。
 * - `SheetRow`：表格内部用的平铺行，由 adapter.normalizeTableRow 统一从 Api 结构转换。
 */
import type { Direction } from '../api/types'

/** 亚盘机构：平博放第一（用户唯一有资金账户） */
export const AH_BOOKS = ['pinnacle', 'macau', 'crown', 'william'] as const
export type AhBook = (typeof AH_BOOKS)[number]
/** 0.3.22 并列亚盘路（不进 AH_BOOKS 共识／公司分歧）；升降盘与水位异动只在同路内比 */
export const AH_PARALLEL_BOOKS = ['macau_5df'] as const
export type AhParallelBook = (typeof AH_PARALLEL_BOOKS)[number]
/** 规则分路用的 book 键：主四家 + 并列路 */
export type AhRuleBook = AhBook | AhParallelBook
/**
 * 术语（用户定，见 v2_0-odds-phase-terminology.md）：
 * 初盘 = 各公司第一次开出的盘（open，带实际开盘时间）；中盘 = 开赛前 8h；临盘 = 开赛前 1h；
 * 即时盘口 = 比赛结束前接口抓到的任一快照（含赛前、滚球），中盘/临盘也属于即时快照。
 * 例外场（后端 phase_exception=true，前端不自己判定范围）：mid/close 为所属竞彩日规则时点（15:00 / 22:00），
 * mid_real/close_real 为真实 T−8h / T−1h。
 * 固定阶段用 open/mid/close，其余时刻的快照放 live[]（带 recorded_at），显示为「即时（抓取时间）」；
 * 竞彩日 11:10 那条由后端标 label=rule_1110，显示为「即时（11:10）」。
 */
export const PHASES = ['open', 'mid', 'close'] as const
export type Phase = (typeof PHASES)[number]
/** 欧赔机构（同亚盘四家） */
export const X1X2_BOOKS = AH_BOOKS
export type X1x2Book = AhBook

// ───────────────────────── 批量接口（嵌套）─────────────────────────

/** 0.3.16 初盘对象附带字段（ah/x1x2.{book}.open 与 jc_1x2.open）；前端只读，不自行判定可用性 */
export interface OpenMetaFields {
  /** first_tick | api_opening | legacy_import */
  open_basis?: string | null
  /** 本家本玩法最早一笔带时间戳报价（含自抓 11:10），无则 null */
  earliest_ts_quote_at?: string | null
  usable_at_mid?: boolean | null
  usable_at_close?: boolean | null
  unusable_reason?: string | null
  /** 0.3.17：earliest_ts_quote_at 是推定的（旧手工数据按竞彩日 11:10 采集） */
  ts_inferred?: boolean | null
  /** 0.3.17：open_basis=null 时的原因：no_open_data（该家该玩法无初盘）| after_as_of；有 open_basis 时为 null */
  open_basis_reason?: string | null
}

/** 0.3.16 返还率格子：empirical（历史中位数）| fixed_fallback（样本不足，用固定兜底阈值） */
export type BaselineMethod = 'empirical' | 'fixed_fallback'

/** 0.3.18 §13：同盘口、两格都是档位换算水位、档位变了时后端给出（值为档位 t，水位 = 0.70 + 0.05t） */
export interface TierCrossEnd {
  phase?: string | null
  /** 0.3.19：该阶段盘口（主让为正） */
  line?: number | null
  home?: number | null
  away?: number | null
}
export interface TierCross {
  /**
   * 0.3.19（hl_v0.3）：line = 初盘与临盘盘口不同（sides=["line"]，水位不比较，只悬停）；
   * water_tier = 同盘口、档位换算水位跨档（home/away 为档位 t，水位 = 0.70 + 0.05t）。旧数据没有 kind 时按 water_tier 处理。
   */
  kind?: 'line' | 'water_tier' | string | null
  from?: TierCrossEnd | null
  to?: TierCrossEnd | null
  /** 档位变了的一侧：home / away；kind=line 时为 ["line"] */
  sides?: string[] | null
}

/** 0.3.19 hl_v0.3：返还率兜底分位数（后端每个 ah / x1x2 格给；P10 / P90 字段可能还没有，有就用） */
export interface FallbackFields {
  fallback_p25?: number | null
  fallback_p10?: number | null
  fallback_p90?: number | null
  fallback_n?: number | null
  fallback_hl_eligible?: boolean | null
  /** 0.3.20 预留：后端判好的兜底档 light | medium | high（有就以它为准） */
  fallback_hl_level?: string | null
}

/** 0.3.19 即时（11:10）对照值 alt：water 亚盘 {home,away}、大小球 {over,under}、欧赔 {home,draw,away} */
export interface InstantAlt {
  line?: number | null
  water?: Record<string, number | null> | null
  /** 时间线表的 tick 时刻（alt 为时间线表时） */
  tick_at?: string | null
  /** 以下为预留：后端若在 alt 里标来源 / 抓取时刻就直接用；没有时按主值 origin 反推 */
  origin?: string | null
  odds_source?: string | null
  capture?: string | null
  captured_at?: string | null
  fetch_lag_min?: number | null
  /** 0.3.20 预留：自采超出 11:00–11:20 → true */
  out_of_window?: boolean | null
}

/** 0.3.19 即时（11:10）格的来源字段（只在 label=rule_1110 的条目上有值） */
export interface InstantFields {
  /** timeline | own_capture */
  origin?: string | null
  /** hist | live */
  odds_source?: string | null
  /** own | null */
  capture?: string | null
  captured_at?: string | null
  fetch_lag_min?: number | null
  merge_rule?: string | null
  alt?: InstantAlt | null
  instant_src_diff?: boolean | null
  /** 0.3.20 预留：自采超窗时主值退回时间线表的原因 */
  main_source_reason?: string | null
  own_capture_out_of_window?: boolean | null
}

export interface ApiAhPoint extends OpenMetaFields, FallbackFields, InstantFields {
  baseline_method?: string | null
  /** 主队视角盘口，主让为正；也兼容中文盘口字符串 */
  line: number | string | null
  home_water: number | null
  away_water: number | null
  /** 该阶段的目标时间（后端按配置给出；前端不推算） */
  target_at?: string | null
  /** 实际抓取时间 */
  recorded_at?: string | null
  /** actual | tier_midpoint | fallback_095 | null … */
  water_source?: string | null
  /** 0.3.22 与 water_source 同义 */
  water_src?: string | null
  water_censored?: boolean | null
  /** 0.3.22：macau_manual | macau_5df */
  book_lane?: string | null
  available?: boolean | null
  /** 0.3.18：仅中盘/临盘格；与上一阶段同盘口且两格都是真实水位 → true；任一非真实 → false；换盘/不可见 → null */
  water_move_eligible?: boolean | null
  /** 0.3.18：跨档信息；0.3.19 起只在临盘格给（初→临比），带 kind */
  tier_cross?: TierCross | null
  /** 0.3.19：初/临同盘、中盘换过盘 → true（照常比、照常上色，只加悬停） */
  tier_cross_mid?: boolean | null
  /** 0.3.20 占位场：开赛时间未确认，已抓取，暂不归中盘或临盘（默认 false） */
  phase_pending?: boolean | null
  /** 0.3.20 占位场：按占位开赛时间推算时 false，不参与特征计算（默认 true） */
  features_ok?: boolean | null
  /** 0.3.20 占位场：开赛时间在该目标时刻之后才确认 → true（默认 false） */
  phase_assign_late?: boolean | null
  /** 预留：亚盘返还率（后端算） */
  return_rate?: number | null
  /** 预留：该公司 as-of 滚动中位数基准 */
  return_rate_baseline?: number | null
  /** 缺数据时的来源标记 */
  source?: string | null
  /** 即时快照标签，如 rule_1110（竞彩日 11:10 规定快照） */
  label?: string | null
  /** 后端说明该格为何为空（如未到时点） */
  hidden_reason?: string | null
  /** 后端数据依据，如 legacy_import（旧库导入） */
  basis?: string | null
}

/** 某公司亚盘：初盘/中盘/临盘 + 其它时刻的即时快照 */
export type ApiAhBook = Partial<Record<Phase, ApiAhPoint | null>> & {
  /** 例外场的真实中盘/临盘（赛前 8h/1h）；普通场为 null。mid/close 存规则值 */
  mid_real?: ApiAhPoint | null
  close_real?: ApiAhPoint | null
  /** 即时快照（按 recorded_at 升序；没有为 []）。仅 include_live≠none 时返回 */
  live?: ApiAhPoint[] | null
}

export interface ApiX1x2Point extends OpenMetaFields, FallbackFields {
  baseline_method?: string | null
  /** hl_v0.2：欧赔格子 §4.2 暂无此字段；有则透传，无则按「水位来源未标注」处理 */
  water_source?: string | null
  n_avg?: number | null
  home: number | null
  draw: number | null
  away: number | null
  recorded_at?: string | null
  /** 预留：欧赔返还率 */
  return_rate?: number | null
  return_rate_baseline?: number | null
  /** 预留：凯利（胜/平/负） */
  kelly?: { home: number | null; draw: number | null; away: number | null } | null
  /** 凯利基准：平博去水 / 多家平均（退回时加角标） */
  kelly_base?: 'pinnacle' | 'multi_avg' | null
  source?: string | null
  /** 数据依据：legacy_import / api_opening / api_closing … */
  basis?: string | null
  /** 仅 api_opening / api_closing：抓取时刻（报价时刻未知） */
  fetched_at?: string | null
  /** 0.3.21 竞彩 jc_1x2 */
  complete?: boolean | null
  jc_1x2_incomplete?: boolean | null
  missing_reason?: string | null
  out_of_window?: boolean | null
  fetch_lag_min?: number | null
  available?: boolean | null
  alt?: Jc1x2Alt | null
  hidden_reason?: string | null
}

export interface ApiJcHhadPoint {
  goal_line?: number | null
  decision_line?: number | null
  current_line?: number | null
  post_decision_line_change?: boolean | null
  from_hist?: boolean | null
  line_rev?: number | null
  home?: number | null
  draw?: number | null
  away?: number | null
  complete?: boolean | null
  jc_1x2_incomplete?: boolean | null
  missing_reason?: string | null
  out_of_window?: boolean | null
  fetch_lag_min?: number | null
  available?: boolean | null
  source?: string | null
  basis?: string | null
  captured_at?: string | null
  target_at?: string | null
  recorded_at?: string | null
  alt?: Jc1x2Alt | null
}

/** 后端给出的各阶段目标时间（前端只展示，不推算） */
export interface ApiSchedule {
  mid_target_time?: string | null
  close_target_time?: string | null
  mid_real_target_time?: string | null
  close_real_target_time?: string | null
  live_rule_1110_target_time?: string | null
  /** 0.3.19 推迟场：例外场判定用的原定开赛时间 */
  kickoff_for_exception?: string | null
  /** 0.3.19 推迟场：mid / close 目标时刻依据 original | announced_new | original_ts_unknown */
  postpone_target_basis?: { mid?: string | null; close?: string | null } | null
}

export interface ApiTableMatch {
  id: string
  date: string
  scope: 'jingcai' | 'extra'
  jc_no?: string | null
  league?: string | null
  kickoff_at?: string | null
  kickoff_hour?: number | null
  /** 0.3.17：5df_1200 = 5DF 整 12:00 占位，开赛分钟未知 */
  kickoff_placeholder?: string | null
  /** 0.3.17：jingcai = 开赛时间取竞彩官方 */
  kickoff_source?: string | null
  /** 0.3.18：竞彩官方开赛时刻（只到整点） */
  kickoff_jc?: string | null
  /** 0.3.19：kickoff_jc 与开赛时间相差 ≥ 90 分钟 → true；null = 没查（不显示） */
  kickoff_jc_conflict?: boolean | null
  /** 0.3.19 推迟场（v2d3 有值；现网 null） */
  postponed?: boolean | null
  kickoff_original?: string | null
  kickoff_actual?: string | null
  postponed_announced_at?: string | null
  postpone_ts_unknown?: boolean | null
  postpone_delay_minutes?: number | null
  /** 预留（分析师 18:24）：实际开赛 − 原定开赛（分钟），只作信息展示 */
  kickoff_drift_min?: number | null
  /** 预留（分析师 18:20）：开赛时间疑似 12:00 占位 */
  kickoff_placeholder_suspect?: boolean | null
  /** 0.3.20 占位四字段（列优先，否则 extras；都没有 → 默认） */
  phase_pending?: boolean | null
  features_ok?: boolean | null
  phase_assign_late?: boolean | null
  /** 0.3.20：开赛时间曾变更次数；0 不写 */
  kickoff_rev?: number | null
  /** 0.3.20：kickoff_drift 附带 */
  kickoff_drift_basis?: string | null
  kickoff_drift_est_at?: string | null
  /** 0.3.19：人工复核（探针 209/210） */
  manual_review?: boolean | null
  manual_review_reason?: string | null
  /** 0.3.19：日核对原因 */
  daily_check?: string[] | null
  home: string
  away: string
}

export interface ApiTablePrediction {
  strategy: string
  direction: Direction | null
  stake: number | null
  confidence: number | null
  settle_book?: string | null
  produced_at: string | null
  /** 0.3.19：旧冻结 S2 / N4 条目「触发依据是换算水位」 */
  ledger_note?: string | null
  ledger_note_reason?: string | null
}

export interface ApiTableResult {
  home_goals: number
  away_goals: number
  wdl: '胜' | '平' | '负'
}

/** 0.3.19：void_postponed = 推迟超过 24 小时作废（澳门第 67/2018 号行政命令第十一条），盈亏为空 */
export type SettleCode = 'win' | 'win_half' | 'push' | 'lose_half' | 'lose' | 'no_bet' | 'void_postponed'

export interface ApiTableSettlement {
  code: SettleCode
  /** 盈亏（份） */
  pnl_units: number | null
  line?: number | null
  juice?: number | null
  juice_source?: string | null
  /** backend */
  source?: string | null
}

export interface ApiTableRow {
  match: ApiTableMatch
  ah: Partial<Record<AhBook | AhParallelBook, ApiAhBook | null>>
  x1x2: Partial<Record<X1x2Book, { open?: ApiX1x2Point | null; close?: ApiX1x2Point | null } | null>>
  jc_1x2: { open?: ApiX1x2Point | null; mid?: ApiX1x2Point | null; close?: ApiX1x2Point | null } | null
  /** 0.3.21 竞彩让球胜平负（决策时刻选线） */
  jc_hhad?: { open?: ApiJcHhadPoint | null; mid?: ApiJcHhadPoint | null; close?: ApiJcHhadPoint | null } | null
  prediction: ApiTablePrediction | null
  result: ApiTableResult | null
  settlement: ApiTableSettlement | null
  /** 例外场（后端判定）：mid/close 为规则值（15:00 / 22:00），另有 mid_real/close_real */
  phase_exception?: boolean | null
  schedule?: ApiSchedule | null
  /** 赛果/结算为空时的原因（如 未完场） */
  hidden_reason?: string | null
  /** 后端结算口径版本 */
  settlement_version?: string | null
  /** 0.3.19：结算为空的原因代码（manual_review 等） */
  settlement_hidden_reason?: string | null
  /**
   * 欧赔「接口收盘价」（basis=api_closing，报价时刻未知）。分析师定：不能当临盘用，
   * 不进 close、不算返还率/凯利、不参与高亮；只在完赛后作「收盘（时间未知）」参考列显示。
   */
  x1x2_closing_ref?: Partial<Record<X1x2Book, ApiX1x2Point | null>>
  /** 预留：多家平均去水概率（参考） */
  multi_avg_prob?: { home: number | null; draw: number | null; away: number | null } | null
  produced_at?: string | null
  as_of?: string | null
}

export interface ApiTableResponse {
  as_of: string | null
  items: ApiTableRow[]
}

// ───────────────────────── 后端真实返回（API 0.3.15）─────────────────────────

export interface BeAhCell extends OpenMetaFields, FallbackFields {
  baseline_method?: BaselineMethod | string | null
  line: number | null
  home_water: number | null
  away_water: number | null
  water_source: string | null
  /** 0.3.22 与 water_source 同义 */
  water_src?: string | null
  water_censored: boolean | null
  water_move_eligible?: boolean | null
  book_lane?: string | null
  tier_cross?: TierCross | null
  tier_cross_mid?: boolean | null
  phase_pending?: boolean | null
  features_ok?: boolean | null
  phase_assign_late?: boolean | null
  recorded_at: string | null
  /** §13 最终名；`target_time` 为早先草稿名（兼容） */
  target_at?: string | null
  target_time?: string | null
  basis?: string | null
  source: string | null
  available?: boolean
  /** after_as_of | no_data */
  hidden_reason?: string | null
  return_rate?: number | null
  return_rate_baseline?: number | null
}

export interface BeTriple {
  home: number | null
  draw: number | null
  away: number | null
}

export interface BeX1x2Cell extends BeTriple, OpenMetaFields, FallbackFields {
  baseline_method?: BaselineMethod | string | null
  /** §4.2 未定义；后端将来若加上则透传（hl_v0.2 返还率上色只认 actual） */
  water_source?: string | null
  /** 凯利多家平均（不含本家）参与的家数 */
  n_avg?: number | null
  complete?: boolean
  basis?: string | null
  fetched_at?: string | null
  recorded_at: string | null
  target_at?: string | null
  target_time?: string | null
  source?: string | null
  return_rate?: number | null
  return_rate_baseline?: number | null
  kelly?: BeTriple | null
  /** §13：multi_avg（早先草稿为 consensus，兼容） */
  kelly_base?: 'pinnacle' | 'multi_avg' | 'consensus' | null
  /** 0.3.21 竞彩 jc_1x2 附加 */
  jc_1x2_incomplete?: boolean | null
  missing_reason?: string | null
  out_of_window?: boolean | null
  fetch_lag_min?: number | null
  available?: boolean | null
  alt?: Jc1x2Alt | null
  hidden_reason?: string | null
}

/** 0.3.21 竞彩 11:10 超窗对照 */
export interface Jc1x2Alt {
  home?: number | null
  draw?: number | null
  away?: number | null
  captured_at?: string | null
  out_of_window?: boolean | null
  fetch_lag_min?: number | null
}

/** 0.3.21 /table/matches jc_hhad 格（决策时刻选线） */
export interface BeJcHhadCell extends BeTriple {
  goal_line?: number | null
  decision_line?: number | null
  current_line?: number | null
  post_decision_line_change?: boolean | null
  from_hist?: boolean | null
  line_rev?: number | null
  goal_line_raw?: string | null
  complete?: boolean | null
  jc_1x2_incomplete?: boolean | null
  missing_reason?: string | null
  out_of_window?: boolean | null
  fetch_lag_min?: number | null
  available?: boolean | null
  source?: string | null
  basis?: string | null
  captured_at?: string | null
  target_at?: string | null
  recorded_at?: string | null
  water_source?: string | null
  usable_at_mid?: boolean | null
  usable_at_close?: boolean | null
  hidden_reason?: string | null
  phase_pending?: boolean | null
  features_ok?: boolean | null
  phase_assign_late?: boolean | null
  alt?: Jc1x2Alt | null
}

export interface BeLiveEntry extends InstantFields {
  book: string
  market: string
  label: string | null
  target_at?: string | null
  target_time?: string | null
  recorded_at: string | null
  line: number | null
  home_water: number | null
  away_water: number | null
  water_source?: string | null
  is_inplay?: boolean
  source?: string | null
  over_water?: number | null
  under_water?: number | null
  phase_assign_late?: boolean | null
  phase_pending?: boolean | null
  features_ok?: boolean | null
}

type BePhases<T> = { open?: T | null; mid?: T | null; close?: T | null; mid_real?: T | null; close_real?: T | null }

export interface BeTableRow {
  match_id: string
  match: {
    match_id: string
    jc_id: string | null
    jc_no: number | null
    jingcai_date: string
    kickoff_at: string | null
    kickoff_placeholder?: string | null
    kickoff_source?: string | null
    kickoff_jc?: string | null
    kickoff_jc_conflict?: boolean | null
    postponed?: boolean | null
    kickoff_original?: string | null
    kickoff_actual?: string | null
    postponed_announced_at?: string | null
    postpone_ts_unknown?: boolean | null
    postpone_void_check?: string | null
    postpone_delay_minutes?: number | null
    kickoff_drift_min?: number | null
    kickoff_placeholder_suspect?: boolean | null
    phase_pending?: boolean | null
    features_ok?: boolean | null
    phase_assign_late?: boolean | null
    kickoff_rev?: number | null
    kickoff_drift_basis?: string | null
    kickoff_drift_est_at?: string | null
    manual_review?: boolean | null
    manual_review_reason?: string | null
    daily_check?: string[] | null
    instant_src_diff?: boolean | null
    /** 预留：新字段可能先放在 extras */
    extras?: Record<string, unknown> | null
    scope: string
    league: string | null
    home_team: string | null
    away_team: string | null
  }
  /** 预留：行级 extras（kickoff_placeholder_suspect / phase_assign_late 可能放这里） */
  extras?: Record<string, unknown> | null
  phase_exception: boolean
  /** 0.3.17 行上字段（也兼容放在 match 里） */
  kickoff_placeholder?: string | null
  kickoff_source?: string | null
  kickoff_jc?: string | null
  schedule?: ApiSchedule | null
  ah: Record<string, BePhases<BeAhCell> | null>
  x1x2?: Record<
    string,
    | (BePhases<BeX1x2Cell> & {
        /** 0.3.16：欧赔接口收盘价，仅赛果可见后出现；参考用，不进任何计算 */
        api_closing?: (BeTriple & { label?: string; display_name?: string; fetched_at?: string | null; reference_only?: boolean }) | null
      })
    | null
  >
  x1x2_base?: Record<
    string,
    { pinnacle?: BeTriple | null; multi_avg?: BeTriple | null; consensus?: BeTriple | null } | null
  >
  /** 行级多家平均去水概率（= x1x2_base.close.multi_avg） */
  multi_avg_prob?: (BeTriple & { n_books?: number }) | null
  jc_1x2?: Record<string, BeX1x2Cell | null>
  /** 0.3.21 */
  jc_hhad?: Record<string, BeJcHhadCell | null>
  live?: BeLiveEntry[]
  prediction: {
    direction: string | null
    strategy: string | null
    settle_book?: string | null
    confidence: number | null
    stake: number | null
    produced_at: string | null
    ledger_note?: string | null
    ledger_note_reason?: string | null
  } | null
  prediction_hidden_reason?: string | null
  produced_at?: string | null
  as_of?: string | null
  result: { home_goals: number; away_goals: number; wdl: string | null } | null
  result_hidden_reason?: string | null
  /** 行级 = result_hidden_reason */
  hidden_reason?: string | null
  settlement: {
    settlement_version?: string
    line?: number | null
    juice?: number | null
    juice_source?: string | null
    /** §13 最终名，与前端 SettleCode 同值 */
    code?: SettleCode
    /** 早先草稿名（half_win / half_loss / loss），兼容 */
    result_code?: string
    /** void_postponed 时为 null */
    pnl_units: number | null
    source?: string | null
  } | null
  settlement_hidden_reason?: string | null
}

/** 0.3.19 响应顶层：本页日核对计数 */
export interface DailyCheckSummary {
  scope?: string
  n_matches?: number
  by_reason?: Record<string, number>
  instant_src_diff_cells_in_live?: number
  own_1110_out_of_window_cells?: number
  [k: string]: unknown
}

export interface BeTableResponse {
  api_version?: string
  config_version?: string
  daily_check_summary?: DailyCheckSummary | null
  as_of: string
  total: number
  count: number
  items: BeTableRow[]
}

/** include_live 取值：none（默认）| rule_1110（只要 11:10 规定快照）| all */
export type IncludeLive = 'none' | 'rule_1110' | 'all'

// ───────────────────────── 表格内部（平铺）─────────────────────────

/** 初盘来源与可用性（只读后端标记） */
export interface OpenInfo {
  basis: string | null
  earliestTsQuoteAt: string | null
  usableAtMid: boolean | null
  usableAtClose: boolean | null
  unusableReason: string | null
  tsInferred: boolean
}

export interface AhCell {
  /** 仅 open 格有 */
  openInfo: OpenInfo | null
  baselineMethod: string | null
  /** 解析后的数值盘口（主让为正）；null=缺 */
  line: number | null
  /** 原始值（便于提示） */
  lineRaw: string | null
  /** 非 0.25 整倍数等非标准盘口 */
  lineNonStandard: boolean
  hw: number | null
  aw: number | null
  waterSource: string | null
  /** 0.3.22：macau_manual | macau_5df 等 */
  bookLane: string | null
  waterCensored: boolean
  /** hl_v0.3：后端判定的初→临水位变动可比（只在临盘格）；涂色只看它 */
  waterMoveEligible: boolean | null
  tierCross: TierCross | null
  /** hl_v0.3：初/临同盘、中盘换过盘（只悬停，照常上色） */
  tierCrossMid: boolean
  /** hl_v0.3 返还率兜底（后端每格给） */
  fallback: FallbackInfo | null
  /** 0.3.19 即时（11:10）来源；其它格为 null */
  instant: InstantInfo | null
  /** 0.3.20 占位场三字段（格子级；比赛级另有同名字段） */
  phasePending: boolean
  featuresOk: boolean
  phaseAssignLate: boolean
  targetAt: string | null
  recordedAt: string | null
  returnRate: number | null
  returnRateBaseline: number | null
  /** 即时快照标签（如 rule_1110） */
  label: string | null
  /** 数据来源（后端 basis / source，如 legacy_import、odds_asian） */
  basis: string | null
  source: string | null
}

/** hl_v0.3 返还率兜底（只读后端）；p10 / p90 为 null 表示接口还没给 */
export interface FallbackInfo {
  p25: number | null
  p10: number | null
  p90: number | null
  n: number | null
  eligible: boolean
  /** 0.3.20 预留：后端判好的档 */
  level: string | null
}

export interface InstantInfo {
  origin: string | null
  oddsSource: string | null
  capture: string | null
  capturedAt: string | null
  fetchLagMin: number | null
  alt: InstantAlt | null
  srcDiff: boolean | null
  mainSourceReason: string | null
}

export interface X1x2Cell {
  fallback: FallbackInfo | null
  openInfo: OpenInfo | null
  baselineMethod: string | null
  /** 凯利多家平均（不含本家）家数 */
  nAvg: number | null
  home: number | null
  draw: number | null
  away: number | null
  recordedAt: string | null
  /** hl_v0.2：返还率上色只认 actual；后端欧赔格子目前不带该字段 → null（按未标注处理） */
  waterSource: string | null
  returnRate: number | null
  returnRateBaseline: number | null
  kelly: { home: number | null; draw: number | null; away: number | null } | null
  kellyBase: 'pinnacle' | 'multi_avg' | null
}

/** 0.3.21 竞彩胜平负格（可空壳：msi_empty 仍保留，供灰字） */
export interface Jc1x2Cell extends X1x2Cell {
  complete: boolean | null
  incomplete: boolean
  missingReason: string | null
  outOfWindow: boolean
  fetchLagMin: number | null
  available: boolean | null
  source: string | null
  alt: Jc1x2Alt | null
}

/** 0.3.21 竞彩让球胜平负（主格用决策线） */
export interface JcHhadCell {
  goalLine: number | null
  decisionLine: number | null
  currentLine: number | null
  postDecisionLineChange: boolean
  fromHist: boolean
  lineRev: number
  home: number | null
  draw: number | null
  away: number | null
  complete: boolean | null
  incomplete: boolean
  missingReason: string | null
  outOfWindow: boolean
  fetchLagMin: number | null
  available: boolean | null
  source: string | null
  recordedAt: string | null
  alt: Jc1x2Alt | null
}

export interface SheetRow {
  id: string
  date: string
  scope: 'jingcai' | 'extra'
  jcNo: string
  league: string
  kickoffAt: string | null
  kickoffLabel: string
  /** 开赛时间悬停注（占位值 / 竞彩官方来源 / 推迟 / 漂移）；无则 null */
  kickoffNote: string | null
  /** 0.3.19：只有后端 kickoff_jc_conflict=true 才为 true（开赛格挂「核」） */
  kickoffJcConflict: boolean
  /** 竞彩官方时刻（只到整点）；冲突悬停用 */
  kickoffJc: string | null
  /** 0.3.19 推迟场 */
  postponed: boolean
  /** 0.3.20 占位场：开赛时间未确认（开赛格灰字「待归阶段」） */
  phasePending: boolean
  /** 0.3.20：按占位开赛时间推算、不参与特征（默认 true；false 才提示） */
  featuresOk: boolean
  /** 0.3.20：开赛时间在目标时刻之后才确认 */
  phaseAssignLate: boolean
  /** 0.3.20：开赛时间曾变更次数；0 = 没变过 */
  kickoffRev: number
  /** 推迟场目标时刻依据（mid / close）：original | announced_new | original_ts_unknown */
  postponeTargetBasis: { mid: string | null; close: string | null } | null
  /** 0.3.19 人工复核（探针场） */
  manualReview: boolean
  manualReviewReason: string | null
  /** 0.3.19 结算为空的后端原因代码 */
  settlementHiddenReason: string | null
  /** 0.3.19 预测台账备注（旧冻结 S2/N4「触发依据是换算水位」） */
  ledgerNote: string | null
  home: string
  away: string
  matchup: string

  ah: Record<
    AhBook,
    Record<Phase, AhCell | null> & {
      midReal: AhCell | null
      closeReal: AhCell | null
      live: AhCell[]
      /** 空格子的后端原因（after_as_of | no_data），键为 open/mid/close/midReal/closeReal */
      hidden: Partial<Record<Phase | 'midReal' | 'closeReal', string>>
    }
  >
  /**
   * 0.3.22 预留：macau_5df 并列亚盘（5DF 真实水位）。
   * 接口无 `ah.macau_5df` 时为 null；有则与手工澳门列并列，规则只在同路内比。
   */
  ahMacau5df: (Record<Phase, AhCell | null> & {
    midReal: AhCell | null
    closeReal: AhCell | null
    live: AhCell[]
    hidden: Partial<Record<Phase | 'midReal' | 'closeReal', string>>
  }) | null
  /** 例外场（后端 phase_exception）：中盘/临盘为规则时点，格子右上角标「规」 */
  phaseException: boolean
  /** 后端给出的阶段目标时间（只展示） */
  schedule: {
    midTarget: string | null
    closeTarget: string | null
    midRealTarget: string | null
    closeRealTarget: string | null
  }
  x1x2: Record<X1x2Book, { open: X1x2Cell | null; close: X1x2Cell | null }>
  /** 欧赔接口收盘价（api_closing，参考，只在完赛后显示；不参与任何规则） */
  x1x2ClosingRef: Record<X1x2Book, { home: number | null; draw: number | null; away: number | null; fetchedAt: string | null } | null>
  /** 竞彩胜平负；缺键时三角为 null；有键但 MSI 空仍可能有 incomplete/msi 壳 */
  jc: { open: Jc1x2Cell | null; mid: Jc1x2Cell | null; close: Jc1x2Cell | null }
  /** 接口是否带了 jc_1x2 对象（现网缺键 → false，不出列也可） */
  jcPresent: boolean
  /** 0.3.21 竞彩让球胜平负 */
  jcHhad: { open: JcHhadCell | null; mid: JcHhadCell | null; close: JcHhadCell | null }
  jcHhadPresent: boolean
  /** 0.3.17 初盘为空的后端原因（open_basis_reason）：no_open_data | after_as_of | 其它原样；有初盘为 null */
  openReason: { ah: Record<AhBook, string | null>; x1x2: Record<X1x2Book, string | null>; jc: string | null }
  multiAvgProb: { home: number | null; draw: number | null; away: number | null } | null

  strategy: string | null
  direction: Direction | null
  stake: number | null
  confidence: number | null
  producedAt: string | null

  finished: boolean
  hiddenReason: string | null
  score: string | null
  wdl: '胜' | '平' | '负' | null
  settleCode: SettleCode | null
  pnlUnits: number | null
  settleSource: string | null
  /** 后端结算口径版本（如 ah_v4_water_midpoint） */
  settleVersion: string | null

  asOf: string | null
}

export type ViewId = 'snapshot' | 'health' | 'review'

export const VIEW_LABEL: Record<ViewId, string> = {
  snapshot: '盘口快照',
  health: '赔率健康',
  review: '预测复盘',
}

export const BOOK_LABEL: Record<AhBook, string> = {
  pinnacle: '平博',
  macau: '澳门',
  crown: '皇冠',
  william: '威廉',
}

/** 并列路中文名（0.3.22） */
export const PARALLEL_BOOK_LABEL: Record<AhParallelBook, string> = {
  macau_5df: '澳门5DF',
}


export const PHASE_LABEL: Record<Phase, string> = {
  open: '初盘',
  mid: '中盘',
  close: '临盘',
}

/**
 * 例外场说明（不写判定窗口；判定由后端 phase_exception 给出，口径见术语文档末节「例外场判定改为按竞彩编号」，
 * 后端指纹 exception_rule 只作口径记录，前端不显示）。
 */
export const EXCEPTION_DESC = '例外场：所属竞彩日当晚 23:00 及以后开赛（含次日开赛），中盘/临盘取该竞彩日 15:00 / 22:00 规则时刻'

/** 阶段说明；具体时刻由后端给出，悬停格子看「目标时间 / 抓取时间」 */
export const PHASE_HINT: Record<Phase, string> = {
  open: '初盘：各公司第一次开出的盘（按公司分别取），悬停看实际开盘时间',
  mid: '中盘：开赛前 8h 的即时快照；例外场（所属竞彩日当晚 23:00 及以后开赛）取该竞彩日 15:00，格子右上角标「规」',
  close: '临盘：开赛前 1h 的即时快照；例外场（所属竞彩日当晚 23:00 及以后开赛）取该竞彩日 22:00，格子右上角标「规」',
}

export const SETTLE_LABEL: Record<SettleCode, string> = {
  win: '赢',
  win_half: '赢半',
  push: '走',
  lose_half: '输半',
  lose: '输',
  no_bet: '不下注',
  void_postponed: '推迟作废',
}

/** 0.3.19 日核对原因 → 中文（状态栏「待核对」标签悬停用）；未知代码原样显示 */
export const DAILY_CHECK_LABEL: Record<string, string> = {
  kickoff_jc_conflict: '竞彩官方时刻与开赛时间相差超过 90 分钟',
  manual_review: '人工复核中',
  instant_src_diff: '11:10 自采与时间线表不一致',
  own_1110_out_of_window: '11:10 自采超出 11:00–11:20（采集延迟），主值改用时间线表',
  postpone_ts_unknown: '推迟消息发布时间未知',
  postpone_void_pending: '推迟场作废与否待核对',
}
