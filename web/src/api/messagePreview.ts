/**
 * 份数推算与旧版亚盘消息行辅助函数（公共文件）。
 * simplifiedStakeFromRationale 与后端 api/app/main.py 的 estimate_stake_from_rationale 口径相同。
 * 「当日赛程」页的消息预览请使用 dailyMessageTemplate.ts 与 pickFormat.ts；本文件不再被任何页面直接引用，
 * 保留是为了与后端份数推算对照，以及供脚本或后续页面复用。
 */
import type { Direction, Odds } from './types'

/** 从澳门结算盘取出主队亚盘口（数字；库内约定是主队让球为正数、主队受让为负数）。 */
export function extractMacauCloseHandicap(odds: Odds | null | undefined): number | null {
  const close = odds?.asian?.macau?.close
  if (close == null) return null
  if (typeof close === 'number') return close
  if (typeof close === 'object' && typeof close.handicap === 'number') return close.handicap
  return null
}

function formatSigned(n: number): string {
  if (Object.is(n, -0) || n === 0) return '0'
  const abs = Math.abs(n)
  const body = Number.isInteger(abs) ? String(abs) : String(abs)
  return n > 0 ? `+${body}` : `-${body}`
}

/** 消息行：`主+0.5  x2`；不写玩法名。不下注无盘口行。 */
export function formatAhMessageLine(
  direction: Direction | null | undefined,
  homeHandicap: number | null | undefined,
  stake: number | null | undefined,
): string | null {
  if (!direction) return null
  if (direction === '不下注') return '不下注'
  if (homeHandicap == null || Number.isNaN(homeHandicap)) {
    const core = direction
    return stake != null && stake > 0 ? `${core}  x${stake}` : core
  }
  const sideLine = direction === '主' ? homeHandicap : -homeHandicap
  const core = `${direction}${formatSigned(sideLine)}`
  if (stake == null || stake <= 0) return core
  return `${core}  x${stake}`
}

/** OOS 不足时的简化份：|s|=5→2，|s|=3→1；解析 rationale 里的 dir_sum / bucket。 */
export function simplifiedStakeFromRationale(rationale: string[] | undefined): number | null {
  if (!rationale?.length) return null
  let abs: number | null = null
  for (const line of rationale) {
    const m = /dir_sum\s*=\s*([+-]?\d+)/i.exec(line) || /bucket=SUM=\+?(-?\d+)/i.exec(line)
    if (m) {
      abs = Math.abs(Number(m[1]))
      break
    }
  }
  if (abs === 5) return 2
  if (abs === 3) return 1
  return null
}

/** 消息标题行：完整竞彩编号（保留星期前缀）+ 时:分 + 赛事 + 主队-客队： */
export function formatMatchHeaderLine(opts: {
  jcNo?: string | null
  kickoff?: string | null
  league?: string | null
  home: string
  away: string
}): string {
  const no = (opts.jcNo ?? '').trim() || '—'
  const time = opts.kickoff
    ? opts.kickoff.slice(11, 16)
    : '—'
  const league = opts.league || '—'
  return `- ${no} ${time} ${league} ${opts.home}-${opts.away}：`
}
