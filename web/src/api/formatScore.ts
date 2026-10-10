import type { Result } from './types'

/** 赛果展示：`2-1`；无则 — */
export function formatScore(result: Result | null | undefined): string {
  if (!result) return '—'
  if (typeof result.home_goals !== 'number' || typeof result.away_goals !== 'number') {
    return '—'
  }
  return `${result.home_goals}-${result.away_goals}`
}
