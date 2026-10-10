/**
 * 「当日消息」模板（赛程页的「当日消息预览」使用，只在页面上显示和复制，不会自动外发）。
 *
 * 来源：仓库里没有定稿的日用消息模板（docs/schema/data-conventions-v1.md 第 4.3 节写明逐行模板待分析师定稿，
 * 本机研究目录（不在仓库里）的 odds-data/schema/daily-prediction-dm-schedule.md 第 5 节只有草案）。因此按用户 2026-10-10 的要求，
 * 沿用草案的标题行和「每场一行」的写法，每行写成：
 *   - 编号 赛事 主队 vs 客队 开赛时间：各下注方向
 * 下注方向只列下注的方向，用 pickFormat.ts 的统一格式，多个方向之间用「；」分隔；不下注的场次不列。
 */
import { betStrings, type Pick } from './pickFormat'

export interface MessageMatch {
  jcId?: string | null
  league?: string | null
  home: string
  away: string
  /** 北京时间「年-月-日 时:分」 */
  kickoffText: string
  /** 开赛时刻（毫秒时间戳）；未知时为 null，按「未开赛」处理 */
  kickoffMs: number | null
  picks?: Pick[] | null
}

export function messageTitle(jingcaiDate: string): string {
  return `【当日预测】竞彩日 ${jingcaiDate}`
}

export function messageLine(m: MessageMatch): string | null {
  const bets = betStrings(m.picks)
  if (!bets.length) return null
  return `- ${m.jcId || '-'} ${m.league || '-'} ${m.home} vs ${m.away} ${m.kickoffText}：${bets.join('；')}`
}

/**
 * 生成当日消息全文。onlyNotStarted=true 时只保留按北京时间当前时刻尚未开赛的场次。
 * 没有可列的场次时返回 null。
 */
export function buildDailyMessage(
  jingcaiDate: string,
  matches: MessageMatch[],
  opts: { onlyNotStarted?: boolean; nowMs?: number } = {},
): string | null {
  const now = opts.nowMs ?? Date.now()
  const lines = matches
    .filter((m) => !opts.onlyNotStarted || m.kickoffMs == null || m.kickoffMs > now)
    .map(messageLine)
    .filter((s): s is string => s != null)
  if (!lines.length) return null
  return [messageTitle(jingcaiDate), ...lines].join('\n')
}
