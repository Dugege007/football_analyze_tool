import {
  NOT_EVALUABLE_REASON_HINT,
  NOT_EVALUABLE_REASON_ORDER,
  labelNotEvaluableReason,
  labelNotEvaluableSubreason,
} from '../labels'

/**
 * validate / compare 分来源（by_odds_source）与初盘口径（by_open_basis）展示辅助。
 * 后端口径：v2_0-shadow-predictions-n5pin.md §8.1「来源分账」「初盘口径分账」。
 * 顶层数字是全部来源合计，仅作总览；L1（命中满 80）只看 by_odds_source.live，hits 不跨来源合并。
 */

export const ODDS_SOURCES = ['live', 'hist', 'unknown'] as const
export type OddsSource = (typeof ODDS_SOURCES)[number]

export const ODDS_SOURCE_LABEL: Record<OddsSource, string> = {
  live: '实时（挂载后）',
  hist: '历史回补',
  unknown: '未区分来源',
}

export const ODDS_SOURCE_TIP: Record<OddsSource, string> = {
  live: '挂载后实时采到的盘口；L1（命中满 80 场）只看这一行',
  hist: '历史回补的盘口，仅作参考；命中数不与实时合并',
  unknown: '预测快照没写来源（现有方案都在这里）',
}

export const ALL_SOURCES_LABEL = '全部来源'
export const ALL_SOURCES_TIP =
  '各来源合计，仅作总览。实时与历史回补合在一起不能作为判断依据；样本是否够、回报率怎么看，以下方分来源各行为准'
export const SPLIT_BACKFILLED_TIP = '旧缓存验证记录：当时未区分来源，全部计入「未区分来源」'

export const OPEN_BASIS_KEYS = ['first_tick', 'api_opening', 'unknown'] as const
export const OPEN_BASIS_LABEL: Record<string, string> = {
  first_tick: '首笔带时间报价',
  api_opening: '接口开盘价（时间未知）',
  unknown: '未标注',
}

/** 0.3.18：validate 按临盘口径分账（close_basis_split=true 时带 by_close_basis）；api_closing 组带 ledger_note */
export const CLOSE_BASIS_KEYS = ['rule_tick', 'api_closing', 'unknown'] as const
export const CLOSE_BASIS_LABEL: Record<string, string> = {
  rule_tick: '规则时刻报价',
  api_closing: '接口收盘价（时间未知）',
  unknown: '未标注',
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** 后端是否给了分来源（odds_source_split=true 或带 by_odds_source） */
export function hasOddsSourceSplit(summary: Record<string, unknown> | null | undefined): boolean {
  if (!summary) return false
  return summary.odds_source_split === true || isObj(summary.by_odds_source)
}

export function sourceSummary(
  summary: Record<string, unknown> | null | undefined,
  src: OddsSource,
): Record<string, unknown> | null {
  const by = summary?.by_odds_source
  if (!isObj(by)) return null
  const v = by[src]
  return isObj(v) ? v : null
}

export function openBasisSplit(
  summary: Record<string, unknown> | null | undefined,
): Record<string, Record<string, unknown>> | null {
  return basisSplit(summary, 'by_open_basis')
}

export function closeBasisSplit(
  summary: Record<string, unknown> | null | undefined,
): Record<string, Record<string, unknown>> | null {
  return basisSplit(summary, 'by_close_basis')
}

function basisSplit(
  summary: Record<string, unknown> | null | undefined,
  field: 'by_open_basis' | 'by_close_basis',
): Record<string, Record<string, unknown>> | null {
  const by = summary?.[field]
  if (!isObj(by)) return null
  const out: Record<string, Record<string, unknown>> = {}
  for (const [k, v] of Object.entries(by)) if (isObj(v)) out[k] = v
  return out
}

/** 不可评估原因：每行「缺平博数据 2（报价过旧 1、无报价 1）」；四个主原因恒列，其它原因附后 */
export function notEvaluableLines(summary: Record<string, unknown> | null | undefined): string[] | null {
  const by = summary?.n_not_evaluable_by_reason
  if (!isObj(by)) return null
  const sub = isObj(summary?.n_not_evaluable_by_subreason)
    ? (summary!.n_not_evaluable_by_subreason as Record<string, unknown>)
    : {}
  const extra = Object.keys(by).filter(
    (k) => !NOT_EVALUABLE_REASON_ORDER.includes(k) && typeof by[k] === 'number' && (by[k] as number) > 0,
  )
  const keys = [...NOT_EVALUABLE_REASON_ORDER, ...extra]
  return keys.map((k) => {
    const n = typeof by[k] === 'number' ? (by[k] as number) : 0
    const subs = isObj(sub[k]) ? Object.entries(sub[k] as Record<string, unknown>) : []
    const subText = subs
      .filter(([, c]) => typeof c === 'number' && c > 0)
      .map(([s, c]) => `${labelNotEvaluableSubreason(s)} ${c}`)
      .join('、')
    const note = subText || (n > 0 ? (NOT_EVALUABLE_REASON_HINT[k] ?? '') : '')
    return `${labelNotEvaluableReason(k)} ${n}${note ? `（${note}）` : ''}`
  })
}
