/**
 * 数据表页取数：只走批量接口 GET /api/table/matches（Vite 代理去掉 /api → 后端 /table/matches）。
 * 字段对照见 docs/schema/v2_0-table-matches-api.md §13（以实际响应为准，早先草稿名兼容）。
 * 阶段时刻不在前端推算：后端给 target_at / recorded_at / phase_exception / schedule.*_target_time / live[].label。
 * 结算以后端 settlement 为准（source="backend"）；逐场拼装兜底与前端估算结算已停用（旧实现见备份 /tmp/sheet-bak-hl_v0.1-pre/）。
 * 不使用假数据；缺的字段一律 null（不编造）。
 */
import { apiBase, metaMismatch, type DbMeta, type DbSource } from '../api/dataSource'
import dayjs from 'dayjs'
import type { Direction } from '../api/types'
import { parseHandicap } from './handicap'
import type {
  AhBook,
  AhCell,
  ApiAhPoint,
  ApiJcHhadPoint,
  ApiTableResponse,
  ApiTableRow,
  ApiX1x2Point,
  BeAhCell,
  BeJcHhadCell,
  BeTableResponse,
  BeTableRow,
  BeX1x2Cell,
  DailyCheckSummary,
  FallbackFields,
  FallbackInfo,
  IncludeLive,
  InstantFields,
  InstantInfo,
  Jc1x2Alt,
  Jc1x2Cell,
  JcHhadCell,
  OpenInfo,
  OpenMetaFields,
  SettleCode,
  SheetRow,
  X1x2Cell,
} from './types'
import { AH_BOOKS } from './types'

export interface LoadParams {
  dateFrom: string
  dateTo: string
  scope: 'jingcai' | 'extra' | 'all'
  strategy: string
  /** 即时快照：none | rule_1110（「即时（11:10）」列）| all；只在「盘口快照」视图请求 */
  includeLive: IncludeLive
  /** 数据源：现网 /api（8787）或副本 /api-replica（8788）；默认现网 */
  source?: DbSource
}

export interface LoadResult {
  rows: SheetRow[]
  asOf: string | null
  /** 后端口径版本（config_version） */
  backendConfigVersion: string | null
  /** 后端结算口径（settlement_version） */
  settlementVersion: string | null
  /** 0.3.18 §12.1：响应 meta={db, promoted, readonly}（已和所选数据源核对过） */
  meta: DbMeta | null
  /** 0.3.19：响应顶层 daily_check_summary（本页日核对计数）；多页时按页累加 by_reason / n_matches */
  dailyCheckSummary: DailyCheckSummary | null
}

// ───────────────────────── 归一化（嵌套 → 平铺）─────────────────────────

function toOpenInfo(p: OpenMetaFields | null | undefined): OpenInfo | null {
  if (!p || p.open_basis === undefined) return null
  return {
    basis: p.open_basis ?? null,
    earliestTsQuoteAt: p.earliest_ts_quote_at ?? null,
    usableAtMid: p.usable_at_mid ?? null,
    usableAtClose: p.usable_at_close ?? null,
    unusableReason: p.unusable_reason ?? null,
    tsInferred: p.ts_inferred === true,
  }
}

/** hl_v0.3 返还率兜底：只读后端字段；fallback_n 缺失时按 0 处理（不满足 n ≥ 100） */
function toFallback(p: FallbackFields | null | undefined): FallbackInfo | null {
  if (!p) return null
  const has = ['fallback_p25', 'fallback_n', 'fallback_hl_eligible', 'fallback_p10', 'fallback_p90'].some((k) => k in p)
  if (!has) return null
  const n = typeof p.fallback_n === 'number' ? p.fallback_n : null
  return {
    p25: p.fallback_p25 ?? null,
    p10: p.fallback_p10 ?? null,
    p90: p.fallback_p90 ?? null,
    n,
    // 两道条件都要过：后端 eligible=true 且 n ≥ 100（前端兜一道，防字段口径漂移）
    eligible: p.fallback_hl_eligible === true && n != null && n >= 100 && p.fallback_p25 != null,
    level: p.fallback_hl_level ?? null,
  }
}

/** 0.3.19 即时（11:10）来源字段；非 rule_1110 条目全 null → 返回 null */
function toInstant(p: InstantFields | null | undefined): InstantInfo | null {
  if (!p || (p.origin == null && p.odds_source == null && p.alt == null && p.instant_src_diff == null && p.main_source_reason == null))
    return null
  return {
    origin: p.origin ?? null,
    oddsSource: p.odds_source ?? null,
    capture: p.capture ?? null,
    capturedAt: p.captured_at ?? null,
    fetchLagMin: p.fetch_lag_min ?? null,
    alt: p.alt ?? null,
    srcDiff: p.instant_src_diff ?? null,
    mainSourceReason: p.main_source_reason ?? null,
  }
}

function toAhCell(p: ApiAhPoint | null | undefined): AhCell | null {
  if (!p) return null
  const parsed = parseHandicap(p.line)
  const empty =
    parsed.value == null && p.home_water == null && p.away_water == null && p.return_rate == null
  if (empty) return null
  return {
    line: parsed.value,
    lineRaw: parsed.raw,
    lineNonStandard: parsed.nonStandard,
    hw: p.home_water ?? null,
    aw: p.away_water ?? null,
    waterSource: p.water_source ?? p.water_src ?? null,
    bookLane: p.book_lane ?? null,
    waterCensored: !!p.water_censored,
    waterMoveEligible: p.water_move_eligible ?? null,
    tierCross: p.tier_cross ?? null,
    tierCrossMid: p.tier_cross_mid === true,
    fallback: toFallback(p),
    instant: toInstant(p),
    phasePending: p.phase_pending === true,
    featuresOk: p.features_ok !== false,
    phaseAssignLate: p.phase_assign_late === true,
    targetAt: p.target_at ?? null,
    recordedAt: p.recorded_at ?? null,
    returnRate: p.return_rate ?? null,
    returnRateBaseline: p.return_rate_baseline ?? null,
    label: p.label ?? null,
    basis: p.basis ?? null,
    source: p.source ?? null,
    openInfo: toOpenInfo(p),
    baselineMethod: p.baseline_method ?? null,
  }
}

function toX1x2Cell(p: ApiX1x2Point | null | undefined): X1x2Cell | null {
  if (!p) return null
  if (p.home == null && p.draw == null && p.away == null && p.return_rate == null) return null
  return {
    home: p.home ?? null,
    draw: p.draw ?? null,
    away: p.away ?? null,
    recordedAt: p.recorded_at ?? null,
    waterSource: p.water_source ?? null,
    returnRate: p.return_rate ?? null,
    returnRateBaseline: p.return_rate_baseline ?? null,
    kelly: p.kelly ?? null,
    kellyBase: p.kelly_base ?? null,
    openInfo: toOpenInfo(p),
    baselineMethod: p.baseline_method ?? null,
    nAvg: p.n_avg ?? null,
    fallback: toFallback(p),
  }
}

/** 0.3.21：竞彩胜平负；空壳（collector_empty）也保留，供灰字 */
function toJc1x2Cell(p: ApiX1x2Point | null | undefined): Jc1x2Cell | null {
  if (!p) return null
  const base = toX1x2Cell(p)
  const incomplete =
    p.jc_1x2_incomplete === true ||
    p.missing_reason === 'legacy_home_only' ||
    p.missing_reason === 'jc_1x2_incomplete'
  const cell: Jc1x2Cell = {
    home: p.home ?? null,
    draw: p.draw ?? null,
    away: p.away ?? null,
    recordedAt: p.recorded_at ?? null,
    waterSource: p.water_source ?? null,
    returnRate: p.return_rate ?? null,
    returnRateBaseline: p.return_rate_baseline ?? null,
    kelly: p.kelly ?? null,
    kellyBase: p.kelly_base ?? null,
    openInfo: toOpenInfo(p),
    baselineMethod: p.baseline_method ?? null,
    nAvg: p.n_avg ?? null,
    fallback: toFallback(p),
    complete: p.complete ?? null,
    incomplete,
    missingReason: p.missing_reason ?? null,
    outOfWindow: p.out_of_window === true,
    fetchLagMin: p.fetch_lag_min ?? null,
    available: p.available ?? null,
    source: p.source ?? null,
    alt: (p.alt as Jc1x2Alt | null | undefined) ?? null,
  }
  // 有赔率、或有缺失原因/不可用标记 → 保留（灰字）
  if (base) return cell
  if (p.missing_reason || p.available === false || p.jc_1x2_incomplete != null || p.out_of_window != null) return cell
  return null
}

function toJcHhadCell(p: ApiJcHhadPoint | null | undefined): JcHhadCell | null {
  if (!p) return null
  const goal = p.goal_line ?? p.decision_line ?? null
  const incomplete = p.jc_1x2_incomplete === true || p.complete === false
  const hasAny =
    goal != null ||
    p.home != null ||
    p.draw != null ||
    p.away != null ||
    !!p.missing_reason ||
    p.available === false ||
    p.post_decision_line_change === true
  if (!hasAny) return null
  return {
    goalLine: goal,
    decisionLine: p.decision_line ?? goal,
    currentLine: p.current_line ?? null,
    postDecisionLineChange: p.post_decision_line_change === true,
    fromHist: p.from_hist === true,
    lineRev: typeof p.line_rev === 'number' ? p.line_rev : 0,
    home: p.home ?? null,
    draw: p.draw ?? null,
    away: p.away ?? null,
    complete: p.complete ?? null,
    incomplete,
    missingReason: p.missing_reason ?? null,
    outOfWindow: p.out_of_window === true,
    fetchLagMin: p.fetch_lag_min ?? null,
    available: p.available ?? null,
    source: p.source ?? null,
    recordedAt: p.recorded_at ?? p.captured_at ?? null,
    alt: p.alt ?? null,
  }
}

/** 开赛时间悬停文案（只读后端标记） */
export const KICKOFF_PLACEHOLDER_TIP = '开赛时间为占位值，分钟未知'
export const KICKOFF_SOURCE_JINGCAI_TIP = '开赛时间来自竞彩官方'
/** 0.3.17 §13：kickoff_source=jingcai_hour_synth（没有完整开赛时刻，按竞彩日 + 整点合成） */
export const KICKOFF_SOURCE_SYNTH_TIP = '开赛时间按竞彩日和整点合成，分钟未知'

/** 0.3.19：竞彩时刻冲突只读后端 kickoff_jc_conflict（前端不再自己比对 kickoff_jc；空值 = 没查，不显示） */
export const KICKOFF_JC_CONFLICT_TIP = '竞彩官方时刻与开赛时间相差超过 90 分钟，待核对'
export function kickoffJcTip(kickoffJc: string): string {
  const d = dayjs(kickoffJc)
  return `竞彩官方时刻：${d.isValid() ? d.format('MM-DD HH:mm') : kickoffJc}（只到整点）`
}

/** 0.3.19 推迟场悬停 */
export const POSTPONE_TS_UNKNOWN_TIP = '推迟消息发布时间未知，目标时刻按原定开赛时间算'
export const VOID_POSTPONED_TIP =
  '比原定开赛时间推迟超过 24 小时（澳门第 67/2018 号行政命令第十一条），按无效退款处理，不计命中、不计可评场次'
/** 预留字段的悬停（分析师 18:20 / 18:22 / 18:24） */
export const PLACEHOLDER_SUSPECT_TIP = '开赛时间疑似占位，待确认'
export const PHASE_PENDING_TIP = '开赛时间未确认，已抓取，暂不归中盘或临盘'
export const PHASE_PENDING_LABEL = '待归阶段'
export const FEATURES_NOT_OK_TIP = '按占位开赛时间推算，不参与特征计算'
/** @deprecated 改用 FEATURES_NOT_OK_TIP；保留别名避免旧引用 */
export const PLACEHOLDER_DEPENDENT_TIP = FEATURES_NOT_OK_TIP
export const PHASE_ASSIGN_LATE_TIP = '开赛时间在该时刻之后才确认，实时台账不计命中'
export const kickoffRevTip = (n: number) => `开赛时间曾变更（第 ${n} 次）`
export const kickoffDriftTip = (min: number) =>
  min >= 0 ? `实际开赛比赛程晚 ${Math.round(min)} 分钟（未按推迟处理）` : `实际开赛比赛程早 ${Math.round(-min)} 分钟（未按推迟处理）`

function fmtMd(s: string | null | undefined): string | null {
  if (!s) return null
  const d = dayjs(s)
  return d.isValid() ? d.format('MM-DD HH:mm') : s
}

interface KickoffNoteInput {
  placeholder?: string | null
  source?: string | null
  kickoffJc?: string | null
  kickoffJcConflict?: boolean | null
  postponed?: boolean | null
  kickoffOriginal?: string | null
  announcedAt?: string | null
  postponeTsUnknown?: boolean | null
  driftMin?: number | null
  placeholderSuspect?: boolean
  phasePending?: boolean
  featuresOk?: boolean
  phaseAssignLate?: boolean
  kickoffRev?: number | null
}

function kickoffNote(k: KickoffNoteInput): string | null {
  const lines: string[] = []
  if (k.postponed) {
    const orig = fmtMd(k.kickoffOriginal)
    if (k.postponeTsUnknown) lines.push(`原定 ${orig ?? '未知'}；${POSTPONE_TS_UNKNOWN_TIP}`)
    else lines.push(`原定 ${orig ?? '未知'}，推迟消息 ${fmtMd(k.announcedAt) ?? '时间未知'} 发布`)
  } else if (typeof k.driftMin === 'number' && Number.isFinite(k.driftMin) && Math.round(k.driftMin) !== 0) {
    lines.push(kickoffDriftTip(k.driftMin))
  }
  if (k.phasePending) lines.push(PHASE_PENDING_TIP)
  if (k.placeholderSuspect) lines.push(PLACEHOLDER_SUSPECT_TIP)
  if (k.featuresOk === false) lines.push(FEATURES_NOT_OK_TIP)
  if (k.phaseAssignLate) lines.push(PHASE_ASSIGN_LATE_TIP)
  if (typeof k.kickoffRev === 'number' && k.kickoffRev > 0) lines.push(kickoffRevTip(k.kickoffRev))
  if (k.placeholder === '5df_1200') lines.push(KICKOFF_PLACEHOLDER_TIP)
  if (k.source === 'jingcai') lines.push(KICKOFF_SOURCE_JINGCAI_TIP)
  if (k.source === 'jingcai_hour_synth') lines.push(KICKOFF_SOURCE_SYNTH_TIP)
  if (k.kickoffJcConflict === true) {
    lines.push(KICKOFF_JC_CONFLICT_TIP)
    if (k.kickoffJc) lines.push(kickoffJcTip(k.kickoffJc))
  }
  return lines.length ? lines.join('；') : null
}

function kickoffLabel(kickoffAt: string | null | undefined, hour: number | null | undefined): string {
  if (kickoffAt && /T\d{2}:\d{2}/.test(kickoffAt)) {
    const d = dayjs(kickoffAt)
    if (d.isValid()) return d.format('MM-DD HH:mm')
  }
  return hour != null ? `${String(hour).padStart(2, '0')}:00` : '—'
}

/** 唯一的 嵌套 → 平铺 转换 */
export function normalizeTableRow(api: ApiTableRow, asOf: string | null = null): SheetRow {
  const m = api.match
  const ah = {} as SheetRow['ah']
  for (const b of AH_BOOKS) {
    const phases = api.ah?.[b] ?? null
    const live = (phases?.live ?? [])
      .map((pt) => toAhCell(pt))
      .filter((c): c is AhCell => c != null && c.recordedAt != null)
      .sort((a, b) => (a.recordedAt! < b.recordedAt! ? -1 : 1))
    ah[b] = {
      open: toAhCell(phases?.open),
      mid: toAhCell(phases?.mid),
      close: toAhCell(phases?.close),
      midReal: toAhCell(phases?.mid_real),
      closeReal: toAhCell(phases?.close_real),
      live,
      hidden: Object.fromEntries(
        (
          [
            ['open', phases?.open],
            ['mid', phases?.mid],
            ['close', phases?.close],
            ['midReal', phases?.mid_real],
            ['closeReal', phases?.close_real],
          ] as const
        )
          .filter(([, pt]) => pt?.hidden_reason)
          .map(([k, pt]) => [k, pt!.hidden_reason as string]),
      ),
    }
  }
  // 0.3.22 预留：macau_5df 并列路；接口无该键 → null（不写死、不顶主列澳门）
  const phases5 = api.ah?.macau_5df ?? null
  const ahMacau5df = phases5
    ? {
        open: toAhCell(phases5.open),
        mid: toAhCell(phases5.mid),
        close: toAhCell(phases5.close),
        midReal: toAhCell(phases5.mid_real),
        closeReal: toAhCell(phases5.close_real),
        live: (phases5.live ?? [])
          .map((pt) => toAhCell(pt))
          .filter((c): c is AhCell => c != null && c.recordedAt != null)
          .sort((a, b) => (a.recordedAt! < b.recordedAt! ? -1 : 1)),
        hidden: Object.fromEntries(
          (
            [
              ['open', phases5.open],
              ['mid', phases5.mid],
              ['close', phases5.close],
              ['midReal', phases5.mid_real],
              ['closeReal', phases5.close_real],
            ] as const
          )
            .filter(([, pt]) => pt?.hidden_reason)
            .map(([k, pt]) => [k, pt!.hidden_reason as string]),
        ),
      }
    : null
  const x1x2 = {} as SheetRow['x1x2']
  for (const b of AH_BOOKS) {
    const v = api.x1x2?.[b] ?? null
    x1x2[b] = { open: toX1x2Cell(v?.open), close: toX1x2Cell(v?.close) }
  }
  const result = api.result
  const finished = result != null
  const closingRef = {} as SheetRow['x1x2ClosingRef']
  for (const b of AH_BOOKS) {
    const c = api.x1x2_closing_ref?.[b]
    // 只在已完赛时显示（未完赛不显示）
    closingRef[b] =
      finished && c && (c.home != null || c.draw != null || c.away != null)
        ? { home: c.home, draw: c.draw, away: c.away, fetchedAt: c.fetched_at ?? null }
        : null
  }
  // 推迟作废（void_postponed）可能没有赛果，也照样显示「推迟作废」
  const settlement = finished || api.settlement?.code === 'void_postponed' ? api.settlement : null
  return {
    id: m.id,
    date: m.date,
    scope: m.scope,
    jcNo: m.jc_no ?? '—',
    league: m.league ?? '—',
    // 推迟场显示实际开赛（kickoff_actual；后端 kickoff_at 也是实际开赛）
    kickoffAt: (m.postponed ? m.kickoff_actual : null) ?? m.kickoff_at ?? null,
    kickoffLabel: kickoffLabel((m.postponed ? m.kickoff_actual : null) ?? m.kickoff_at, m.kickoff_hour),
    kickoffNote: kickoffNote({
      placeholder: m.kickoff_placeholder,
      source: m.kickoff_source,
      kickoffJc: m.kickoff_jc,
      kickoffJcConflict: m.kickoff_jc_conflict,
      postponed: m.postponed,
      kickoffOriginal: m.kickoff_original,
      announcedAt: m.postponed_announced_at,
      postponeTsUnknown: m.postpone_ts_unknown,
      driftMin: m.kickoff_drift_min,
      placeholderSuspect: m.kickoff_placeholder_suspect === true,
      phasePending: m.phase_pending === true,
      featuresOk: m.features_ok !== false,
      phaseAssignLate: m.phase_assign_late === true,
      kickoffRev: typeof m.kickoff_rev === 'number' ? m.kickoff_rev : 0,
    }),
    kickoffJcConflict: m.kickoff_jc_conflict === true,
    kickoffJc: m.kickoff_jc ?? null,
    postponed: m.postponed === true,
    phasePending: m.phase_pending === true,
    featuresOk: m.features_ok !== false,
    phaseAssignLate: m.phase_assign_late === true,
    kickoffRev: typeof m.kickoff_rev === 'number' ? m.kickoff_rev : 0,
    postponeTargetBasis: api.schedule?.postpone_target_basis
      ? { mid: api.schedule.postpone_target_basis.mid ?? null, close: api.schedule.postpone_target_basis.close ?? null }
      : null,
    manualReview: m.manual_review === true,
    manualReviewReason: m.manual_review_reason ?? null,
    settlementHiddenReason: api.settlement_hidden_reason ?? null,
    ledgerNote: api.prediction?.ledger_note ?? null,
    home: m.home,
    away: m.away,
    matchup: `${m.home} 对 ${m.away}`,
    ah,
    ahMacau5df,
    phaseException: !!api.phase_exception,
    schedule: {
      midTarget: api.schedule?.mid_target_time ?? null,
      closeTarget: api.schedule?.close_target_time ?? null,
      midRealTarget: api.schedule?.mid_real_target_time ?? null,
      closeRealTarget: api.schedule?.close_real_target_time ?? null,
    },
    x1x2,
    x1x2ClosingRef: closingRef,
    jc: {
      open: toJc1x2Cell(api.jc_1x2?.open),
      mid: toJc1x2Cell(api.jc_1x2?.mid),
      close: toJc1x2Cell(api.jc_1x2?.close),
    },
    jcPresent: api.jc_1x2 != null,
    jcHhad: {
      open: toJcHhadCell(api.jc_hhad?.open),
      mid: toJcHhadCell(api.jc_hhad?.mid),
      close: toJcHhadCell(api.jc_hhad?.close),
    },
    jcHhadPresent: api.jc_hhad != null,
    openReason: {
      ah: Object.fromEntries(AH_BOOKS.map((b) => [b, api.ah?.[b]?.open?.open_basis_reason ?? null])) as Record<AhBook, string | null>,
      x1x2: Object.fromEntries(AH_BOOKS.map((b) => [b, api.x1x2?.[b]?.open?.open_basis_reason ?? null])) as Record<AhBook, string | null>,
      jc: api.jc_1x2?.open?.open_basis_reason ?? null,
    },
    multiAvgProb: api.multi_avg_prob ?? null,
    strategy: api.prediction?.strategy ?? null,
    direction: api.prediction?.direction ?? null,
    stake: api.prediction?.stake ?? null,
    confidence: api.prediction?.confidence ?? null,
    producedAt: api.prediction?.produced_at ?? api.produced_at ?? null,
    finished,
    hiddenReason: finished ? null : (api.hidden_reason ?? '未完场'),
    score: result ? `${result.home_goals}:${result.away_goals}` : null,
    wdl: result?.wdl ?? null,
    settleCode: settlement?.code ?? null,
    pnlUnits: settlement?.pnl_units ?? null,
    settleSource: settlement?.source ?? null,
    settleVersion: settlement ? (api.settlement_version ?? null) : null,
    asOf: api.as_of ?? asOf,
  }
}

// ───────────────────────── 批量接口 ─────────────────────────

/** settlement.code（§13 最终名，与 SettleCode 同值）；早先草稿 result_code 的 half_win / half_loss / loss 也兼容 */
const SETTLE_CODE: Record<string, SettleCode> = {
  win: 'win',
  win_half: 'win_half',
  half_win: 'win_half',
  push: 'push',
  lose_half: 'lose_half',
  half_loss: 'lose_half',
  lose: 'lose',
  loss: 'lose',
  no_bet: 'no_bet',
  void_postponed: 'void_postponed',
}

/** 后端 *_hidden_reason 代码 → 中文（未知代码不直接显示英文） */
function hiddenReasonCn(code: string | null | undefined): string | null {
  if (!code) return null
  const map: Record<string, string> = {
    no_result: '暂无赛果',
    not_finished: '未完场',
    before_kickoff: '未开赛',
    not_started: '未开赛',
    too_early: '开赛后一段时间才显示赛果',
    not_visible_yet: '开赛后一段时间才显示赛果',
    kickoff_unknown: '开赛时间未知，暂不显示赛果',
    no_prediction: '无预测',
    manual_review: '人工复核中',
  }
  return map[code] ?? '暂无赛果'
}

function beAhPoint(c: BeAhCell | null | undefined): ApiAhPoint | null {
  if (!c) return null
  return {
    line: c.line,
    home_water: c.home_water,
    away_water: c.away_water,
    target_at: c.target_at ?? c.target_time ?? null,
    recorded_at: c.recorded_at,
    water_source: c.water_source ?? (c as BeAhCell & { water_src?: string | null }).water_src ?? null,
    water_src: (c as BeAhCell & { water_src?: string | null }).water_src ?? c.water_source ?? null,
    book_lane: (c as BeAhCell & { book_lane?: string | null }).book_lane ?? null,
    available: (c as BeAhCell & { available?: boolean | null }).available ?? null,
    water_censored: c.water_censored,
    water_move_eligible: c.water_move_eligible ?? null,
    tier_cross: c.tier_cross ?? null,
    tier_cross_mid: c.tier_cross_mid ?? null,
    phase_pending: c.phase_pending ?? null,
    features_ok: c.features_ok ?? null,
    phase_assign_late: c.phase_assign_late ?? null,
    ...fallbackOf(c),
    return_rate: c.return_rate ?? null,
    return_rate_baseline: c.return_rate_baseline ?? null,
    source: c.source,
    basis: c.basis ?? null,
    hidden_reason: c.hidden_reason ?? null,
    baseline_method: c.baseline_method ?? null,
    ...openMeta(c),
  }
}

/** hl_v0.3 兜底字段：原样透传（缺字段不写键） */
function fallbackOf(c: FallbackFields): FallbackFields {
  const out: FallbackFields = {}
  for (const k of ['fallback_p25', 'fallback_p10', 'fallback_p90', 'fallback_n', 'fallback_hl_eligible', 'fallback_hl_level'] as const) {
    if (k in c) (out as Record<string, unknown>)[k] = c[k] ?? null
  }
  return out
}

/** 只在初盘对象上带；字段缺失（非 open 格）时不写键 */
function openMeta(c: OpenMetaFields): OpenMetaFields {
  if (c.open_basis === undefined) return {}
  return {
    open_basis: c.open_basis ?? null,
    earliest_ts_quote_at: c.earliest_ts_quote_at ?? null,
    usable_at_mid: c.usable_at_mid ?? null,
    usable_at_close: c.usable_at_close ?? null,
    unusable_reason: c.unusable_reason ?? null,
    ts_inferred: c.ts_inferred ?? null,
    open_basis_reason: c.open_basis_reason ?? null,
  }
}

function beX1x2Point(c: BeX1x2Cell | null | undefined): ApiX1x2Point | null {
  if (!c) return null
  return {
    home: c.home,
    draw: c.draw,
    away: c.away,
    recorded_at: c.recorded_at,
    return_rate: c.return_rate ?? null,
    return_rate_baseline: c.return_rate_baseline ?? null,
    kelly: c.kelly ?? null,
    kelly_base: c.kelly_base === 'consensus' ? 'multi_avg' : (c.kelly_base ?? null),
    water_source: c.water_source ?? null,
    source: c.source ?? null,
    basis: c.basis ?? null,
    fetched_at: c.fetched_at ?? null,
    baseline_method: c.baseline_method ?? null,
    n_avg: c.n_avg ?? null,
    complete: c.complete ?? null,
    jc_1x2_incomplete: c.jc_1x2_incomplete ?? null,
    missing_reason: c.missing_reason ?? null,
    out_of_window: c.out_of_window ?? null,
    fetch_lag_min: c.fetch_lag_min ?? null,
    available: c.available ?? null,
    alt: c.alt ?? null,
    hidden_reason: c.hidden_reason ?? null,
    ...fallbackOf(c),
    ...openMeta(c),
  }
}

function beJcHhadPoint(c: BeJcHhadCell | null | undefined): ApiJcHhadPoint | null {
  if (!c) return null
  return {
    goal_line: c.goal_line ?? null,
    decision_line: c.decision_line ?? null,
    current_line: c.current_line ?? null,
    post_decision_line_change: c.post_decision_line_change ?? null,
    from_hist: c.from_hist ?? null,
    line_rev: c.line_rev ?? null,
    home: c.home ?? null,
    draw: c.draw ?? null,
    away: c.away ?? null,
    complete: c.complete ?? null,
    jc_1x2_incomplete: c.jc_1x2_incomplete ?? null,
    missing_reason: c.missing_reason ?? null,
    out_of_window: c.out_of_window ?? null,
    fetch_lag_min: c.fetch_lag_min ?? null,
    available: c.available ?? null,
    source: c.source ?? null,
    basis: c.basis ?? null,
    captured_at: c.captured_at ?? null,
    target_at: c.target_at ?? null,
    recorded_at: c.recorded_at ?? null,
    alt: c.alt ?? null,
  }
}

const API_CLOSING = 'api_closing'

const BE_BOOK: Record<string, AhBook> = { pinnacle: 'pinnacle', pinbet: 'pinnacle', macau: 'macau', crown: 'crown', william: 'william' }
/** 0.3.22 并列路 live[] book 名 → 写入 ah.macau_5df.live（不进主四家） */
const BE_PARALLEL_BOOK: Record<string, 'macau_5df'> = { macau_5df: 'macau_5df', '5df_macau': 'macau_5df' }


/** 后端真实返回（API 0.3.15）→ 前端中间嵌套结构。字段对照见 v2_0-table-matches-api.md §13 */
export function fromBackendRow(be: BeTableRow): ApiTableRow {
  const m = be.match
  const ah: ApiTableRow['ah'] = {}
  for (const b of AH_BOOKS) {
    const v = be.ah?.[b]
    if (!v) {
      ah[b] = null
      continue
    }
    ah[b] = {
      open: beAhPoint(v.open),
      mid: beAhPoint(v.mid),
      close: beAhPoint(v.close),
      mid_real: beAhPoint(v.mid_real),
      close_real: beAhPoint(v.close_real),
      live: [],
    }
  }
  // 0.3.22 预留：副本将先出 ah.macau_5df（5DF 真实水位）；现网/无键则跳过
  const v5 = be.ah?.['macau_5df']
  if (v5) {
    ah.macau_5df = {
      open: beAhPoint(v5.open),
      mid: beAhPoint(v5.mid),
      close: beAhPoint(v5.close),
      mid_real: beAhPoint(v5.mid_real),
      close_real: beAhPoint(v5.close_real),
      live: [],
    }
  }
  // 即时快照在行级 live[]（带 book / market / label）；只取亚盘
  for (const e of be.live ?? []) {
    const b = BE_BOOK[e.book]
    if (!b || !ah[b]) continue
    if (!/^(ah|asian)/i.test(e.market) && e.line == null) continue
    ah[b]!.live!.push({
      line: e.line,
      home_water: e.home_water,
      away_water: e.away_water,
      target_at: e.target_at ?? e.target_time ?? null,
      recorded_at: e.recorded_at,
      water_source: e.water_source ?? null,
      source: e.source ?? null,
      label: e.label,
      // 0.3.19 即时（11:10）双源：主值来源 + alt 对照（alt 不参与任何上色 / 升降盘 / 导出）
      origin: e.origin ?? null,
      odds_source: e.odds_source ?? null,
      capture: e.capture ?? null,
      captured_at: e.captured_at ?? null,
      fetch_lag_min: e.fetch_lag_min ?? null,
      merge_rule: e.merge_rule ?? null,
      alt: e.alt ?? null,
      instant_src_diff: e.instant_src_diff ?? null,
      main_source_reason: e.main_source_reason ?? null,
      own_capture_out_of_window: e.own_capture_out_of_window ?? null,
      phase_assign_late: e.phase_assign_late ?? null,
    })
  }
  // 0.3.22：macau_5df 的 live 快照（有并列路才写）
  for (const e of be.live ?? []) {
    const pb = BE_PARALLEL_BOOK[e.book]
    if (!pb || !ah[pb]) continue
    if (!/^(ah|asian)/i.test(e.market) && e.line == null) continue
    ah[pb]!.live!.push({
      line: e.line,
      home_water: e.home_water,
      away_water: e.away_water,
      target_at: e.target_at ?? e.target_time ?? null,
      recorded_at: e.recorded_at,
      water_source: e.water_source ?? null,
      source: e.source ?? null,
      label: e.label,
      origin: e.origin ?? null,
      odds_source: e.odds_source ?? null,
      capture: e.capture ?? null,
      captured_at: e.captured_at ?? null,
      fetch_lag_min: e.fetch_lag_min ?? null,
      merge_rule: e.merge_rule ?? null,
      alt: e.alt ?? null,
      instant_src_diff: e.instant_src_diff ?? null,
      main_source_reason: e.main_source_reason ?? null,
      own_capture_out_of_window: e.own_capture_out_of_window ?? null,
      phase_assign_late: e.phase_assign_late ?? null,
    })
  }
  // 欧赔接口收盘价（0.3.16）：后端单独放在 x1x2[book].api_closing（仅赛果可见后），从不并进 close / 返还率 / 凯利
  const x1x2: ApiTableRow['x1x2'] = {}
  const closingRef: NonNullable<ApiTableRow['x1x2_closing_ref']> = {}
  for (const b of AH_BOOKS) {
    const v = be.x1x2?.[b]
    if (!v) {
      x1x2[b] = null
      continue
    }
    // 防御：若 close 本身标了 api_closing（旧草稿行为），不当临盘用
    const close = v.close?.basis === API_CLOSING ? null : beX1x2Point(v.close)
    x1x2[b] = { open: beX1x2Point(v.open), close }
    const ac = v.api_closing
    if (ac) closingRef[b] = { home: ac.home, draw: ac.draw, away: ac.away, fetched_at: ac.fetched_at ?? null }
  }
  const base = be.x1x2_base?.close
  const mavg = be.multi_avg_prob ?? base?.multi_avg ?? base?.consensus ?? null
  const st = be.settlement
  const code = st ? SETTLE_CODE[st.code ?? st.result_code ?? ''] : undefined
  const dir = be.prediction?.direction
  return {
    match: {
      id: be.match_id ?? m.match_id,
      date: m.jingcai_date,
      scope: m.scope === 'extra' ? 'extra' : 'jingcai',
      jc_no: m.jc_id ?? (m.jc_no != null ? String(m.jc_no) : null),
      league: m.league,
      kickoff_at: m.kickoff_at,
      kickoff_hour: null,
      kickoff_placeholder: be.kickoff_placeholder ?? m.kickoff_placeholder ?? null,
      kickoff_source: be.kickoff_source ?? m.kickoff_source ?? null,
      kickoff_jc: be.kickoff_jc ?? m.kickoff_jc ?? null,
      kickoff_jc_conflict: m.kickoff_jc_conflict ?? null,
      postponed: m.postponed ?? null,
      kickoff_original: m.kickoff_original ?? null,
      kickoff_actual: m.kickoff_actual ?? null,
      postponed_announced_at: m.postponed_announced_at ?? null,
      postpone_ts_unknown: m.postpone_ts_unknown ?? null,
      postpone_delay_minutes: m.postpone_delay_minutes ?? null,
      kickoff_drift_min: m.kickoff_drift_min ?? null,
      // 预留字段：先看 match，再看 match.extras / 行级 extras（分析师说可能放 extras，以接口为准）
      kickoff_placeholder_suspect: pickFlag('kickoff_placeholder_suspect', m, m.extras, be.extras),
      phase_pending: pickFlag('phase_pending', m, m.extras, be.extras) ?? false,
      features_ok: pickFlag('features_ok', m, m.extras, be.extras) ?? true,
      phase_assign_late: pickFlag('phase_assign_late', m, m.extras, be.extras) ?? false,
      kickoff_rev: (() => {
        if (typeof m.kickoff_rev === 'number') return m.kickoff_rev
        const ex = (m.extras ?? be.extras) as Record<string, unknown> | null | undefined
        return typeof ex?.kickoff_rev === 'number' ? (ex.kickoff_rev as number) : 0
      })(),
      manual_review: m.manual_review ?? null,
      manual_review_reason: m.manual_review_reason ?? null,
      daily_check: m.daily_check ?? null,
      home: m.home_team ?? '—',
      away: m.away_team ?? '—',
    },
    ah,
    x1x2,
    x1x2_closing_ref: closingRef,
    jc_1x2: be.jc_1x2
      ? {
          open: beX1x2Point(be.jc_1x2.open),
          mid: beX1x2Point(be.jc_1x2.mid),
          close: beX1x2Point(be.jc_1x2.close),
        }
      : null,
    jc_hhad: be.jc_hhad
      ? {
          open: beJcHhadPoint(be.jc_hhad.open),
          mid: beJcHhadPoint(be.jc_hhad.mid),
          close: beJcHhadPoint(be.jc_hhad.close),
        }
      : null,
    prediction: be.prediction
      ? {
          strategy: be.prediction.strategy ?? '',
          direction: (dir === '主' || dir === '客' || dir === '不下注' ? dir : null) as Direction | null,
          stake: be.prediction.stake,
          confidence: be.prediction.confidence,
          settle_book: be.prediction.settle_book ?? null,
          produced_at: be.prediction.produced_at,
          ledger_note: be.prediction.ledger_note ?? null,
          ledger_note_reason: be.prediction.ledger_note_reason ?? null,
        }
      : null,
    result: be.result ? { home_goals: be.result.home_goals, away_goals: be.result.away_goals, wdl: be.result.wdl as '胜' | '平' | '负' } : null,
    settlement:
      st && code
        ? { code, pnl_units: st.pnl_units, line: st.line ?? null, juice: st.juice ?? null, juice_source: st.juice_source ?? null, source: st.source ?? 'backend' }
        : null,
    settlement_version: st?.settlement_version ?? null,
    settlement_hidden_reason: be.settlement_hidden_reason ?? null,
    phase_exception: be.phase_exception,
    schedule: be.schedule ?? null,
    hidden_reason: hiddenReasonCn(be.hidden_reason ?? be.result_hidden_reason ?? be.settlement_hidden_reason),
    multi_avg_prob: mavg ? { home: mavg.home, draw: mavg.draw, away: mavg.away } : null,
    produced_at: be.produced_at ?? null,
    as_of: be.as_of ?? null,
  }
}

/** 预留布尔字段：按顺序在 match / match.extras / 行级 extras 里找，第一个出现的为准 */
function pickFlag(key: string, ...objs: (Record<string, unknown> | object | null | undefined)[]): boolean | null {
  for (const o of objs) {
    if (o && typeof o === 'object' && key in o) {
      const v = (o as Record<string, unknown>)[key]
      if (v === true || v === false) return v
    }
  }
  return null
}

function isBackendRow(x: unknown): x is BeTableRow {
  return !!x && typeof x === 'object' && 'match_id' in x && 'match' in x
}

const BATCH_PAGE = 2000

/** 批量接口失败：抛中文错误（页面显示并提供重试），不再退回逐场拼装 */
export async function loadSheet(p: LoadParams): Promise<LoadResult> {
  const items: (BeTableRow | ApiTableRow)[] = []
  let asOf: string | null = null
  let cfg: string | null = null
  let sv: string | null = null
  let meta: DbMeta | null = null
  let dcs: DailyCheckSummary | null = null
  const src: DbSource = p.source ?? 'live'
  const base = apiBase(src)
  const instName = src === 'replica' ? '副本 8788' : '后端 8787'
  for (let offset = 0; ; offset += BATCH_PAGE) {
    const q = new URLSearchParams({
      date_from: p.dateFrom,
      date_to: p.dateTo,
      scope: p.scope,
      strategy: p.strategy,
      include_live: p.includeLive,
      limit: String(BATCH_PAGE),
      offset: String(offset),
    })
    let res: Response
    try {
      res = await fetch(`${base}/table/matches?${q}`)
    } catch {
      throw new Error(`批量接口 /table/matches 连接失败，请确认${instName}已启动`)
    }
    if (!res.ok) {
      let detail = ''
      try {
        const j = (await res.json()) as { detail?: unknown }
        if (typeof j?.detail === 'string') detail = `：${j.detail}`
      } catch {
        /* 非 JSON 错误体 */
      }
      throw new Error(`批量接口 /table/matches 返回错误（HTTP ${res.status}）${detail}`)
    }
    const ct = res.headers.get('content-type') ?? ''
    if (!ct.includes('json')) throw new Error('批量接口 /table/matches 返回的不是 JSON')
    const body = (await res.json()) as Partial<BeTableResponse> & Partial<ApiTableResponse> & { settlement_version?: string }
    if (!body || !Array.isArray(body.items)) throw new Error('批量接口 /table/matches 响应缺少 items')
    // 数据源标识只认响应 meta；与所选不符直接报错，不静默混用
    const m = (body as { meta?: DbMeta }).meta ?? null
    const bad = metaMismatch(src, m)
    if (bad) throw new Error(bad)
    if (meta && m && meta.db !== m.db) throw new Error(`分页返回的 meta.db 前后不一致（${meta.db} / ${m.db}），已停止显示`)
    meta = m
    asOf = body.as_of ?? asOf
    cfg = (body as BeTableResponse).config_version ?? cfg
    sv = body.settlement_version ?? sv
    const pageDcs = (body as BeTableResponse).daily_check_summary
    if (pageDcs && typeof pageDcs === 'object') dcs = mergeDailyCheck(dcs, pageDcs)
    items.push(...(body.items as (BeTableRow | ApiTableRow)[]))
    const total = (body as BeTableResponse).total
    if (typeof total !== 'number' || items.length >= total || body.items.length === 0) break
  }
  const rows = items.map((it) => normalizeTableRow(isBackendRow(it) ? fromBackendRow(it) : it, asOf))
  console.info(`[数据表] 数据来源：批量接口 ${base}/table/matches（meta.db=${meta?.db ?? '—'}），${rows.length} 场，后端口径 ${cfg ?? '—'}`)
  return { rows, asOf, backendConfigVersion: cfg, settlementVersion: sv, meta, dailyCheckSummary: dcs }
}

/** 分页时累加各页 daily_check_summary（结构以实际返回为准：n_matches / by_reason / instant_src_diff_cells_in_live） */
function mergeDailyCheck(a: DailyCheckSummary | null, b: DailyCheckSummary): DailyCheckSummary {
  if (!a) return { ...b, by_reason: { ...(b.by_reason ?? {}) } }
  const by: Record<string, number> = { ...(a.by_reason ?? {}) }
  for (const [k, v] of Object.entries(b.by_reason ?? {})) by[k] = (by[k] ?? 0) + (typeof v === 'number' ? v : 0)
  return {
    ...a,
    n_matches: (a.n_matches ?? 0) + (b.n_matches ?? 0),
    by_reason: by,
    instant_src_diff_cells_in_live: (a.instant_src_diff_cells_in_live ?? 0) + (b.instant_src_diff_cells_in_live ?? 0),
    own_1110_out_of_window_cells: (a.own_1110_out_of_window_cells ?? 0) + (b.own_1110_out_of_window_cells ?? 0),
  }
}
