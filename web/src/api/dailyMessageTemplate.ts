/**
 * 「当日消息」模板（赛程页的「当日消息预览」使用，只在页面上显示和复制，不会自动外发）。
 *
 * 格式由用户在 2026-10-10 确认，见 docs/schema/schedule-page-prediction-direction-columns.md「当日消息」一节。
 * 旧草案（本机研究目录 odds-data/schema/daily-prediction-dm-schedule.md 第 5 节）已作废。
 *
 * 示例（用户确认）：
 * - 六001 03:00 英超 曼城-阿森纳：
 *   主+0.5  x2；
 * - 六002 03:30 西甲 皇马-巴萨：
 *   客-1.25  x1；
 *   2.25大  x3；
 */
import { PICK_COLUMNS, formatPick, NO_BET, type Pick } from './pickFormat'

export interface MessageMatch {
  jcId?: string | null
  league?: string | null
  home: string
  away: string
  /**
   * 开赛时间文本，用来取出北京时间的「时:分」。
   * 可以是「2026-06-06 16:00」这类完整文本，也可以只是「16:00」。
   */
  kickoffText: string
  /** 开赛时刻（毫秒时间戳）；未知时为 null，按「未开赛」处理 */
  kickoffMs: number | null
  picks?: Pick[] | null
}

/** 竞彩编号原样使用（保留星期前缀），例如「六204」「六001」；没有编号时返回「-」。 */
export function messageJcNo(jcId?: string | null): string {
  const id = (jcId ?? '').trim()
  return id || '-'
}

/** 从开赛时间文本里取出「时:分」；取不到时返回「—」。 */
export function messageKickoffHm(kickoffText: string): string {
  const m = /(\d{1,2}:\d{2})/.exec(kickoffText || '')
  if (!m) return '—'
  const [h, min] = m[1].split(':')
  return `${h.padStart(2, '0')}:${min}`
}

/**
 * 消息里的单个方向写法：符号与 formatPick 相同（亚盘「主+0.5」、大小「2.25大」），
 * 方向与「x份数」之间是两个空格；推算份数的星号「*」不进入消息。
 */
export function formatMessageDirection(p: Pick | null | undefined): string | null {
  if (!p) return null
  const text = formatPick(p)
  if (text === NO_BET) return null
  return text.replace(/ x(\d+)$/, '  x$1')
}

/** 一场比赛所有下注方向，按五列顺序，每个方向单独一项（不合并）。 */
export function messageDirections(picks: Pick[] | null | undefined): string[] {
  const out: string[] = []
  for (const col of PICK_COLUMNS) {
    for (const p of (picks ?? []).filter((x) => x.market === col.market)) {
      const s = formatMessageDirection(p)
      if (s) out.push(s)
    }
  }
  return out
}

/** 一场比赛的消息块（标题行 + 缩进的方向行）；不下注时返回 null。 */
export function messageBlock(m: MessageMatch): string | null {
  const dirs = messageDirections(m.picks)
  if (!dirs.length) return null
  const header = `- ${messageJcNo(m.jcId)} ${messageKickoffHm(m.kickoffText)} ${m.league || '-'} ${m.home}-${m.away}：`
  const body = dirs.map((d) => `  ${d}；`).join('\n')
  return `${header}\n${body}`
}

/** 当天有下注的场次数（用于预览标题显示）。 */
export function countBetMatches(matches: MessageMatch[]): number {
  return matches.filter((m) => messageDirections(m.picks).length > 0).length
}

/**
 * 生成当日消息全文。onlyNotStarted=true 时只保留按北京时间当前时刻尚未开赛的场次。
 * 没有可列的场次时返回 null。不带标题行。
 */
export function buildDailyMessage(
  _jingcaiDate: string,
  matches: MessageMatch[],
  opts: { onlyNotStarted?: boolean; nowMs?: number } = {},
): string | null {
  const now = opts.nowMs ?? Date.now()
  const blocks = matches
    .filter((m) => !opts.onlyNotStarted || m.kickoffMs == null || m.kickoffMs > now)
    .map(messageBlock)
    .filter((s): s is string => s != null)
  if (!blocks.length) return null
  return blocks.join('\n')
}
