/**
 * 数据表页列定义（AG Grid Community）。
 * 列 id 规则：ah.{book}.{phase}.line / ah.{book}.chg / ah.{book}.{phase}.hw|aw|rr /
 *            x.{book}.{phase}.home|draw|away|rr / x.{book}.close.k_home… / jc.{phase}.home… /
 *            pred.* / res.* / 基本列用字段名。
 */
import type { ColDef, ColGroupDef, TooltipCallbackParams, ValueFormatterParams } from 'ag-grid-community'
import dayjs from 'dayjs'
import type { AhBook, OpenInfo, Phase, SheetRow, ViewId } from './types'
import { AH_BOOKS, BOOK_LABEL, EXCEPTION_DESC, PARALLEL_BOOK_LABEL, PHASE_HINT, PHASE_LABEL, SETTLE_LABEL, isCollectorEmpty } from './types'
import { formatHandicapCn, formatHandicapLine, formatMove, lineMove } from './handicap'
import { RR_WATER_TIP, buildCellClassRules, evaluateCell, fallbackLineTip, openMissingTip, rrWaterKind, type ColMeta, type RuleContext } from './rules'
import {
  FEATURES_NOT_OK_TIP,
  PHASE_ASSIGN_LATE_TIP,
  PHASE_PENDING_LABEL,
  PHASE_PENDING_TIP,
  POSTPONE_TS_UNKNOWN_TIP,
  VOID_POSTPONED_TIP,
} from './adapter'
import type { AhCell, FallbackInfo } from './types'

export type AnyCol = ColDef<SheetRow> | ColGroupDef<SheetRow>

/** 数据有无（决定列是否出现） */
export interface Presence {
  ahLine: Record<AhBook, boolean>
  ahMid: Record<AhBook, boolean>
  /** 有实际开盘时间（初盘列加宽显示副文本） */
  openTime: Record<AhBook, boolean>
  /** 有例外场（phase_exception，才出「（真实）」列） */
  phaseException: boolean
  ahLive: Record<AhBook, boolean>
  ahWater: Record<AhBook, boolean>
  ahRr: Record<AhBook, boolean>
  x1x2Home: Record<AhBook, boolean>
  x1x2DrawAway: Record<AhBook, boolean>
  x1x2Rr: Record<AhBook, boolean>
  kelly: Record<AhBook, boolean>
  /** 0.3.18：凯利有值的阶段（副本只有初盘有凯利，现网暂无） */
  kellyPhases: Record<AhBook, ('open' | 'close')[]>
  /** 欧赔接口收盘价（api_closing，参考） */
  closingRef: Record<AhBook, boolean>
  multiAvg: boolean
  jcHome: boolean
  jcDrawAway: boolean
  /** 0.3.21：行上带 jc_1x2 */
  jcPresent: boolean
  jcHhadPresent: boolean
  confidence: boolean
  /** 0.3.22：macau_5df 有可用阶段 */
  macau5df: boolean
}

function perBook(fn: (b: AhBook) => boolean): Record<AhBook, boolean> {
  return Object.fromEntries(AH_BOOKS.map((b) => [b, fn(b)])) as Record<AhBook, boolean>
}

export function computePresence(rows: SheetRow[]): Presence {
  const any = (f: (r: SheetRow) => unknown) => rows.some((r) => f(r) != null)
  const phases: Phase[] = ['open', 'mid', 'close']
  return {
    ahLine: perBook((b) => phases.some((p) => any((r) => r.ah[b][p]?.line))),
    ahMid: perBook((b) => any((r) => r.ah[b].mid?.line)),
    openTime: perBook((b) => any((r) => r.ah[b].open?.recordedAt)),
    phaseException: rows.some((r) => r.phaseException),
    ahLive: perBook((b) => rows.some((r) => r.ah[b].live.length > 0)),
    ahWater: perBook((b) => phases.some((p) => any((r) => r.ah[b][p]?.hw ?? r.ah[b][p]?.aw))),
    ahRr: perBook((b) => phases.some((p) => any((r) => r.ah[b][p]?.returnRate))),
    x1x2Home: perBook((b) => any((r) => r.x1x2[b].open?.home ?? r.x1x2[b].close?.home)),
    x1x2DrawAway: perBook((b) =>
      any((r) => r.x1x2[b].open?.draw ?? r.x1x2[b].close?.draw ?? r.x1x2[b].close?.away),
    ),
    x1x2Rr: perBook((b) => any((r) => r.x1x2[b].open?.returnRate ?? r.x1x2[b].close?.returnRate)),
    kelly: perBook((b) => any((r) => r.x1x2[b].close?.kelly?.home ?? r.x1x2[b].open?.kelly?.home)),
    kellyPhases: Object.fromEntries(
      AH_BOOKS.map((b) => [b, (['open', 'close'] as const).filter((p) => any((r) => r.x1x2[b][p]?.kelly?.home))]),
    ) as Record<AhBook, ('open' | 'close')[]>,
    closingRef: perBook((b) => rows.some((r) => r.x1x2ClosingRef[b] != null)),
    multiAvg: any((r) => r.multiAvgProb?.home),
    jcHome: any((r) => r.jc.open?.home ?? r.jc.mid?.home ?? r.jc.close?.home),
    jcDrawAway: any((r) => r.jc.open?.draw ?? r.jc.mid?.draw ?? r.jc.close?.draw ?? r.jc.open?.away ?? r.jc.close?.away),
    /** 0.3.21：接口带了 jc_1x2（含仅 incomplete 或本地采集器无数据的空壳） */
    jcPresent: rows.some((r) => r.jcPresent),
    jcHhadPresent: rows.some((r) => r.jcHhadPresent),
    confidence: any((r) => r.confidence),
    // 0.3.22：任一阶段 available 且有线/水 → 出并列列（副本亮、现网全 false 则藏）
    macau5df: rows.some(
      (r) =>
        r.ahMacau5df != null &&
        (['open', 'mid', 'close'] as const).some((ph) => {
          const c = r.ahMacau5df?.[ph]
          return c != null && (c.line != null || c.hw != null || c.aw != null)
        }),
    ),
  }
}

// ───────────────────────── 格式 ─────────────────────────

export function fmtNum(v: unknown, d = 3): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  return Number(v.toFixed(d)).toString()
}

/** 盘口主格文案：只显示盘口数字（如 +0.5）；水位在独立主水/客水列，不再内嵌 */
export function formatAhLineWithWater(
  c: { line: number | null; lineRaw: string | null; lineNonStandard: boolean; hw?: number | null; aw?: number | null } | null | undefined,
): string {
  if (!c || c.line == null) return '—'
  if (c.lineNonStandard && c.lineRaw && Number.isNaN(Number(c.lineRaw))) return c.lineRaw
  return formatHandicapLine(c.line)
}

/** 悬停：数字 + 汉字对照；无水位标明「暂无水位」 */
export function ahLineWaterTipExtra(c: {
  line: number | null
  hw: number | null
  aw: number | null
  waterSource?: string | null
  source?: string | null
  bookLane?: string | null
}): string {
  const cn = c.line != null ? formatHandicapCn(c.line) : '—'
  const parts: string[] = [`汉字对照 ${cn}`]
  if (c.hw == null && c.aw == null) parts.push('暂无水位')
  else parts.push(`主水 ${fmtNum(c.hw)} · 客水 ${fmtNum(c.aw)}`)
  if (c.source === '5df' && (c.waterSource === 'actual' || c.waterSource == null && c.hw != null)) {
    parts.push('5DF 补数')
  }
  if (c.bookLane === 'macau_5df') parts.push('并列路 macau_5df（不同手工澳门跨源比较）')
  return parts.join('；')
}

/** 0.3.21 竞彩胜平负主格文案 */
export function formatJc1x2Cell(
  c: {
    home: number | null
    draw: number | null
    away: number | null
    incomplete: boolean
    missingReason: string | null
    outOfWindow: boolean
    available: boolean | null
    complete: boolean | null
  } | null | undefined,
  side: 'home' | 'draw' | 'away',
): string {
  if (!c) return '—'
  if (isCollectorEmpty(c.missingReason) || (c.available === false && c.home == null && c.draw == null && c.away == null)) {
    return '暂无竞彩官方数据'
  }
  if (c.outOfWindow) {
    // 不当完整官方即时：主格灰字提示，数值放悬停对照
    return '自采超出 11:00–11:20'
  }
  if (c.incomplete || c.missingReason === 'legacy_home_only' || c.missingReason === 'jc_1x2_incomplete') {
    // 不拿单边 home 冒充完整盘
    return '竞彩胜平负不完整'
  }
  const v = c[side]
  return typeof v === 'number' && Number.isFinite(v) ? fmtNum(v, 2) : '—'
}

export function jc1x2Tip(
  c: {
    home: number | null
    draw: number | null
    away: number | null
    incomplete: boolean
    missingReason: string | null
    outOfWindow: boolean
    source: string | null
    fetchLagMin: number | null
    alt: { home?: number | null; draw?: number | null; away?: number | null; captured_at?: string | null; out_of_window?: boolean | null } | null
  } | null | undefined,
): string | null {
  if (!c) return null
  const lines: string[] = []
  if (isCollectorEmpty(c.missingReason)) lines.push('暂无竞彩官方数据（本地采集器未接通或尚未采集）')
  else if (c.incomplete || c.missingReason === 'legacy_home_only') {
    lines.push('竞彩胜平负不完整，缺平/负等项；不拿单边主胜冒充完整盘')
    if (c.home != null) lines.push(`仅有主胜 ${fmtNum(c.home, 2)}（对照，非完整市场）`)
  }
  if (c.outOfWindow) {
    lines.push('自采超出 11:00–11:20，不当完整官方即时')
    if (c.alt) {
      lines.push(
        `对照 alt：胜 ${fmtNum(c.alt.home, 2)} / 平 ${fmtNum(c.alt.draw, 2)} / 负 ${fmtNum(c.alt.away, 2)}${c.alt.captured_at ? `（${c.alt.captured_at}）` : ''}`,
      )
    }
  }
  if (c.source) lines.push(`来源 ${c.source}`)
  if (c.fetchLagMin != null) lines.push(`延迟 ${c.fetchLagMin} 分钟`)
  if (!lines.length && (c.home != null || c.draw != null || c.away != null)) {
    lines.push(`胜 ${fmtNum(c.home, 2)} / 平 ${fmtNum(c.draw, 2)} / 负 ${fmtNum(c.away, 2)}`)
  }
  return lines.length ? lines.join('；') : null
}

export function formatJcHhadLine(
  c: {
    goalLine: number | null
    decisionLine: number | null
    currentLine: number | null
    postDecisionLineChange: boolean
    fromHist: boolean
    incomplete: boolean
    missingReason: string | null
    outOfWindow: boolean
    available: boolean | null
    home: number | null
    draw: number | null
    away: number | null
  } | null | undefined,
): string {
  if (!c) return '—'
  if (isCollectorEmpty(c.missingReason) || (c.available === false && c.goalLine == null)) return '暂无竞彩官方数据'
  if (c.outOfWindow) return '自采超出 11:00–11:20'
  if (c.incomplete) return '竞彩让球胜平负不完整'
  if (c.goalLine == null && c.decisionLine == null) return '—'
  const line = c.goalLine ?? c.decisionLine
  const odds =
    c.home != null || c.draw != null || c.away != null
      ? ` ${fmtNum(c.home, 2)}/${fmtNum(c.draw, 2)}/${fmtNum(c.away, 2)}`
      : ''
  return `${formatHandicapLine(line)}${odds}`
}

export function jcHhadTip(
  c: {
    goalLine: number | null
    decisionLine: number | null
    currentLine: number | null
    postDecisionLineChange: boolean
    fromHist: boolean
    lineRev: number
    missingReason: string | null
    outOfWindow: boolean
    source: string | null
    home: number | null
    draw: number | null
    away: number | null
  } | null | undefined,
): string | null {
  if (!c) return null
  const lines: string[] = []
  if (isCollectorEmpty(c.missingReason)) lines.push('暂无竞彩官方数据（本地采集器未接通或尚未采集）')
  if (c.missingReason === 'no_line_visible_at_decision') lines.push('决策时刻无可开让球线')
  if (c.goalLine != null || c.decisionLine != null) {
    lines.push(`决策线 ${formatHandicapLine(c.goalLine ?? c.decisionLine)}（主格只用决策时刻线）`)
  }
  if (c.fromHist) lines.push('决策线来自 hist')
  if (c.postDecisionLineChange) {
    lines.push(
      `决策后让球线已变：当前线 ${c.currentLine != null ? formatHandicapLine(c.currentLine) : '—'}（仅悬停对照，不计入该点特征）`,
    )
  }
  if (c.outOfWindow) lines.push('自采超出 11:00–11:20，不当完整官方即时')
  if (c.lineRev > 0) lines.push(`换盘次数 line_rev=${c.lineRev}`)
  if (c.source) lines.push(`来源 ${c.source}`)
  if (c.home != null || c.draw != null || c.away != null) {
    lines.push(`胜 ${fmtNum(c.home, 2)} / 平 ${fmtNum(c.draw, 2)} / 负 ${fmtNum(c.away, 2)}`)
  }
  return lines.length ? lines.join('；') : null
}

/** 产出时间 → 🔒 已冻结 HH:mm（与竞彩日不同天时带上月-日，避免误读） */
export function fmtFrozen(row: SheetRow | undefined): string {
  if (!row?.producedAt) return '—'
  const d = dayjs(row.producedAt.replace(' ', 'T'))
  if (!d.isValid()) return `🔒 已冻结 ${row.producedAt}`
  const sameDay = d.format('YYYY-MM-DD') === row.date
  return `🔒 已冻结 ${d.format(sameDay ? 'HH:mm' : 'MM-DD HH:mm')}`
}




/** 推迟场：mid / close 目标时刻依据（后端 schedule.postpone_target_basis） */
export const POSTPONE_BASIS_TIP: Record<string, string> = {
  original: '推迟消息晚于原定目标时刻，目标时刻按原定开赛时间算',
  announced_new: '推迟消息在原定目标时刻前已发布，目标时刻按新开赛时间算',
  original_ts_unknown: POSTPONE_TS_UNKNOWN_TIP,
}

/**
 * 推迟 / 占位 / 迟确认 在中盘、临盘格上的补充悬停。
 * 推迟依据：例外场写在（真实）列（real=true），其它场写在（规则）列。
 */
export function phaseExtraTip(r: SheetRow, phase: 'mid' | 'close', real: boolean, cell: AhCell | null | undefined): string | null {
  const lines: string[] = []
  if (r.postponed && r.postponeTargetBasis && real === r.phaseException) {
    const b = r.postponeTargetBasis[phase]
    if (b) lines.push(POSTPONE_BASIS_TIP[b] ?? `推迟场目标时刻依据：${b}`)
  }
  // 0.3.20 占位四字段：格子级优先，否则比赛级
  const pending = cell?.phasePending || r.phasePending
  const featuresOk = cell ? cell.featuresOk : r.featuresOk
  const late = cell?.phaseAssignLate || r.phaseAssignLate
  if (pending) lines.push(PHASE_PENDING_TIP)
  if (featuresOk === false) lines.push(FEATURES_NOT_OK_TIP)
  if (late) lines.push(PHASE_ASSIGN_LATE_TIP)
  return lines.length ? lines.join('；') : null
}

/** 带时区或无时区（按北京时间）的时间串 → 显示 */
export function fmtTime(s: string | null | undefined, f = 'MM-DD HH:mm'): string | null {
  if (!s) return null
  const d = dayjs(s.replace(' ', 'T'))
  return d.isValid() ? d.format(f) : null
}

/** 变化格子提示：各公司初盘/临盘实际时间窗不同 */
export function changeSpan(row: SheetRow, book: AhBook): string {
  const o = row.ah[book].open?.recordedAt
  const c = row.ah[book].close?.recordedAt
  if (!o || !c) return '初盘/临盘时间未知（旧库导入，无记录时间）'
  const to = dayjs(o.replace(' ', 'T'))
  const tc = dayjs(c.replace(' ', 'T'))
  if (!to.isValid() || !tc.isValid()) return '初盘/临盘抓取时间无法识别'
  const hours = tc.diff(to, 'minute') / 60
  return `初盘 ${to.format('MM-DD HH:mm')} → 临盘 ${tc.format(to.isSame(tc, 'day') ? 'HH:mm' : 'MM-DD HH:mm')}，跨 ${Number(hours.toFixed(1))} 小时`
}

// ───────────────────────── 构造 ─────────────────────────

/** 例外场（后端 phase_exception=true）的中盘/临盘悬停说明；前端不自己判定范围 */
export const EXCEPTION_TIP = EXCEPTION_DESC
const LEGACY_TIP = '旧手工数据，按旧采集时刻抓取，非规则时刻'

function isLegacy(c: { basis: string | null; source: string | null }): boolean {
  return /legacy/.test(`${c.basis ?? ''}|${c.source ?? ''}`)
}

// ───────────────────────── 初盘来源 / 可用性 / 返还率基准 文案（集中放，便于以后改）─────────────────────────

/** 初盘 usable_at_mid / usable_at_close 任一为 false 时的悬停文案（术语文档「api_opening 的处理」） */
export const OPEN_UNUSABLE_TIP = '初盘时间未知，可能晚于决策时点，不参与特征计算'
/** open_basis=legacy_import 且可用性未判断（null）时的悬停文案 */
export const OPEN_LEGACY_USABILITY_TIP = '旧手工数据，未判断可用性'
/** legacy_import 带 ts_inferred=true、对应决策点 usable=true（0.3.17） */
export const OPEN_LEGACY_INFERRED_OK_TIP = '旧手工数据，按 11:10 采集推定在决策前'
/** legacy_import 带 ts_inferred=true、某决策点 usable=false；which = 「中盘」「临盘」或「中盘、临盘」 */
export const openLegacyInferredLateTip = (which: string) =>
  `旧手工数据按 11:10 采集，晚于${which}决策时点，不参与该点特征计算`
/** api_opening 的来源说明 */
export const OPEN_API_TIP = '初盘来自接口开盘价（API），开盘时间未知'
/** 返还率基准说明 */
export const RR_BASELINE_TIP: Record<string, string> = {
  empirical: '按该公司历史中位数',
  fixed_fallback: '基准样本不足 20 场，改按兜底线判断',
}

const PHASE_SHORT: Record<Phase, string> = { open: '初', mid: '中', close: '临' }

/** 初盘来源与可用性悬停；前端只读后端标记，不自行判定 */
export function openInfoTip(info: OpenInfo | null, withApiNote = false): string | null {
  if (!info) return null
  const lines: string[] = []
  if (info.basis === 'api_opening' && withApiNote) lines.push(OPEN_API_TIP)
  const midBad = info.usableAtMid === false
  const closeBad = info.usableAtClose === false
  if (info.tsInferred && (midBad || closeBad)) {
    lines.push(openLegacyInferredLateTip(midBad && closeBad ? '中盘、临盘' : midBad ? '中盘' : '临盘'))
  } else if (info.tsInferred && (info.usableAtMid === true || info.usableAtClose === true)) {
    lines.push(OPEN_LEGACY_INFERRED_OK_TIP)
  } else if (midBad || closeBad) {
    const which = midBad && closeBad ? '中盘、临盘决策点' : midBad ? '中盘决策点' : '临盘决策点'
    const earliest = fmtTime(info.earliestTsQuoteAt)
    lines.push(`${OPEN_UNUSABLE_TIP}（${which}；本家最早带时间报价 ${earliest ?? '无'}）`)
  } else if (info.basis === 'legacy_import' && info.usableAtMid == null && info.usableAtClose == null) {
    lines.push(OPEN_LEGACY_USABILITY_TIP)
  }
  return lines.length ? lines.join('；') : null
}

/** 返还率格子：baseline_method 说明（无字段不写）；颜色不区分 */
function rrBaselineTip(method: string | null, baseline: number | null): string | null {
  if (!method) return baseline != null ? `基准 ${fmtNum(baseline)}` : null
  const t = RR_BASELINE_TIP[method] ?? null
  if (method === 'empirical' && baseline != null) return `${t}（基准 ${fmtNum(baseline)}）`
  return t
}

/**
 * hl_v0.2 返还率格子的水位来源：档位换算 → 灰字（与规则开关无关，始终显示）；悬停先写来源说明。
 * 真实水位不加任何标记；未标注（缺字段 / null）不改字色，只在悬停里写明。
 */
function withRrWater(
  col: ColDef<SheetRow>,
  getCell: (r: SheetRow) => { returnRate: number | null; waterSource: string | null } | null | undefined,
): ColDef<SheetRow> {
  return {
    ...col,
    cellClassRules: {
      ...(col.cellClassRules as Record<string, unknown> | undefined),
      'hl-rr-tier': (p: { data?: SheetRow }) => {
        const c = p.data ? getCell(p.data) : null
        return c?.returnRate != null && rrWaterKind(c.waterSource) === 'tier'
      },
    } as ColDef<SheetRow>['cellClassRules'],
  }
}

/**
 * hl_v0.3：有经验基准时按基准判色，后端兜底线（满足条件时）也写进悬停供对照；
 * 无基准时兜底线已由规则悬停写出，这里不重复。档位换算 / 未标注水位不写。
 */
function rrFallbackRefTip(
  c: { returnRateBaseline: number | null; waterSource: string | null; fallback: FallbackInfo | null } | null | undefined,
): string | null {
  if (!c?.fallback || c.returnRateBaseline == null || rrWaterKind(c.waterSource) !== 'real') return null
  if (!c.fallback.eligible) return null
  return `${fallbackLineTip(c.fallback)}（hl_v0.3.1 优先按兜底档上色；经验基准仅供对照）`
}

/** 返还率格子悬停的水位来源一句（真实水位 / 空格不写） */
function rrWaterTip(c: { returnRate: number | null; waterSource: string | null } | null | undefined): string | null {
  if (c?.returnRate == null) return null
  const k = rrWaterKind(c.waterSource)
  return k === 'real' ? null : RR_WATER_TIP[k]
}

/**
 * 0.3.17：初盘为空且后端 open_basis_reason=no_open_data 的格子显示空（不写「—」）。
 * withTip=true 时，格子为空、原悬停也为空时补一句 openMissingTip（亚盘线/水位格由 approx_data 规则提示，不用补）。
 */
function blankNoOpen(
  col: ColDef<SheetRow>,
  getReason: (r: SheetRow) => string | null | undefined,
  tipBook: AhBook | 'jc',
  withTip: boolean,
): ColDef<SheetRow> {
  const fmt = col.valueFormatter
  const tip = col.tooltip
  return {
    ...col,
    valueFormatter: (p: ValueFormatterParams<SheetRow>) => {
      if (p.value == null && p.data && getReason(p.data) === 'no_open_data') return ''
      return typeof fmt === 'function' ? fmt(p) : fmtNum(p.value)
    },
    ...(withTip
      ? {
          tooltip: (p: TooltipCallbackParams<SheetRow>) => {
            const base = typeof tip === 'function' ? tip(p) : null
            if (base) return base
            return p.value == null && p.data ? openMissingTip(getReason(p.data), tipBook) : null
          },
        }
      : {}),
  }
}

/** 初盘格加「API」角标（api_opening） */
function withOpenMark(col: ColDef<SheetRow>, getInfo: ((r: SheetRow) => OpenInfo | null) | null): ColDef<SheetRow> {
  if (!getInfo) return col
  return {
    ...col,
    cellClassRules: {
      ...(col.cellClassRules as Record<string, unknown> | undefined),
      'hl-api-mark': (p: { data?: SheetRow }) => (p.data ? getInfo(p.data)?.basis === 'api_opening' : false),
    } as ColDef<SheetRow>['cellClassRules'],
  }
}

/** 后端格子级 hidden_reason → 中文 */
function hiddenTip(code: string | undefined): string | null {
  if (!code) return null
  if (code === 'after_as_of') return '该时点晚于数据截至时刻，暂不显示'
  if (code === 'no_data') return null
  return null
}

/** 列 id → 语义；页面用来统计规则命中 */
export const COL_META = new Map<string, ColMeta>()

function withRules(col: ColDef<SheetRow>, meta: ColMeta, extraTip?: (p: TooltipCallbackParams<SheetRow>) => string | null): ColDef<SheetRow> {
  COL_META.set(col.colId!, meta)
  return {
    ...col,
    cellClassRules: { ...(col.cellClassRules as Record<string, unknown> | undefined), ...buildCellClassRules(meta) } as ColDef<SheetRow>['cellClassRules'],
    tooltip: (p: TooltipCallbackParams<SheetRow>) => {
      const hits = evaluateCell(p.data, meta, p.context as RuleContext)
        .map((h) => h.reason)
        .filter(Boolean)
      const extra = extraTip?.(p)
      const lines = [...(extra ? [extra] : []), ...hits]
      return lines.length ? lines.join('；') : null
    },
  }
}

function numCol(colId: string, headerName: string, getter: (r: SheetRow) => number | null | undefined, d = 3, width = 78): ColDef<SheetRow> {
  return {
    colId,
    headerName,
    width,
    filter: 'agNumberColumnFilter',
    type: 'rightAligned',
    valueGetter: (p) => (p.data ? (getter(p.data) ?? null) : null),
    valueFormatter: (p: ValueFormatterParams<SheetRow>) => fmtNum(p.value, d),
  }
}

function ahGroup(
  book: AhBook,
  pr: Presence,
  view: ViewId,
  hidden: boolean,
  showReal: boolean,
): ColGroupDef<SheetRow> {
  const has = pr.ahLine[book]
  const label = BOOK_LABEL[book]
  const children: ColDef<SheetRow>[] = []
  const lineCol = (phase: Phase): ColDef<SheetRow> =>
    withRules(
      {
        colId: `ah.${book}.${phase}.line`,
        // 开关「对照真实时点」打开时，中盘/临盘列头用完整名「中盘（规则）」以便和（真实）列对照
        headerName: phase !== 'open' && showReal && pr.phaseException ? `${PHASE_LABEL[phase]}（规则）` : PHASE_LABEL[phase],
        headerTooltip: PHASE_HINT[phase],
        width: 72,
        minWidth: 56,
        maxWidth: 88,
        filter: 'agNumberColumnFilter',
        valueGetter: (p) => p.data?.ah[book][phase]?.line ?? null,
        valueFormatter: (p) => {
          const c = p.data?.ah[book][phase]
          if (!c || c.line == null) return phase === 'open' && p.data?.openReason.ah[book] === 'no_open_data' ? '' : '—'
          // 格内只显示盘口数字；开盘时间仅悬停 tip（API/规角标保留）
          return formatAhLineWithWater(c)
        },
        cellClassRules:
          phase !== 'open'
            ? {
                'hl-rule-mark': (p) => !!p.data?.phaseException,
                'hl-kickoff-suspect': (p) => {
                  const c = p.data?.ah[book][phase]
                  return !!(c?.phasePending || (c != null && c.featuresOk === false) || p.data?.phasePending || p.data?.featuresOk === false)
                },
              }
            : { 'hl-api-mark': (p) => p.data?.ah[book].open?.openInfo?.basis === 'api_opening' },
      },
      { kind: 'ahLine', book, phase },
      (p) => {
        const r = p.data
        const c = r?.ah[book][phase]
        if (!r) return null
        const exc = phase !== 'open' && r.phaseException ? EXCEPTION_TIP : null
        if (!c || c.line == null) {
          const h = hiddenTip(r.ah[book].hidden[phase])
          return [exc, h].filter(Boolean).join('；') || null
        }
        if (phase === 'open') {
          const api = c.openInfo?.basis === 'api_opening'
          const legacy = isLegacy(c) ? '（旧手工数据，开盘时刻未记录）' : ''
          const t = api ? '时间未知（接口开盘价）' : (fmtTime(c.recordedAt) ?? `未知${legacy}`)
          return [
            `初盘：${label}第一次开出的盘；开盘时间 ${t}；${formatHandicapLine(c.line)}（主让为正）`,
            ahLineWaterTipExtra(c),
            openInfoTip(c.openInfo),
          ]
            .filter(Boolean)
            .join('；')
        }
        // 只显示后端给的时刻（精确到分钟，不按整点截断）；旧手工数据 target_at/recorded_at 为空，改用 schedule 并说明
        const target = c.targetAt ?? (phase === 'mid' ? r.schedule.midTarget : r.schedule.closeTarget)
        const legacy = isLegacy(c) && !c.targetAt ? `（${LEGACY_TIP}）` : ''
        const time = `目标时间 ${fmtTime(target) ?? '—'}${legacy} / 抓取时间 ${fmtTime(c.recordedAt) ?? '未知'}`
        return [exc, `${formatHandicapLine(c.line)}（主让为正）；${time}`, ahLineWaterTipExtra(c), phaseExtraTip(r, phase, false, c)].filter(Boolean).join('；')
      },
    )
  // 例外场的真实中盘/临盘（赛前 8h / 1h）：开关「对照真实时点」打开后紧跟在规则列后面；只作对照，不参与高亮
  const realCol = (key: 'midReal' | 'closeReal'): ColDef<SheetRow> => {
    const phaseName = key === 'midReal' ? '中盘' : '临盘'
    const hours = key === 'midReal' ? '8' : '1'
    return {
      colId: `ah.${book}.${key}.line`,
      headerName: `${phaseName}（真实）`,
      headerTooltip: `只对例外场（后端标记）：${phaseName}（真实）= 开赛前 ${hours} 小时的快照，作对照；分析、高亮与结算默认用（规则）。其他场留空`,
      width: 124,
      hide: !showReal || !pr.phaseException,
      filter: 'agNumberColumnFilter',
      cellClass: 'sheet-real-col',
      valueGetter: (p) => (p.data?.phaseException ? (p.data.ah[book][key]?.line ?? null) : null),
      valueFormatter: (p) => {
        if (!p.data?.phaseException) return ''
        const c = p.data.ah[book][key]
        if (!c || c.line == null) return '—'
        return formatAhLineWithWater(c)
      },
      tooltip: (p: TooltipCallbackParams<SheetRow>) => {
        const r = p.data
        if (!r) return null
        if (!r.phaseException) return `非例外场，${phaseName}即开赛前 ${hours} 小时，不分规则/真实`
        const c = r.ah[book][key]
        const target = c?.targetAt ?? (key === 'midReal' ? r.schedule.midRealTarget : r.schedule.closeRealTarget)
        const extra = phaseExtraTip(r, key === 'midReal' ? 'mid' : 'close', true, c)
        if (!c || c.line == null) {
          const h = r.ah[book].hidden[key] === 'after_as_of' ? hiddenTip('after_as_of') : '暂无真实时点数据'
          return [`${h}${target ? `（目标时间 ${fmtTime(target)}）` : ''}`, extra].filter(Boolean).join('；')
        }
        return [`${phaseName}（真实）：目标时间 ${fmtTime(target) ?? '—'} / 抓取时间 ${fmtTime(c.recordedAt) ?? '—'} · 主水 ${fmtNum(c.hw)} · 客水 ${fmtNum(c.aw)}`, extra]
          .filter(Boolean)
          .join('；')
      },
    }
  }
  // 每阶段列序：主水 → 盘口 → 客水（有水位时）；无水位只出盘口。盘口格只显示盘口数字，不内嵌水位。
  const showWaterCols = pr.ahWater[book] && view !== 'review'
  const showMid = view === 'snapshot' || pr.ahMid[book]
  const waterSideCol = (phase: Phase, side: 'hw' | 'aw', sideLabel: string): ColDef<SheetRow> => {
    const short = PHASE_SHORT[phase]
    const col = withRules(
      {
        ...numCol(`ah.${book}.${phase}.${side}`, `${sideLabel}${short}`, (r) => r.ah[book][phase]?.[side], 3, 56),
        minWidth: 48,
        maxWidth: 64,
      },
      { kind: 'ahWater', book, phase, side },
    )
    return phase === 'open' ? blankNoOpen(col, (r) => r.openReason.ah[book], book, false) : col
  }
  // open
  if (showWaterCols) children.push(waterSideCol('open', 'hw', '主水'))
  children.push(lineCol('open'))
  if (showWaterCols) children.push(waterSideCol('open', 'aw', '客水'))
  // mid
  if (showMid) {
    if (showWaterCols) children.push(waterSideCol('mid', 'hw', '主水'))
    children.push(lineCol('mid'))
    if (showWaterCols) children.push(waterSideCol('mid', 'aw', '客水'))
    children.push(realCol('midReal'))
  }
  // close
  if (showWaterCols) children.push(waterSideCol('close', 'hw', '主水'))
  children.push(lineCol('close'))
  if (showWaterCols) children.push(waterSideCol('close', 'aw', '客水'))
  children.push(realCol('closeReal'))
  if (pr.ahLive[book]) {
    // 其它时刻的即时快照：只展示最近一条，不参与升降/分歧判定（不同阶段不比较）
    children.push({
      colId: `ah.${book}.live.line`,
      headerName: '即时（最新）',
      headerTooltip: '即时盘口：最新一条即时快照；抓取时间见悬停；不参与高亮判定',
      width: 88,
      minWidth: 64,
      maxWidth: 110,
      columnGroupShow: 'open',
      filter: 'agNumberColumnFilter',
      valueGetter: (p) => p.data?.ah[book].live.filter((c) => c.label !== 'rule_1110').at(-1)?.line ?? null,
      valueFormatter: (p) => {
        const c = p.data?.ah[book].live.filter((c) => c.label !== 'rule_1110').at(-1)
        if (!c || c.line == null) return '—'
        return formatAhLineWithWater(c)
      },
      tooltip: (p) => {
        const live = (p.data?.ah[book].live ?? []).filter((c) => c.label !== 'rule_1110')
        if (!live.length) return null
        return live
          .map((c) => `抓取时间 ${c.recordedAt} · ${formatHandicapLine(c.line)} · ${ahLineWaterTipExtra(c)}`)
          .join('\n')
      },
    })
  }
  children.push(
    withRules(
      {
        colId: `ah.${book}.chg`,
        headerName: '变化',
        width: 92,
        filter: 'agNumberColumnFilter',
        valueGetter: (p) => {
          const m = p.data ? lineMove(p.data.ah[book].open?.line ?? null, p.data.ah[book].close?.line ?? null) : null
          if (!m) return null
          if (m.kind === 'none') return 0
          return m.kind === 'up' ? m.amount : -m.amount
        },
        valueFormatter: (p) =>
          p.data ? formatMove(lineMove(p.data.ah[book].open?.line ?? null, p.data.ah[book].close?.line ?? null)) : '—',
      },
      { kind: 'ahChg', book },
      (p) => {
        if (book === 'pinnacle' && !p.data?.ah.pinnacle.open && !p.data?.ah.pinnacle.close) return '暂无平博数据'
        const span = p.data ? changeSpan(p.data, book) : null
        return `临盘−初盘（同一公司），按让球方看：↑升盘 ↓降盘${span ? `；${span}` : ''}`
      },
    ),
  )
  // 水位列已按阶段交错插入（主水→盘口→客水）；此处只接返还率
  if (pr.ahRr[book]) {
    const rrPhases: Phase[] = pr.ahMid[book] ? ['open', 'mid', 'close'] : ['open', 'close']
    for (const phase of rrPhases) {
      children.push(
        withRrWater(
          withRules(
            {
              ...numCol(`ah.${book}.${phase}.rr`, `返还率${PHASE_SHORT[phase]}`, (r) => r.ah[book][phase]?.returnRate, 3, 86),
              // 赔率健康视图：返还率是主角，默认显示；其它视图放在分组展开区
              columnGroupShow: view === 'health' ? undefined : 'open',
            },
            { kind: 'ahRr', book, phase },
            (p) => {
              const c = p.data?.ah[book][phase]
              if (c?.returnRate == null) return null
              return [rrWaterTip(c), rrBaselineTip(c.baselineMethod, c.returnRateBaseline), rrFallbackRefTip(c)].filter(Boolean).join('；') || null
            },
          ),
          (r) => r.ah[book][phase],
        ),
      )
    }
  }
  return {
    groupId: `ah.${book}`,
    headerName: has ? `${label}亚盘` : `${label}亚盘（暂无数据）`,
    headerTooltip: !has
      ? book === 'pinnacle'
        ? '暂无平博数据（两库目前都没有，待后端补数；平博相关规则缺数据不判）'
        : `${label}暂无数据（待后端补数）`
      : !pr.ahWater[book]
        ? book === 'macau'
          ? '本页澳门暂无水位（接口有 home/away_water 就会显示，不写死永远无）；无水位时结算可回落 0.95'
          : `${label}暂无水位`
        : book === 'pinnacle'
          ? '平博：用户唯一有资金的账户'
          : '皇冠/威廉：盘口列只显示盘口；主水/客水独立列。water_source=tier_midpoint 为档位中点近似，与 actual 真实水位区分',
    headerClass: book === 'pinnacle' ? 'sheet-hdr-pinnacle' : undefined,
    // 皇冠/威廉等默认展开，避免中盘盘口/水位被分组收起藏掉
    openByDefault: true,
    marryChildren: true,
    children: children.map((c) => ({ ...c, hide: hidden || c.hide === true })),
  }
}

function x1x2Group(book: AhBook, pr: Presence, hidden: boolean): ColGroupDef<SheetRow> | null {
  if (!pr.x1x2Home[book] && !pr.x1x2DrawAway[book] && !pr.x1x2Rr[book] && !pr.closingRef[book]) return null
  const children: ColDef<SheetRow>[] = []
  const sides = pr.x1x2DrawAway[book]
    ? ([
        ['home', '胜'],
        ['draw', '平'],
        ['away', '负'],
      ] as const)
    : ([['home', '主胜']] as const)
  for (const phase of ['open', 'close'] as const) {
    for (const [side, l] of sides) {
      children.push(
        withOpenMark(
          (phase === 'open' ? (c: ColDef<SheetRow>) => blankNoOpen(c, (r) => r.openReason.x1x2[book], book, false) : (c: ColDef<SheetRow>) => c)(
            withRules(
              numCol(`x.${book}.${phase}.${side}`, `${l}${phase === 'open' ? '初' : '临'}`, (r) => r.x1x2[book][phase]?.[side], 2, 70),
              { kind: 'x1x2Odds', book, phase, side },
              (p) => {
                if (phase !== 'open' || !p.data) return null
                const o = p.data.x1x2[book].open
                return o ? openInfoTip(o.openInfo, true) : openMissingTip(p.data.openReason.x1x2[book], book)
              },
            ),
          ),
          phase === 'open' ? (r) => r.x1x2[book].open?.openInfo ?? null : null,
        ),
      )
    }
    if (pr.x1x2Rr[book]) {
      children.push(
        withRrWater(
          withRules(
            numCol(`x.${book}.${phase}.rr`, `返还率${phase === 'open' ? '初' : '临'}`, (r) => r.x1x2[book][phase]?.returnRate, 3, 86),
            { kind: 'x1x2Rr', book, phase },
            (p) => {
              const c = p.data?.x1x2[book][phase]
              if (c?.returnRate == null) return null
              return [rrWaterTip(c), rrBaselineTip(c.baselineMethod, c.returnRateBaseline), rrFallbackRefTip(c)].filter(Boolean).join('；') || null
            },
          ),
          (r) => r.x1x2[book][phase],
        ),
      )
    }
  }
  for (const phase of pr.kellyPhases[book] ?? []) {
    const pl = phase === 'open' ? '初' : '临'
    for (const [side, l] of [
      ['home', '胜'],
      ['draw', '平'],
      ['away', '负'],
    ] as const) {
      const kellyCol = withRules(
          numCol(`x.${book}.${phase}.k_${side}`, `凯利${l}${pl}`, (r) => r.x1x2[book][phase]?.kelly?.[side], 3, 76),
          { kind: 'kelly', book, phase, side },
          (p) => {
            const c = p.data?.x1x2[book][phase]
            if (c?.kelly?.[side] == null) return null
            const nAvg = c.nAvg != null ? `对照其他 ${c.nAvg} 家平均` : null
            const base =
              book === 'pinnacle'
                ? `平博自身按多家平均基准${nAvg ? `（${nAvg}）` : ''}`
                : c.kellyBase === 'multi_avg'
                  ? `⚑ 缺平博欧赔，已退回多家平均基准${nAvg ? `（${nAvg}）` : ''}`
                  : '基准：平博去水概率'
            return base
          },
        )
      children.push({
        ...kellyCol,
        cellClassRules: {
          ...kellyCol.cellClassRules,
          // 非平博公司缺平博欧赔、退回多家平均基准时加角标
          'hl-corner': (p) => book !== 'pinnacle' && p.data?.x1x2[book][phase]?.kellyBase === 'multi_avg',
        },
      })
    }
  }
  if (pr.closingRef[book]) {
    // 接口收盘价：报价时刻未知，不是临盘；灰字、标参考，不进 COL_META、不参与任何规则；未完赛不显示
    children.push({
      colId: `x.${book}.closingRef`,
      headerName: '收盘·参考',
      headerTooltip: '接口给的欧赔收盘价，报价时刻未知，不能当临盘用；不参与返还率、凯利和任何高亮；只在完赛后显示',
      width: 170,
      cellClass: 'sheet-ref-col',
      filter: false,
      sortable: false,
      valueGetter: (p) => {
        const c = p.data?.x1x2ClosingRef[book]
        if (!c) return null
        return [c.home, c.draw, c.away].map((v) => fmtNum(v, 2)).join(' / ')
      },
      valueFormatter: (p) => (p.value as string | null) ?? '',
      tooltip: (p: TooltipCallbackParams<SheetRow>) => {
        const c = p.data?.x1x2ClosingRef[book]
        if (!c) return null
        return `胜/平/负 接口收盘价（参考）；报价时刻未知${c.fetchedAt ? `，抓取于 ${fmtTime(c.fetchedAt)}` : ''}`
      },
    })
  }
  return {
    groupId: `x.${book}`,
    headerName: `欧赔·${BOOK_LABEL[book]}`,
    headerTooltip: pr.x1x2DrawAway[book] ? undefined : '只有主胜赔率，缺少平和负的赔率。',
    marryChildren: true,
    children: children.map((c) => ({ ...c, hide: hidden || c.hide === true })),
  }
}

export interface BuildOpts {
  view: ViewId
  presence: Presence
  /** 对照真实时点：显示「中盘（真实）」「临盘（真实）」列（默认关，只对例外场有意义） */
  showReal: boolean
  /** 公司（列）筛选：只显示这些公司的盘口列 */
  books: AhBook[]
}

/** 各视图默认显示哪些分组 */
const VIEW_GROUPS: Record<ViewId, { ahBooks: AhBook[] | 'all'; x1x2: boolean; jc: boolean; pred: 'brief' | 'full' | 'none'; result: boolean }> = {
  snapshot: { ahBooks: 'all', x1x2: true, jc: true, pred: 'brief', result: false },
  health: { ahBooks: 'all', x1x2: true, jc: true, pred: 'none', result: false },
  review: { ahBooks: ['pinnacle', 'macau'], x1x2: false, jc: false, pred: 'full', result: true },
}

export function buildColumnDefs({ view, presence: pr, books, showReal }: BuildOpts): AnyCol[] {
  COL_META.clear()
  const vg = VIEW_GROUPS[view]
  const cols: AnyCol[] = []

  cols.push({
    groupId: 'base',
    headerName: '基本',
    children: [
      { colId: 'jcNo', field: 'jcNo', headerName: '编号', width: 78, pinned: 'left', filter: 'agTextColumnFilter' },
      { colId: 'date', field: 'date', headerName: '竞彩日', width: 104, pinned: 'left', filter: 'agTextColumnFilter', sort: 'asc', sortIndex: 0 },
      {
        colId: 'kickoff',
        headerName: '开赛',
        width: 104,
        pinned: 'left',
        valueGetter: (p) => p.data?.kickoffAt ?? null,
        valueFormatter: (p) => {
          if (!p.data) return '—'
          // 0.3.20：开赛时间未确认 → 灰字「待归阶段」
          if (p.data.phasePending) return PHASE_PENDING_LABEL
          return p.data.kickoffLabel ?? '—'
        },
        tooltip: (p: TooltipCallbackParams<SheetRow>) => p.data?.kickoffNote ?? null,
        cellClassRules: {
          // 0.3.19：只有后端 kickoff_jc_conflict=true 才挂灰色「核」（null / false 不显示）
          'hl-check-mark': (p) => p.data?.kickoffJcConflict === true,
          // 0.3.20：开赛时间未确认 / 占位推算 → 灰字
          'hl-kickoff-suspect': (p) => p.data?.phasePending === true || p.data?.featuresOk === false,
        },
        filter: 'agTextColumnFilter',
        sort: 'asc',
        sortIndex: 1,
      },
      { colId: 'league', field: 'league', headerName: '联赛', width: 92, pinned: 'left', filter: 'agTextColumnFilter' },
      {
        colId: 'home',
        field: 'home',
        headerName: '主队',
        width: 100,
        pinned: 'left',
        filter: 'agTextColumnFilter',
        valueFormatter: (p) => {
          const name = (p.value as string) ?? '—'
          return p.data?.manualReview ? `${name} · 人工复核中` : name
        },
        cellClassRules: { 'hl-manual-review': (p) => p.data?.manualReview === true },
        tooltip: (p: TooltipCallbackParams<SheetRow>) => {
          const r = p.data
          if (!r) return null
          if (!r.manualReview) return `${r.home} vs ${r.away}`
          return `${r.home} vs ${r.away}；人工复核中，赛果与结算暂不显示${r.manualReviewReason ? `（后端原因：${r.manualReviewReason}）` : ''}`
        },
      },
      {
        colId: 'away',
        field: 'away',
        headerName: '客队',
        width: 100,
        pinned: 'left',
        filter: 'agTextColumnFilter',
        valueFormatter: (p) => (p.value as string) ?? '—',
        cellClassRules: { 'hl-manual-review': (p) => p.data?.manualReview === true },
        tooltip: (p: TooltipCallbackParams<SheetRow>) => {
          const r = p.data
          if (!r) return null
          return `${r.home} vs ${r.away}`
        },
      },
    ],
  })

  for (const b of AH_BOOKS) {
    const inView = vg.ahBooks === 'all' || vg.ahBooks.includes(b)
    cols.push(ahGroup(b, pr, view, !(inView && books.includes(b)), showReal))
  }

  // 0.3.22：macau_5df 并列列（有数据才出）；与手工澳门分路，规则只比本路 open→close
  if (pr.macau5df && view !== 'review') {
    const phases: Array<'open' | 'mid' | 'close'> = ['open', 'mid', 'close']
    const children: ColDef<SheetRow>[] = []
    for (const phase of phases) {
      const short = PHASE_SHORT[phase]
      // 主水 → 盘口 → 客水（与主列亚盘同序）
      children.push(
        withRules(
          {
            ...numCol(
              `ah.macau_5df.${phase}.hw`,
              `主水${short}`,
              (r) => r.ahMacau5df?.[phase]?.hw,
              3,
              56,
            ),
            minWidth: 48,
            maxWidth: 64,
          },
          { kind: 'ahWater', book: 'macau_5df', phase, side: 'hw' },
        ),
      )
      children.push(
        withRules(
          {
            colId: `ah.macau_5df.${phase}.line`,
            headerName: PHASE_LABEL[phase],
            headerTooltip: '澳门5DF（并列路，真实水位）；升降盘/水位异动只与本路初/临比，不和手工澳门跨源',
            width: 72,
            minWidth: 56,
            maxWidth: 88,
            filter: 'agNumberColumnFilter',
            valueGetter: (p) => p.data?.ahMacau5df?.[phase]?.line ?? null,
            valueFormatter: (p) => formatAhLineWithWater(p.data?.ahMacau5df?.[phase]),
          },
          { kind: 'ahLine', book: 'macau_5df', phase },
          (p) => {
            const c = p.data?.ahMacau5df?.[phase]
            if (!c || c.line == null) return '暂无澳门5DF数据'
            return `${formatHandicapLine(c.line)}（主让为正）；${ahLineWaterTipExtra({ ...c, waterSource: c.waterSource, source: c.source, bookLane: c.bookLane ?? 'macau_5df' })}`
          },
        ),
      )
      children.push(
        withRules(
          {
            ...numCol(
              `ah.macau_5df.${phase}.aw`,
              `客水${short}`,
              (r) => r.ahMacau5df?.[phase]?.aw,
              3,
              56,
            ),
            minWidth: 48,
            maxWidth: 64,
          },
          { kind: 'ahWater', book: 'macau_5df', phase, side: 'aw' },
        ),
      )
    }
    children.push(
      withRules(
        {
          colId: 'ah.macau_5df.chg',
          headerName: '变化',
          width: 92,
          valueGetter: (p) => {
            const m = p.data
              ? lineMove(p.data.ahMacau5df?.open?.line ?? null, p.data.ahMacau5df?.close?.line ?? null)
              : null
            if (!m) return null
            if (m.kind === 'none') return 0
            return m.kind === 'up' ? m.amount : -m.amount
          },
          valueFormatter: (p) =>
            p.data
              ? formatMove(lineMove(p.data.ahMacau5df?.open?.line ?? null, p.data.ahMacau5df?.close?.line ?? null))
              : '—',
        },
        { kind: 'ahChg', book: 'macau_5df' },
        () => '临盘−初盘（仅澳门5DF本路，不与手工澳门跨源）',
      ),
    )
    cols.push({
      groupId: 'ah.macau_5df',
      headerName: `${PARALLEL_BOOK_LABEL.macau_5df}亚盘`,
      headerTooltip: '0.3.22 并列列（book_lane=macau_5df）：5DF 补数真实水位；与手工澳门（macau_manual）分列，升降盘/水位异动/返还率禁止跨源',
      openByDefault: true,
      marryChildren: true,
      children,
    })
  }

  for (const b of AH_BOOKS) {
    const g = x1x2Group(b, pr, !(vg.x1x2 && books.includes(b)))
    if (g) cols.push(g)
  }

  if (pr.multiAvg) {
    cols.push({
      groupId: 'multiavg',
      headerName: '多家平均',
      headerTooltip: '澳门/皇冠/威廉/平博各自去水后取平均；只有 4 家，噪声大，仅供参考',
      children: (['home', 'draw', 'away'] as const).map((s, i) => ({
        ...numCol(`mavg.${s}`, ['胜', '平', '负'][i] + '概率', (r) => r.multiAvgProb?.[s], 3, 76),
        hide: view !== 'health',
      })),
    })
  }

  // 0.3.21 竞彩胜平负：有 jc_1x2 键或有赔率就出列；incomplete 与本地采集器无数据都灰字，不拿 home_only 冒充完整
  if (vg.jc && (pr.jcPresent || pr.jcHome || pr.jcDrawAway)) {
    const phases: Array<'open' | 'mid' | 'close'> = ['open', 'mid', 'close']
    const sides = (
      pr.jcDrawAway
        ? ([
            ['home', '胜'],
            ['draw', '平'],
            ['away', '负'],
          ] as const)
        : ([
            ['home', '胜'],
            ['draw', '平'],
            ['away', '负'],
          ] as const)
    ) // 始终三向：不完整时灰字说明，避免「仅主胜」假完整
    const children: ColDef<SheetRow>[] = []
    for (const phase of phases) {
      for (const [s, l] of sides) {
        children.push({
          colId: `jc.${phase}.${s}`,
          headerName: `${l}${PHASE_SHORT[phase]}`,
          width: 108,
          hide: !vg.jc,
          filter: 'agTextColumnFilter',
          valueGetter: (p) => p.data?.jc[phase]?.[s] ?? null,
          valueFormatter: (p) => formatJc1x2Cell(p.data?.jc[phase], s),
          cellClassRules: {
            'hl-approx': (p) => {
              const c = p.data?.jc[phase]
              if (!c) return false
              return (
                isCollectorEmpty(c.missingReason) ||
                c.incomplete ||
                c.outOfWindow ||
                c.missingReason === 'legacy_home_only'
              )
            },
          },
          tooltip: (p: TooltipCallbackParams<SheetRow>) => jc1x2Tip(p.data?.jc[phase]),
        })
      }
    }
    cols.push({
      groupId: 'jc',
      headerName: '竞彩胜平负',
      headerTooltip: '完整盘来自 odds_jc_had；仅 home_only 灰字「不完整」；本地采集器没有数据时灰字「暂无竞彩官方数据」；超窗不当主值',
      openByDefault: true,
      marryChildren: true,
      children,
    })
  }

  // 0.3.21 竞彩让球胜平负：主格决策线；current_line / post_decision 仅悬停
  if (vg.jc && pr.jcHhadPresent) {
    const children: ColDef<SheetRow>[] = []
    for (const phase of ['open', 'mid', 'close'] as const) {
      children.push({
        colId: `jc_hhad.${phase}.line`,
        headerName: `让球${PHASE_LABEL[phase]}`,
        width: 140,
        hide: !vg.jc,
        filter: 'agNumberColumnFilter',
        valueGetter: (p) => p.data?.jcHhad[phase]?.goalLine ?? p.data?.jcHhad[phase]?.decisionLine ?? null,
        valueFormatter: (p) => formatJcHhadLine(p.data?.jcHhad[phase]),
        cellClassRules: {
          'hl-approx': (p) => {
            const c = p.data?.jcHhad[phase]
            if (!c) return false
            return isCollectorEmpty(c.missingReason) || c.incomplete || c.outOfWindow || c.postDecisionLineChange
          },
        },
        tooltip: (p: TooltipCallbackParams<SheetRow>) => jcHhadTip(p.data?.jcHhad[phase]),
      })
    }
    cols.push({
      groupId: 'jc_hhad',
      headerName: '竞彩让球胜平负',
      headerTooltip: '主格为决策时刻让球线（goal_line/decision_line）；决策后换线仅悬停对照，不与旧线拼格',
      openByDefault: true,
      marryChildren: true,
      children,
    })
  }

  const predHidden = vg.pred === 'none'
  const predChildren: ColDef<SheetRow>[] = [
    {
      colId: 'pred.direction',
      field: 'direction',
      headerName: '方向',
      width: 76,
      filter: 'agTextColumnFilter',
      valueFormatter: (p) => (p.value ?? '—') as string,
      cellClassRules: {
        'sheet-dir-home': (p) => p.value === '主',
        'sheet-dir-away': (p) => p.value === '客',
        'sheet-dir-skip': (p) => p.value === '不下注',
      },
      hide: predHidden,
    },
    { ...numCol('pred.stake', '份', (r) => r.stake, 0, 60), hide: predHidden || vg.pred === 'brief' },
  ]
  if (pr.confidence) predChildren.push({ ...numCol('pred.confidence', '置信度', (r) => r.confidence, 2, 76), hide: predHidden || vg.pred === 'brief' })
  predChildren.push(
    withRules(
      {
        colId: 'pred.producedAt',
        headerName: '产出时间',
        width: 150,
        valueGetter: (p) => p.data?.producedAt ?? null,
        valueFormatter: (p) => fmtFrozen(p.data),
        filter: 'agTextColumnFilter',
        hide: predHidden,
      },
      { kind: 'producedAt' },
      (p) => {
        const r = p.data
        if (!r?.producedAt) return '无预测'
        const late = r.kickoffAt && dayjs(r.producedAt.replace(' ', 'T')).isAfter(dayjs(r.kickoffAt))
        return `预测已冻结，只读。产出（入库）时间 ${r.producedAt}${late ? '；晚于开赛，为历史回填记录' : ''}${r.ledgerNote ? `；台账备注：${r.ledgerNote}` : ''}`
      },
    ),
  )
  cols.push({ groupId: 'pred', headerName: '预测 V3', children: predChildren })

  const resHidden = !vg.result
  const dash = (p: { data?: SheetRow }) => !p.data?.finished
  const isVoid = (r: SheetRow | undefined) => r?.settleCode === 'void_postponed'
  cols.push({
    groupId: 'res',
    headerName: '赛果',
    children: [
      { colId: 'res.score', field: 'score', headerName: '比分', width: 70, hide: resHidden, valueFormatter: (p) => (dash(p) ? '—' : (p.value as string)), tooltip: (p) => (p.data?.finished ? null : (p.data?.hiddenReason ?? '未完场')) },
      { colId: 'res.wdl', field: 'wdl', headerName: '胜平负', width: 76, hide: resHidden, filter: 'agTextColumnFilter', valueFormatter: (p) => (dash(p) ? '—' : ((p.value as string) ?? '—')) },
      withRules(
        {
          colId: 'res.settle',
          headerName: '结算',
          width: 76,
          hide: resHidden,
          filter: 'agTextColumnFilter',
          valueGetter: (p) =>
            (p.data?.finished || isVoid(p.data)) && p.data?.settleCode ? SETTLE_LABEL[p.data.settleCode] : null,
          valueFormatter: (p) => (p.value as string) ?? '—',
        },
        { kind: 'settle' },
        (p) => {
          const r = p.data
          if (isVoid(r)) return VOID_POSTPONED_TIP
          if (!r?.finished) return r?.hiddenReason ?? '未完场'
          if (r.settleSource === 'backend') return `后端结算${r.settleVersion ? `（口径 ${r.settleVersion}）` : ''}`
          return null
        },
      ),
      withRules(
        {
          // 推迟作废：盈亏一律「—」（后端 pnl 为空，前端也不算）
          ...numCol('res.pnl', '盈亏(份)', (r) => (r.finished && !isVoid(r) ? r.pnlUnits : null), 2, 86),
          hide: resHidden,
          valueFormatter: (p) => {
            if (typeof p.value !== 'number') return '—'
            const v = Number(p.value.toFixed(2))
            return v > 0 ? `+${v}` : String(v)
          },
        },
        { kind: 'pnl' },
        (p) =>
          isVoid(p.data)
            ? VOID_POSTPONED_TIP
            : p.data?.finished && p.data.settleVersion
              ? `后端结算（口径 ${p.data.settleVersion}）`
              : null,
      ),
    ],
  })

  return cols
}

export function allMetas(): ColMeta[] {
  return [...COL_META.values()]
}
