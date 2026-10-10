/**
 * 数据表页 · 高亮规则预设（口径版本见 thresholds.ts CONFIG_VERSION，当前 hl_v0.3.1）。
 *
 * hl_v0.3.1（0.3.20）：返还率兜底按公司×盘种×阶段的 P10/P25/P90；优先读 fallback_hl_level。
 * hl_v0.3（0.3.19）：只改上色口径，不改策略。
 *   - 凯利：轻 = 凯利 − 返还率 ≥ 0.02，中 = 凯利 ≥ 1.02，不设重档；阈值按主/平/客各存一套（现在三套相同）。
 *   - 返还率兜底：读后端每格 fallback_p10/p25/p90/n/eligible/level：
 *     <P10 中、<P25 轻、≥P90 中性色「返还率偏高」（不算风险、不进导出、不计入风险统计）；
 *     不满足（eligible≠true 或 n < 100）灰字「样本不足，暂不判断」，不上色。
 *   - 水位异动：只比初→临，可比性只看临盘格后端 water_move_eligible（删掉 0.3.18 的中→临分支）；
 *     tier_cross_mid=true 照常上色，悬停「中盘曾换盘，已回到初盘盘口」；tier_cross.kind 区分 line / water_tier 写悬停。
 *
 * hl_v0.2：返还率（亚盘 / 欧赔）和 §2 水位异动只对 water_source=actual 的格子上色（return_hl_water=real_only）。
 * 水位异动遇档位换算只在悬停写「跨档：X→Y（档位换算，幅度不精确）」（X/Y 为初、临盘水位值）。
 * 档位中点换算（tier_midpoint）的格子不上色；「档位换算也上色（仅排查用）」打开时按原阈值上色并带 hl-debug-tier 排查标记。
 * EXPORT-NOTE(hl_v0.2)：排查色（Hit.debug=true / class hl-debug-tier）不进任何导出、不进方案计算；
 *   将来 exceljs 导出取色请用 exportableHits()，不要直接用 evaluateCell()。
 *
 * 每条规则 = { id, name(中文), views, columns, condition(中文说明), style{kind, levels}, defaultOn, available, note, evaluate }。
 * 页面把规则翻译成 AG Grid 的 cellClassRules（见 buildCellClassRules），开关存 localStorage。
 * 阈值一律从 thresholds.ts 取。
 */
import type { AhBook, AhCell, AhRuleBook, FallbackInfo, Phase, SheetRow, ViewId, X1x2Cell } from './types'
import { AH_BOOKS, BOOK_LABEL, PHASE_LABEL } from './types'
import { formatHandicap, givingSide, lineMove } from './handicap'
import {
  AH_RETURN_RATE_FALLBACK,
  CONFIG_VERSION,
  DISAGREE,
  KELLY,
  KELLY_THRESHOLDS,
  LINE_MOVE,
  RR_FALLBACK,
  RETURN_RATE_BASELINE_DELTA,
  WATER_MOVE,
  WATER_SOURCE_REAL,
  WATER_SOURCE_TIER,
  X1X2_RETURN_RATE_FALLBACK,
} from './thresholds'

export { CONFIG_VERSION }

export type Level = 1 | 2 | 3

/** 列的语义（rules 只认语义，不认具体列名） */
export type ColKind =
  | 'ahLine'
  | 'ahChg'
  | 'ahWater'
  | 'ahRr'
  | 'x1x2Odds'
  | 'x1x2Rr'
  | 'kelly'
  | 'settle'
  | 'pnl'
  | 'producedAt'
  | 'other'

export interface ColMeta {
  kind: ColKind
  /** 同路分路键：macau↔macau、macau_5df↔macau_5df，禁止跨源 */
  book?: AhRuleBook
  phase?: Phase
  /** 水位列：主水/客水；欧赔/凯利列：胜/平/负 */
  side?: 'hw' | 'aw' | 'home' | 'draw' | 'away'
}

export interface Hit {
  className: string
  reason: string
  /** 只在悬停提示里显示、不着色、不计入命中（如分歧 0.25） */
  tooltipOnly?: boolean
  /** hl_v0.2 排查色（档位换算也上色）：只在页面显示，不进导出、不进方案计算 */
  debug?: boolean
  /** hl_v0.3 中性色（返还率偏高 ≥ P90）：只在页面显示，不算风险、不进导出、不计入风险统计（单独计数） */
  neutral?: boolean
  /** hl_v0.3 灰字「样本不足，暂不判断」：不上色、不进导出、不计入命中（单独计数） */
  insufficient?: boolean
}

export interface RuleDef {
  id: string
  name: string
  views: ViewId[]
  columns: ColKind[]
  /** 中文条件说明（图例里显示） */
  condition: string
  style: { kind: 'font' | 'fill'; levels: Level[] }
  defaultOn: boolean
  /** false = 数据还没有，开关置灰 */
  available: boolean
  note: string
  evaluate: (row: SheetRow, meta: ColMeta, ctx?: RuleContext) => Hit | null
}

export const FILL_CLASS: Record<Level, string> = {
  1: 'hl-fill-1',
  2: 'hl-fill-2',
  3: 'hl-fill-3',
}
const LEVEL_LABEL: Record<Level, string> = { 1: '轻', 2: '中', 3: '严重' }

// ───────────────────────── 工具 ─────────────────────────


function isMainAhBook(book: AhRuleBook | undefined): book is AhBook {
  return book === 'pinnacle' || book === 'macau' || book === 'crown' || book === 'william'
}

function ah(row: SheetRow, book: AhRuleBook | undefined, phase: Phase): AhCell | null {
  if (!book) return null
  // 同路分路：macau_5df 只读并列桶，绝不落到手工 macau
  if (book === 'macau_5df') return row.ahMacau5df?.[phase] ?? null
  return row.ah[book]?.[phase] ?? null
}

function moveOf(row: SheetRow, book: AhRuleBook | undefined) {
  return lineMove(ah(row, book, 'open')?.line ?? null, ah(row, book, 'close')?.line ?? null)
}

/** 升降盘规则只作用在「变化」列和「临盘」盘口列 */
function isMoveCell(meta: ColMeta): boolean {
  return meta.kind === 'ahChg' || (meta.kind === 'ahLine' && meta.phase === 'close')
}

function fmt(n: number, d = 2): string {
  return Number(n.toFixed(d)).toString()
}

/**
 * §2 水位异动（hl_v0.3）：只比初→临。可比性只看临盘格后端 water_move_eligible
 * （后端：初/临同盘口且两格都是真实水位 → true；中盘换过盘又回到初盘盘口时照样 true，另给 tier_cross_mid=true）。
 * 0.3.18 的「中→临」分支已删除。
 */
export function waterMovePair(row: SheetRow, book: AhRuleBook): { from: AhCell; to: AhCell; fromPhase: Phase } | null {
  const c = ah(row, book, 'close')
  if (!c || c.waterMoveEligible !== true) return null
  const o = ah(row, book, 'open')
  if (!o) return null
  return { from: o, to: c, fromPhase: 'open' }
}

function moveLevel(from: AhCell, to: AhCell): { level: Level; delta: number } | null {
  if (from.hw == null || from.aw == null || to.hw == null || to.aw == null) return null
  const delta = Math.max(Math.abs(to.hw - from.hw), Math.abs(to.aw - from.aw))
  const eps = 1e-9
  if (delta + eps >= WATER_MOVE.level3) return { level: 3, delta }
  if (delta + eps >= WATER_MOVE.level2) return { level: 2, delta }
  if (delta + eps >= WATER_MOVE.level1) return { level: 1, delta }
  return null
}

/** §2 水位异动：返回级别与 Δ（只在后端判可比时）；不满足返回 null */
export function waterMoveLevel(row: SheetRow, book: AhRuleBook): { level: Level; delta: number; fromPhase: Phase } | null {
  const p = waterMovePair(row, book)
  if (!p) return null
  const r = moveLevel(p.from, p.to)
  return r ? { ...r, fromPhase: p.fromPhase } : null
}

/**
 * 排查开关专用：后端判不可比（eligible≠true）但初/临盘口相同、且两格都有标注来源（真实 / 档位换算，至少一格档位换算）时，
 * 仍按原阈值算初→临 Δ 上排查色。只用于排查，不进导出、不进方案。
 */
function waterMoveDebugLevel(row: SheetRow, book: AhRuleBook): { level: Level; delta: number; fromPhase: Phase } | null {
  const c = ah(row, book, 'close')
  const o = ah(row, book, 'open')
  if (!c || !o || c.waterMoveEligible === true || c.line == null || o.line == null) return null
  if (Math.abs(o.line - c.line) > 1e-9) return null
  const kinds = [o, c].map((x) => rrWaterKind(x.waterSource))
  if (kinds.includes('unknown') || !kinds.includes('tier')) return null
  const r = moveLevel(o, c)
  return r ? { ...r, fromPhase: 'open' } : null
}

/** 水位显示：至少两位小数（1.00 / 0.95 / 0.875） */
function fmtWater(v: number): string {
  return v.toFixed(3).replace(/0$/, '')
}

/** 档位 t → 水位（0.70 + 0.05t） */
export function tierToWater(t: number): number {
  return Math.round((0.7 + 0.05 * t) * 1000) / 1000
}

const SIDE_ZH: Record<'home' | 'away', string> = { home: '主队', away: '客队' }
const phaseShort = (p: string | null | undefined) =>
  p && p in PHASE_LABEL ? PHASE_LABEL[p as Phase].slice(0, 1) : (p ?? '')

/** hl_v0.3 tier_cross_mid 悬停 */
export const TIER_CROSS_MID_TIP = '中盘曾换盘，已回到初盘盘口'

/**
 * 0.3.19 tier_cross（只在临盘格，初→临比）→ 悬停文案：
 *   kind=water_tier（旧数据无 kind 时同此）：「主队跨档：初 0.95→临 1.00（档位换算，幅度不精确）」，只列本列这一侧；
 *   kind=line：「初盘与临盘盘口不同（主让 0.5→主让 0.75），水位不比较」。
 */
export function tierCrossTip(cell: AhCell | null, side: 'home' | 'away'): string | null {
  const tc = cell?.tierCross
  if (!tc) return null
  if (tc.kind === 'line') {
    const f = tc.from?.line
    const t = tc.to?.line
    const fromTo =
      typeof f === 'number' && typeof t === 'number' ? `（${formatHandicap(f)}→${formatHandicap(t)}）` : ''
    return `${phaseShort(tc.from?.phase) || '初'}盘与${phaseShort(tc.to?.phase) || '临'}盘盘口不同${fromTo}，水位不比较`
  }
  if (!Array.isArray(tc.sides) || !tc.sides.includes(side)) return null
  const f = tc.from?.[side]
  const t = tc.to?.[side]
  if (typeof f !== 'number' || typeof t !== 'number') return null
  return `${SIDE_ZH[side]}跨档：${phaseShort(tc.from?.phase)} ${fmtWater(tierToWater(f))}→${phaseShort(tc.to?.phase)} ${fmtWater(tierToWater(t))}（档位换算，幅度不精确）`
}

/** 该公司其它阶段来自旧库导入 / 手工数据（basis=legacy_import） */
function fromManualLegacy(row: SheetRow, book: AhRuleBook): boolean {
  const block = book === 'macau_5df' ? row.ahMacau5df : row.ah[book]
  if (!block) return false
  const cells = [block.open, block.close].filter((c): c is AhCell => c != null)
  return cells.some((c) => {
    const tag = `${c.basis ?? ''}|${c.source ?? ''}`
    return /legacy|manual|手工/.test(tag)
  })
}

/** §3 同一阶段盘口极差 */
export function disagreeSpread(row: SheetRow, phase: Phase): { spread: number; n: number } | null {
  const lines = AH_BOOKS.map((b) => ah(row, b, phase)?.line).filter(
    (v): v is number => v != null,
  )
  if (lines.length < 2) return null
  return { spread: Math.max(...lines) - Math.min(...lines), n: lines.length }
}

export function pinnacleMacauOpposite(row: SheetRow, phase: Phase): boolean {
  const p = ah(row, 'pinnacle', phase)?.line
  const m = ah(row, 'macau', phase)?.line
  if (p == null || m == null) return false
  const sp = givingSide(p)
  const sm = givingSide(m)
  return sp !== 0 && sm !== 0 && sp !== sm
}

type RrFixedFallback = { lowLevel1: number; lowLevel2: number; highLevel1: number | null }

/** 经验基准偏离（hl_v0.1 起不变）：有 return_rate_baseline 时用 */
function rrBaselineLevel(rr: number, baseline: number): { level: Level; reason: string } | null {
  const d = rr - baseline
  if (d <= -RETURN_RATE_BASELINE_DELTA.lowLevel2) return { level: 2, reason: `低于基准 ${fmt(baseline, 3)} 达 ${fmt(-d, 3)}` }
  if (d <= -RETURN_RATE_BASELINE_DELTA.lowLevel1) return { level: 1, reason: `低于基准 ${fmt(baseline, 3)} 达 ${fmt(-d, 3)}` }
  if (d >= RETURN_RATE_BASELINE_DELTA.highLevel1) return { level: 1, reason: `高于基准 ${fmt(baseline, 3)} 达 ${fmt(d, 3)}` }
  return null
}

/** hl_v0.2 固定兜底绝对值：hl_v0.3 起只给「档位换算也上色（仅排查用）」用 */
function rrFixedLevel(rr: number, fb: RrFixedFallback): { level: Level; reason: string } | null {
  if (rr < fb.lowLevel2) return { level: 2, reason: `固定兜底：低于 ${fb.lowLevel2}` }
  if (rr < fb.lowLevel1) return { level: 1, reason: `固定兜底：低于 ${fb.lowLevel1}` }
  if (fb.highLevel1 != null && rr > fb.highLevel1) return { level: 1, reason: `固定兜底：高于 ${fb.highLevel1}` }
  return null
}

export const RR_INSUFFICIENT_TIP = '样本不足，暂不判断'
export const RR_HIGH_TIP = '返还率偏高'

/** 兜底线说明（悬停）：「兜底线 P25 0.912（样本 n=356）」，有 P10 / P90 时一并列出 */
export function fallbackLineTip(fb: FallbackInfo): string {
  const parts = [`P25 ${fmt(fb.p25 as number, 3)}`]
  if (fb.p10 != null) parts.push(`P10 ${fmt(fb.p10, 3)}`)
  if (fb.p90 != null) parts.push(`P90 ${fmt(fb.p90, 3)}`)
  return `兜底线 ${parts.join(' / ')}（样本 n=${fb.n ?? 0}）`
}

/**
 * hl_v0.3 返还率兜底（无经验基准时）：
 *   不满足（eligible≠true 或 n < 100）→ 灰字「样本不足，暂不判断」；
 *   低于 P10（有字段才判）中、低于 P25 轻；不低于 P90（有字段才判）中性色「返还率偏高」；
 *   否则只悬停兜底线。
 */
function rrFallbackHit(name: string, rr: number, fb: FallbackInfo | null): Hit | null {
  if (!fb || !fb.eligible || fb.p25 == null || (fb.n ?? 0) < RR_FALLBACK.minN) {
    const tail = fb && fb.n != null ? `（兜底样本 n=${fb.n}，不足 ${RR_FALLBACK.minN}）` : ''
    return { className: 'hl-rr-insufficient', reason: `${name}：${RR_INSUFFICIENT_TIP}${tail}`, insufficient: true }
  }
  const line = fallbackLineTip(fb)
  // 0.3.20 起后端直接给档（fallback_hl_level）；有就以接口为准，没有再按分位数自己比
  if (fb.level === 'medium') return { className: FILL_CLASS[RR_FALLBACK.belowP10Level], reason: `${name} 中：${fmt(rr, 3)} 低于 P10；${line}` }
  if (fb.level === 'light') return { className: FILL_CLASS[RR_FALLBACK.belowP25Level], reason: `${name} 轻：${fmt(rr, 3)} 低于 P25；${line}` }
  if (fb.level === 'high') return { className: 'hl-rr-high', reason: `${RR_HIGH_TIP}（不低于 P90，中性色，不算风险）；${line}`, neutral: true }
  if (fb.p10 != null && rr < fb.p10)
    return { className: FILL_CLASS[RR_FALLBACK.belowP10Level], reason: `${name} 中：${fmt(rr, 3)} 低于 P10；${line}` }
  if (rr < fb.p25)
    return { className: FILL_CLASS[RR_FALLBACK.belowP25Level], reason: `${name} 轻：${fmt(rr, 3)} 低于 P25；${line}` }
  if (fb.p90 != null && rr >= fb.p90)
    return { className: 'hl-rr-high', reason: `${RR_HIGH_TIP}（不低于 P90，中性色，不算风险）；${line}`, neutral: true }
  return { className: '', reason: `${name}：${line}`, tooltipOnly: true }
}

/** hl_v0.2：返还率格子的水位来源分类（§4.1 / §13：actual | tier_midpoint | null） */
export type RrWaterKind = 'real' | 'tier' | 'unknown'
export function rrWaterKind(ws: string | null | undefined): RrWaterKind {
  if (ws === WATER_SOURCE_REAL) return 'real'
  if (ws === WATER_SOURCE_TIER) return 'tier'
  // 缺字段 / null / 未约定的值：文档没写默认处理 → 当作未标注，不上色
  return 'unknown'
}
/** 返还率格子悬停：水位来源说明（真实水位不写） */
export const RR_WATER_TIP: Record<Exclude<RrWaterKind, 'real'>, string> = {
  tier: '水位为档位中点换算，非真实水位',
  unknown: '水位来源未标注',
}

/**
 * 返还率规则共用：只对真实水位上色。
 * hl_v0.3.1（0.3.20）：优先用后端 fallback_hl_level（light / medium / high）；
 *   high = 中性色「返还率偏高」，不算风险；eligible≠true → 灰字「样本不足」。
 * 没有兜底档时再退回经验基准偏离（旧口径）；档位换算只在排查模式上色。
 */
function rrHit(
  name: string,
  rr: number | null,
  baseline: number | null,
  ws: string | null | undefined,
  fallback: FallbackInfo | null,
  fixed: RrFixedFallback,
  ctx: RuleContext | undefined,
): Hit | null {
  if (rr == null) return null
  const kind = rrWaterKind(ws)
  if (kind === 'unknown') return null
  if (kind === 'tier') {
    if (!ctx?.debugTier) return null
    // 排查：优先按后端兜底档，否则基准 / 固定兜底
    const byFb = rrFallbackHit(name, rr, fallback)
    if (byFb && !byFb.insufficient && !byFb.tooltipOnly) {
      return { ...byFb, className: `${byFb.className} hl-debug-tier`.trim(), reason: `【排查】${byFb.reason}（档位换算水位，排查色不进导出）`, debug: true }
    }
    const r = baseline != null ? rrBaselineLevel(rr, baseline) : rrFixedLevel(rr, fixed)
    if (!r) return null
    return {
      className: `${FILL_CLASS[r.level]} hl-debug-tier`,
      reason: `【排查】${name} ${LEVEL_LABEL[r.level]}：${r.reason}（档位换算水位，排查色不进导出）`,
      debug: true,
    }
  }
  // hl_v0.3.1：有兜底字段就走分位（含 high 中性色 / 样本不足灰字）；与后端 light/medium/high 计数对齐
  if (fallback) {
    const h = rrFallbackHit(name, rr, fallback)
    if (h) return h
  }
  if (baseline != null) {
    const r = rrBaselineLevel(rr, baseline)
    return r ? { className: FILL_CLASS[r.level], reason: `${name} ${LEVEL_LABEL[r.level]}：${r.reason}` } : null
  }
  return null
}

function x1x2(row: SheetRow, book: AhBook | undefined, phase: Phase | undefined): X1x2Cell | null {
  if (!book || !phase || phase === 'mid') return null
  return row.x1x2[book]?.[phase] ?? null
}

/** ISO 或 "YYYY-MM-DD HH:mm:ss" → 毫秒；无时区按北京时间 */
export function parseTime(s: string | null | undefined): number | null {
  if (!s) return null
  const t = /[zZ]|[+-]\d{2}:?\d{2}$/.test(s) ? s : `${s.replace(' ', 'T')}+08:00`
  const ms = Date.parse(t)
  return Number.isFinite(ms) ? ms : null
}

/** 0.3.17 初盘为空（open_basis=null）的悬停文案 */
export const OPEN_NO_DATA_TIP = '该公司无初盘数据'
export const AFTER_AS_OF_TIP = '该时点晚于数据截至时刻，暂不显示'
/**
 * open_basis_reason → 中文。no_open_data：平博复用已有的「暂无平博数据」，其余「该公司无初盘数据」；
 * after_as_of 复用格子级 hidden_reason 的文案；未知值原样显示。
 */
export function openMissingTip(reason: string | null | undefined, book?: AhBook | 'jc'): string | null {
  if (!reason) return null
  if (reason === 'no_open_data') return book === 'pinnacle' ? '暂无平博数据' : OPEN_NO_DATA_TIP
  if (reason === 'after_as_of') return AFTER_AS_OF_TIP
  return `初盘缺失（后端原因：${reason}）`
}

// ───────────────────────── 规则 ─────────────────────────

export const RULES: RuleDef[] = [
  {
    id: 'line_move_font',
    name: '升降盘',
    views: ['snapshot', 'health', 'review'],
    columns: ['ahChg', 'ahLine'],
    condition: `临盘−初盘，按让球方看 |变化| ≥ ${LINE_MOVE.font}：升盘红字 ↑、降盘绿字 ↓（换边按降盘）`,
    style: { kind: 'font', levels: [] },
    defaultOn: true,
    available: true,
    note: '只比同一公司的初盘与临盘；缺任一快照不判',
    evaluate(row, meta) {
      if (!isMoveCell(meta)) return null
      const m = moveOf(row, meta.book)
      if (!m || m.kind === 'none') return null
      if (m.kind === 'switch')
        return { className: 'hl-down', reason: `换边（按降盘）：变化 ${m.amount}` }
      if (m.amount + 1e-9 < LINE_MOVE.font) return null
      return m.kind === 'up'
        ? { className: 'hl-up', reason: `升盘 ${m.amount}` }
        : { className: 'hl-down', reason: `降盘 ${m.amount}` }
    },
  },
  {
    id: 'line_move_big',
    name: '大幅变盘',
    views: ['snapshot', 'review'],
    columns: ['ahChg', 'ahLine'],
    condition: `|变化| ≥ ${LINE_MOVE.fillYellow}：叠黄填充（提醒看，不一定是风险）`,
    style: { kind: 'fill', levels: [1] },
    defaultOn: true,
    available: true,
    note: '与升降盘字色同时出现；换边时只标橙、不叠黄（§8-2）',
    evaluate(row, meta) {
      if (!isMoveCell(meta)) return null
      const m = moveOf(row, meta.book)
      if (!m || m.kind === 'none' || m.kind === 'switch') return null
      if (m.amount + 1e-9 < LINE_MOVE.fillYellow) return null
      return { className: FILL_CLASS[1], reason: `大幅变盘 ${m.amount}` }
    },
  },
  {
    id: 'line_switch',
    name: '换边',
    views: ['snapshot', 'health', 'review'],
    columns: ['ahChg', 'ahLine'],
    condition: '让球方变了（如主让 0.25 → 主受让 0.25）：绿字 + 橙填充；变化 ≥0.5 也只标橙',
    style: { kind: 'fill', levels: [LINE_MOVE.switchFillLevel] },
    defaultOn: true,
    available: true,
    note: '平手不算让球方：平手→有让球方算升盘，有让球方→平手算降盘',
    evaluate(row, meta) {
      if (!isMoveCell(meta)) return null
      const m = moveOf(row, meta.book)
      if (m?.kind !== 'switch') return null
      return { className: FILL_CLASS[LINE_MOVE.switchFillLevel], reason: '换边' }
    },
  },
  {
    id: 'water_move',
    // 0.3.22 预留：升降盘/水位异动/返还率只在同一 book 路径内比（macau↔macau、macau_5df↔macau_5df），禁止跨源；evaluate 已用 meta.book 分路
    name: '水位异动',
    views: ['snapshot', 'health'],
    columns: ['ahWater'],
    condition: `只比初→临，只在后端判「可比」时涂（初/临同盘口、两端都是真实水位）。max(|Δ主水|, |Δ客水|) ≥ ${WATER_MOVE.level1} 轻 / ≥ ${WATER_MOVE.level2} 中 / ≥ ${WATER_MOVE.level3} 严重`,
    style: { kind: 'fill', levels: [1, 2, 3] },
    defaultOn: true,
    available: true,
    note: 'hl_v0.3：可比性只看临盘格后端标记，涂在临盘水位格。档位中点换算的水位（现网皇冠/威廉全部是）不上色：同盘跨档时悬停「主队/客队跨档：X→Y（档位换算，幅度不精确）」，初/临换盘时悬停「初盘与临盘盘口不同，水位不比较」；中盘换过盘又回到初盘盘口的照常上色，悬停「中盘曾换盘，已回到初盘盘口」；v0 阈值待补数库分位校准',
    evaluate(row, meta, ctx) {
      // 亚盘水位只有初/临两列：初→临的跨档 / 换盘写在临盘水位格悬停里
      if (meta.kind !== 'ahWater' || meta.phase !== 'close' || !meta.book) return null
      const side: 'home' | 'away' = meta.side === 'aw' ? 'away' : 'home'
      const close = ah(row, meta.book, 'close')
      const cross =
        [tierCrossTip(close, side), close?.tierCrossMid ? TIER_CROSS_MID_TIP : null].filter(Boolean).join('；') || null
      const r = waterMoveLevel(row, meta.book)
      if (r)
        return {
          className: FILL_CLASS[r.level],
          reason: [`水位异动 ${LEVEL_LABEL[r.level]}：Δ ${fmt(r.delta, 3)}（${PHASE_LABEL[r.fromPhase]}→临盘）`, close?.tierCrossMid ? TIER_CROSS_MID_TIP : null]
            .filter(Boolean)
            .join('；'),
        }
      // 排查开关：档位换算水位也按原阈值上色 + 排查标记，不进导出
      if (ctx?.debugTier) {
        const d = waterMoveDebugLevel(row, meta.book)
        if (d)
          return {
            className: `${FILL_CLASS[d.level]} hl-debug-tier`,
            reason: [cross, `【排查】水位异动 ${LEVEL_LABEL[d.level]}：Δ ${fmt(d.delta, 3)}（${PHASE_LABEL[d.fromPhase]}→临盘，档位换算水位，排查色不进导出）`]
              .filter(Boolean)
              .join('；'),
            debug: true,
          }
      }
      return cross ? { className: '', reason: cross, tooltipOnly: true } : null
    },
  },
  {
    id: 'disagree',
    name: '公司分歧',
    views: ['snapshot', 'health'],
    columns: ['ahLine'],
    condition: `初/中/临每个阶段分别判，各公司盘口 最大−最小：= ${DISAGREE.hintOnly} 只在提示里显示、不标色；≥ ${DISAGREE.level1} 轻 / ≥ ${DISAGREE.level2} 中`,
    style: { kind: 'fill', levels: [1, 2] },
    defaultOn: true,
    available: true,
    note: '至少两家有盘口才判；标在该阶段所有有盘口的公司格子上；hl_v0.1 收紧（原 0.25 轻 / 0.5 中）',
    evaluate(row, meta) {
      if (meta.kind !== 'ahLine' || !meta.phase || !meta.book) return null
      if (ah(row, meta.book, meta.phase)?.line == null) return null
      const s = disagreeSpread(row, meta.phase)
      if (!s) return null
      const label = `${PHASE_LABEL[meta.phase]}分歧 ${fmt(s.spread)}（${s.n} 家）`
      if (s.spread + 1e-9 >= DISAGREE.level2) return { className: FILL_CLASS[2], reason: `${label} 中` }
      if (s.spread + 1e-9 >= DISAGREE.level1) return { className: FILL_CLASS[1], reason: `${label} 轻` }
      if (s.spread + 1e-9 >= DISAGREE.hintOnly) return { className: '', reason: `${label}（不标色）`, tooltipOnly: true }
      return null
    },
  },
  {
    id: 'pin_macau_opposite',
    name: '平博与澳门反向',
    views: ['snapshot', 'health', 'review'],
    columns: ['ahLine'],
    condition: '同一阶段平博与澳门让球方向相反（一家主让、一家客让）：红填充',
    style: { kind: 'fill', levels: [DISAGREE.pinnacleMacauOppositeLevel] },
    defaultOn: true,
    available: true,
    note: '标在平博、澳门两格；平手视为没有方向，不算反向（§8-4）；两库目前都没有平博数据，缺数据不判，暂时不会触发',
    evaluate(row, meta) {
      if (meta.kind !== 'ahLine' || !meta.phase) return null
      if (meta.book !== 'pinnacle' && meta.book !== 'macau') return null
      if (!pinnacleMacauOpposite(row, meta.phase)) return null
      return {
        className: FILL_CLASS[DISAGREE.pinnacleMacauOppositeLevel],
        reason: `${PHASE_LABEL[meta.phase]}平博与澳门让球方向相反`,
      }
    },
  },
  {
    id: 'ah_return_rate',
    name: '亚盘返还率',
    views: ['health'],
    columns: ['ahRr'],
    condition: `只对真实水位的格子上色。按该公司 as-of 基准：低 ${RETURN_RATE_BASELINE_DELTA.lowLevel1} 轻 / 低 ${RETURN_RATE_BASELINE_DELTA.lowLevel2} 中 / 高 ${RETURN_RATE_BASELINE_DELTA.highLevel1} 轻；无基准时按后端兜底线：低于 P25 轻 / 低于 P10 中，不低于 P90 中性色「返还率偏高」；兜底样本不足 ${RR_FALLBACK.minN} 灰字「样本不足」`,
    style: { kind: 'fill', levels: [1, 2] },
    defaultOn: true,
    available: false,
    note: '返还率、基准与兜底分位数都由后端计算（前端只读）。档位中点换算的格子（现网皇冠/威廉全部是）不上色；水位来源未标注的也不上色。「返还率偏高」是中性色，不算风险、不进导出、不计入风险统计',
    evaluate(row, meta, ctx) {
      if (meta.kind !== 'ahRr' || !meta.book || !meta.phase) return null
      // 并列路暂无独立返还率兜底表：同路内用 ah()，兜底阈值仅主四家
      if (!isMainAhBook(meta.book)) return null
      const c = ah(row, meta.book, meta.phase)
      return rrHit('亚盘返还率', c?.returnRate ?? null, c?.returnRateBaseline ?? null, c?.waterSource, c?.fallback ?? null, AH_RETURN_RATE_FALLBACK[meta.book], ctx)
    },
  },
  {
    id: 'x1x2_return_rate',
    name: '欧赔返还率',
    views: ['health'],
    columns: ['x1x2Rr'],
    condition: '只对真实报价的格子上色；同亚盘返还率：有基准按基准偏离，无基准按后端兜底线（P25 轻 / P10 中 / 不低于 P90 中性色），兜底样本不足灰字「样本不足」',
    style: { kind: 'fill', levels: [1, 2] },
    defaultOn: true,
    available: false,
    note: '后端有欧赔返还率时自动生效；现网只有主胜赔率，胜平负三项齐全前算不了。0.3.18 起欧赔格子带水位来源：只有真实报价（actual）上色，档位换算 / 未标注不上色',
    evaluate(row, meta, ctx) {
      if (meta.kind !== 'x1x2Rr' || !meta.book || !isMainAhBook(meta.book)) return null
      const c = x1x2(row, meta.book, meta.phase)
      return rrHit('欧赔返还率', c?.returnRate ?? null, c?.returnRateBaseline ?? null, c?.waterSource, c?.fallback ?? null, X1X2_RETURN_RATE_FALLBACK[meta.book], ctx)
    },
  },
  {
    id: 'kelly',
    name: '凯利指数',
    views: ['health'],
    columns: ['kelly'],
    condition: `凯利 − 该公司当时返还率 ≥ ${KELLY_THRESHOLDS.home.lightOverReturnRate.toFixed(2)} 轻；凯利 ≥ ${KELLY_THRESHOLDS.home.medium.toFixed(2)} 中；不设重档，低凯利不标色。基准概率用平博去水（平博自身用多家平均）`,
    style: { kind: 'fill', levels: [1, 2] },
    defaultOn: true,
    available: false,
    note: '后端有凯利值时自动生效；缺平博欧赔时退回多家平均基准（不含本家），格子加角标；「多家平均（参考）」列含全部机构，仅供参考',
    evaluate(row, meta) {
      if (meta.kind !== 'kelly' || !meta.book || !meta.side || !isMainAhBook(meta.book)) return null
      const c = x1x2(row, meta.book, meta.phase)
      const side = meta.side as 'home' | 'draw' | 'away'
      const k = c?.kelly?.[side]
      if (k == null) return null
      // hl_v0.3：主/平/客各取一套阈值（现在三套相同；hl_v0.4 改读后端 kelly_thr_{h,d,a}）
      const thr = KELLY_THRESHOLDS[side]
      const eps = 1e-9
      if (k + eps >= thr.medium)
        return { className: FILL_CLASS[KELLY.overOneLevel], reason: `凯利 ${fmt(k, 3)} ≥ ${thr.medium.toFixed(2)}` }
      if (c?.returnRate != null && k - c.returnRate + eps >= thr.lightOverReturnRate)
        return {
          className: FILL_CLASS[KELLY.overReturnRateLevel],
          reason: `凯利 ${fmt(k, 3)} − 返还率 ${fmt(c.returnRate, 3)} ≥ ${thr.lightOverReturnRate.toFixed(2)}`,
        }
      return null
    },
  },
  {
    id: 'settle_color',
    name: '结算颜色',
    views: ['snapshot', 'health', 'review'],
    columns: ['settle', 'pnl'],
    condition: '赢 绿 / 赢半 浅绿 / 走 灰 / 输半 浅红 / 输 红 / 不下注、推迟作废 灰斜体（字色，不填充）',
    style: { kind: 'font', levels: [] },
    defaultOn: true,
    available: true,
    note: '未完场一律显示「—」不着色',
    evaluate(row, meta) {
      if (meta.kind !== 'settle' && meta.kind !== 'pnl') return null
      if (!row.settleCode || (!row.finished && row.settleCode !== 'void_postponed')) return null
      return { className: `hl-settle-${row.settleCode}`, reason: '' }
    },
  },
  {
    id: 'after_prediction',
    name: '预测后盘口',
    views: ['snapshot', 'health', 'review'],
    columns: ['ahLine', 'ahWater'],
    condition: '盘口记录时间晚于预测产出时间：只加虚线下划线和提示，不改原来的红绿字色（§8-7）',
    style: { kind: 'font', levels: [] },
    defaultOn: true,
    available: true,
    note: '需要盘口记录时间；旧库导入的盘口没有记录时间，暂时不会触发',
    evaluate(row, meta) {
      if (meta.kind !== 'ahLine' && meta.kind !== 'ahWater') return null
      if (!meta.book || !meta.phase) return null
      const at = parseTime(ah(row, meta.book, meta.phase)?.recordedAt)
      const pt = parseTime(row.producedAt)
      if (at == null || pt == null || at <= pt) return null
      return { className: 'hl-after-pred', reason: '预测后盘口' }
    },
  },
  {
    id: 'approx_data',
    name: '缺数 / 回落 / 近似',
    views: ['snapshot', 'health', 'review'],
    columns: ['ahLine', 'ahWater'],
    condition: '缺快照、澳门 0.95 回落、档位中点水位、非标准盘口：灰色斜体，悬停看说明',
    style: { kind: 'font', levels: [] },
    defaultOn: true,
    available: true,
    note: '缺快照的格子留空不判，其它规则也不会在空格上着色',
    evaluate(row, meta) {
      if (!meta.book || !meta.phase) return null
      const c = ah(row, meta.book, meta.phase)
      if (meta.kind === 'ahLine') {
        if (!c || c.line == null) {
          // 0.3.17：初盘为空时按后端 open_basis_reason 说明（after_as_of 已由格子 hidden_reason 提示，不重复）
          if (isMainAhBook(meta.book)) {
            const or = meta.phase === 'open' ? row.openReason?.ah[meta.book] : null
            if (or) {
              const dup = or === 'after_as_of' && row.ah[meta.book].hidden.open === 'after_as_of'
              return { className: 'hl-approx', reason: dup ? '' : (openMissingTip(or, meta.book) ?? '') }
            }
          }
          if (meta.book === 'pinnacle') return { className: 'hl-approx', reason: '暂无平博数据' }
          if (meta.book === 'macau_5df') return { className: 'hl-approx', reason: '暂无澳门5DF快照' }
          if (meta.phase === 'mid' && fromManualLegacy(row, meta.book))
            return { className: 'hl-approx', reason: '旧手工数据未采中盘' }
          return { className: 'hl-approx', reason: '缺快照' }
        }
        if (c.lineNonStandard) return { className: 'hl-approx', reason: `非标准盘口（原值 ${c.lineRaw}）` }
        return null
      }
      if (meta.kind === 'ahWater') {
        const v = meta.side === 'aw' ? c?.aw : c?.hw
        if (!c || v == null) {
          if (isMainAhBook(meta.book)) {
            const or = meta.phase === 'open' && !c ? row.openReason?.ah[meta.book] : null
            if (or) return { className: 'hl-approx', reason: openMissingTip(or, meta.book) ?? '' }
          }
          if (meta.book === 'pinnacle') return { className: 'hl-approx', reason: '暂无平博数据' }
          // 按格判断：有水不上这条；澳门无水时可回落 0.95（不写死永远无）
          if (meta.book === 'macau') return { className: 'hl-approx', reason: '暂无水位；无水位时结算可回落 0.95' }
          return { className: 'hl-approx', reason: '暂无水位' }
        }
        const src = c.waterSource ?? ''
        if (src.includes('fallback') || src.includes('095'))
          return { className: 'hl-approx', reason: '近似：0.95 回落' }
        if (src === 'tier_midpoint')
          return { className: 'hl-approx', reason: c.waterCensored ? '近似：档位中点（截断档）' : '近似：档位中点水位' }
        if (c.waterCensored) return { className: 'hl-approx', reason: '近似：截断档' }
        return null
      }
      return null
    },
  },
]

// ───────────────────────── 开关与翻译 ─────────────────────────

export interface RuleContext {
  view: ViewId
  enabled: Record<string, boolean>
  /** 按当前数据判断规则是否可用（如返还率/凯利：后端有值才激活）；缺省用 rule.available */
  dataAvailable?: Record<string, boolean>
  /** hl_v0.2「档位换算也上色（仅排查用）」：默认关；排查色不进导出 */
  debugTier?: boolean
}

export function defaultEnabled(): Record<string, boolean> {
  return Object.fromEntries(RULES.map((r) => [r.id, r.defaultOn]))
}

export function ruleAvailable(rule: RuleDef, ctx: Pick<RuleContext, 'dataAvailable'> | undefined): boolean {
  return ctx?.dataAvailable?.[rule.id] ?? rule.available
}

export function isRuleActive(rule: RuleDef, ctx: RuleContext | undefined): boolean {
  if (!ctx) return false
  return ruleAvailable(rule, ctx) && !!ctx.enabled[rule.id] && rule.views.includes(ctx.view)
}

/** 当前生效规则在某格子上的命中（用于着色和悬停说明） */
export function evaluateCell(row: SheetRow | undefined, meta: ColMeta, ctx: RuleContext | undefined): Hit[] {
  if (!row) return []
  const out: Hit[] = []
  for (const r of RULES) {
    if (!r.columns.includes(meta.kind) || !isRuleActive(r, ctx)) continue
    const h = r.evaluate(row, meta, ctx)
    if (h) out.push(h)
  }
  return out
}

/**
 * 导出用取色（EXPORT-NOTE(hl_v0.2)）：去掉排查色（debug）和只提示不着色的命中。
 * 将来 exceljs 带样式导出一律走这里，排查模式打开时也不会把排查色写进文件。
 */
export function exportableHits(row: SheetRow | undefined, meta: ColMeta, ctx: RuleContext | undefined): Hit[] {
  // hl_v0.3：中性色（返还率偏高）和灰字「样本不足」也不进导出
  return evaluateCell(row, meta, ctx).filter((h) => !h.debug && !h.tooltipOnly && !h.neutral && !h.insufficient)
}

/** 翻译成 AG Grid cellClassRules：key=class，value=该 class 是否被任一生效规则命中 */
export function buildCellClassRules(meta: ColMeta) {
  const classes = new Set<string>()
  for (const r of RULES) {
    if (!r.columns.includes(meta.kind)) continue
    if (r.style.kind === 'fill') r.style.levels.forEach((l) => classes.add(FILL_CLASS[l]))
  }
  if (RULES.some((r) => r.id === 'line_move_font' && r.columns.includes(meta.kind))) {
    classes.add('hl-up')
    classes.add('hl-down')
  }
  if (meta.kind === 'settle' || meta.kind === 'pnl') {
    for (const c of ['win', 'win_half', 'push', 'lose_half', 'lose', 'no_bet', 'void_postponed']) classes.add(`hl-settle-${c}`)
  }
  if (meta.kind === 'ahLine' || meta.kind === 'ahWater') {
    classes.add('hl-approx')
    classes.add('hl-after-pred')
  }
  if (meta.kind === 'ahRr' || meta.kind === 'x1x2Rr' || meta.kind === 'ahWater') classes.add('hl-debug-tier')
  if (meta.kind === 'ahRr' || meta.kind === 'x1x2Rr') {
    classes.add('hl-rr-high')
    classes.add('hl-rr-insufficient')
  }
  const rules: Record<string, (p: { data?: SheetRow; context?: RuleContext }) => boolean> = {}
  for (const cls of classes) {
    // className 可能带多个 class（如排查色「hl-fill-1 hl-debug-tier」）
    rules[cls] = (p) => evaluateCell(p.data, meta, p.context).some((h) => h.className.split(' ').includes(cls))
  }
  return rules
}

/**
 * 统计当前行集合里每条规则的命中格子数和命中场次（状态栏/报告用）。
 * hl_v0.3：中性色（返还率偏高）和灰字「样本不足」不算命中，分别计入 neutralCells / insufficientCells（及场次）。
 */
export interface RuleHitCount {
  cells: number
  rows: number
  debugCells: number
  neutralCells: number
  neutralRows: number
  insufficientCells: number
  insufficientRows: number
}
export function countRuleHits(rows: SheetRow[], metas: ColMeta[], ctx: RuleContext) {
  const res: Record<string, RuleHitCount> = {}
  for (const r of RULES) {
    if (!isRuleActive(r, ctx)) continue
    const c: RuleHitCount = { cells: 0, rows: 0, debugCells: 0, neutralCells: 0, neutralRows: 0, insufficientCells: 0, insufficientRows: 0 }
    for (const row of rows) {
      let any = false
      let anyN = false
      let anyI = false
      for (const m of metas) {
        if (!r.columns.includes(m.kind)) continue
        const h = r.evaluate(row, m, ctx)
        if (!h || h.tooltipOnly) continue
        if (h.neutral) {
          c.neutralCells++
          anyN = true
        } else if (h.insufficient) {
          c.insufficientCells++
          anyI = true
        } else {
          c.cells++
          if (h.debug) c.debugCells++
          any = true
        }
      }
      if (any) c.rows++
      if (anyN) c.neutralRows++
      if (anyI) c.insufficientRows++
    }
    res[r.id] = c
  }
  return res
}

export const BOOK_NAMES = BOOK_LABEL
