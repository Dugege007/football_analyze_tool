/**
 * 「当日赛程」页五个预测方向列与当日消息共用的唯一格式化入口（用户 2026-10-10 确认的格式）。
 *
 * 库内约定（docs/schema/v1_sqlite.sql 中 odds_asian.handicap 的注释）：亚盘盘口是主队视角，
 * 主队让球为正数，主队受让为负数。例如 2026-06-06 六208 葡萄牙 vs 智利，澳门临盘在库里是 2.0，
 * 表示葡萄牙（主队）让 2 球。
 *
 * 亚盘显示按雷速体育的规则：+ 表示所下注的这一方让球，- 表示所下注的这一方受让。
 * 所以下主队时直接使用库里的数值，下客队时把库里的数值取相反数。
 *
 * 竞彩让球显示按竞彩官方的规则：+ 表示主队受让，- 表示主队让球（主队让一球写作 -1），
 * 库里的竞彩让球线（odds_jc_hhad.goal_line、prediction_legs.line）本身就是竞彩官方写法，原样显示。
 *
 * 欧盘（1x2）与竞彩胜平负（jc_had）的方向写法相同，因此加前缀区分：欧盘「(欧) 胜 x3」，
 * 竞彩「(竞) 平负 x1」（半角括号，括号后一个空格）。亚盘、大小、竞彩让球不加前缀。
 * 表格列和当日消息都调用本文件的 formatPick；表格里方向与「x份数」之间是一个空格，
 * 消息里再改成两个空格并以全角「；」结尾（见 dailyMessageTemplate.ts）。
 *
 * 本文件不引用其他模块，便于脚本 scripts/test-pick-format.mjs 单独编译测试。
 */

export type PickMarket = 'ah' | '1x2' | 'ou' | 'jc_had' | 'jc_hhad'

export interface Pick {
  market: PickMarket | string
  strategy?: string | null
  side?: string | null
  line?: number | null
  line_text?: string | null
  stake?: number | null
  /** true 表示冻结预测没有记录份数，份数是按预测页口径推算出来的 */
  stake_estimated?: boolean
}

/** 赛程页五列的列名与市场代码，按显示顺序排列。 */
export const PICK_COLUMNS: { market: PickMarket; title: string }[] = [
  { market: 'ah', title: '亚盘' },
  { market: '1x2', title: '欧盘' },
  { market: 'ou', title: '大小' },
  { market: 'jc_had', title: '竞彩' },
  { market: 'jc_hhad', title: '竞彩让球' },
]

export const NO_BET = '-'

function num(n: number): string {
  return Object.is(n, -0) ? '0' : String(n)
}

function signed(n: number): string {
  if (n === 0 || Object.is(n, -0)) return '0'
  return n > 0 ? `+${num(n)}` : `-${num(-n)}`
}

/** 把胜平负的选项整理成「胜」「平」「负」的固定顺序，可以是双选，例如「平负」。 */
function wdl(side: string): string {
  const s = side.replace(/让/g, '')
  const parts = ['胜', '平', '负'].filter((k) => s.includes(k))
  return parts.join('')
}

function stakeSuffix(stake: number | null | undefined): string {
  return stake != null && stake > 0 ? ` x${stake}` : ''
}

/** 单个预测方向的显示字符串；不下注或没有方向时返回「-」。 */
export function formatPick(p: Pick | null | undefined): string {
  if (!p || !p.side || p.side === '不下注' || p.side === '-') return NO_BET
  const side = p.side.trim()
  const line =
    p.line != null && !Number.isNaN(p.line)
      ? p.line
      : p.line_text != null && p.line_text !== '' && !Number.isNaN(Number(p.line_text))
        ? Number(p.line_text)
        : null
  const x = stakeSuffix(p.stake)
  switch (p.market) {
    case 'ah': {
      if (side !== '主' && side !== '客') return NO_BET
      if (line == null) return `${side}${x}`
      const sideLine = side === '主' ? line : -line
      return `${side}${signed(sideLine)}${x}`
    }
    case 'ou': {
      const ou = side.includes('大') ? '大' : side.includes('小') ? '小' : null
      if (!ou) return NO_BET
      return `${line != null ? num(line) : ''}${ou}${x}`
    }
    case '1x2': {
      const v = wdl(side)
      // 欧盘与竞彩胜平负写法相同，加前缀区分：半角括号，括号后一个空格
      return v ? `(欧) ${v}${x}` : NO_BET
    }
    case 'jc_had': {
      const v = wdl(side)
      return v ? `(竞) ${v}${x}` : NO_BET
    }
    case 'jc_hhad': {
      const v = wdl(side)
      if (!v) return NO_BET
      return `${line != null ? signed(line) : ''}让${v}${x}`
    }
    default:
      return NO_BET
  }
}

/** 一场比赛某个市场的显示字符串；同一市场有多个正式方案下注时用「 / 」连接。 */
export function formatMarketCell(picks: Pick[] | null | undefined, market: PickMarket): string {
  const out = (picks ?? [])
    .filter((p) => p.market === market)
    .map(formatPick)
    .filter((s) => s !== NO_BET)
  return out.length ? out.join(' / ') : NO_BET
}

/** 这一场里所有下注方向的显示字符串，按五列的顺序排列。 */
export function betStrings(picks: Pick[] | null | undefined): string[] {
  return PICK_COLUMNS.map((c) => formatMarketCell(picks, c.market)).filter((s) => s !== NO_BET)
}

/** 这一场是否有推算出来的份数（冻结预测没有记录份数）。 */
export function hasEstimatedStake(picks: Pick[] | null | undefined): boolean {
  return (picks ?? []).some((p) => p.stake_estimated && formatPick(p) !== NO_BET)
}
