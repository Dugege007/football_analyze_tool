/**
 * 亚盘盘口解析与显示。
 *
 * 仓库口径：主队视角，**主让为正、主受让为负**（见 odds-data/schema/v2_0-shadow-predictions-n1.md
 * `home_gives_positive`；backtest.py `margin = (主进-客进) - line`）。
 *
 * 现网 /matches/{id}/odds 实测（2026-06 全 177 场）：澳门是纯数字、皇冠/威廉是 {handicap: 数字}，
 * 出现过 1 次非 0.25 整倍数（1.7）和 3 次 null。这里也兼容常见文字写法，便于以后接其它源：
 *   数字 / 数字字符串（"0.5"、"-0.25"、"+1"）、拆分写法（"0/0.5"、"-0.5/1"）、
 *   中文（平手、平/半、半球、半/一、一球、球半、两球半、受让半球、受一球/球半、*半球 …）。
 */

const CN_BASE: Record<string, number> = {
  平手: 0,
  平: 0,
  半球: 0.5,
  半: 0.5,
  一球: 1,
  一: 1,
  球半: 1.5,
  一球半: 1.5,
  两球: 2,
  二球: 2,
  两: 2,
  两球半: 2.5,
  二球半: 2.5,
  三球: 3,
  三: 3,
  三球半: 3.5,
  四球: 4,
  四: 4,
  四球半: 4.5,
  五球: 5,
}

/** 显示用名称：0.25 步长 → 中文盘口名 */
const NAME_BY_QUARTERS: string[] = [
  '平手',
  '平手/半球',
  '半球',
  '半球/一球',
  '一球',
  '一球/球半',
  '球半',
  '球半/两球',
  '两球',
  '两球/两球半',
  '两球半',
  '两球半/三球',
  '三球',
  '三球/三球半',
  '三球半',
  '三球半/四球',
  '四球',
  '四球/四球半',
  '四球半',
  '四球半/五球',
  '五球',
]

export interface ParsedLine {
  value: number | null
  raw: string | null
  /** 不是 0.25 整倍数（或无法识别） */
  nonStandard: boolean
}

function parseAbsCn(s: string): number | null {
  const t = s.trim()
  if (t in CN_BASE) return CN_BASE[t]
  // 拆分写法：平/半、半球/一球、一/球半
  if (t.includes('/')) {
    const [a, b] = t.split('/')
    const va = parseAbsCn(a)
    const vb = parseAbsCn(b)
    if (va != null && vb != null) return (va + vb) / 2
  }
  return null
}

function parseAbsNum(s: string): number | null {
  const t = s.trim()
  if (/^[+-]?\d+(\.\d+)?$/.test(t)) return Number(t)
  // 0/0.5、0.5/1、-0.5/1（负号作用于整体）
  const m = /^([+-]?)(\d+(?:\.\d+)?)\/(\d+(?:\.\d+)?)$/.exec(t)
  if (m) {
    const v = (Number(m[2]) + Number(m[3])) / 2
    return m[1] === '-' ? -v : v
  }
  return null
}

function isQuarter(v: number): boolean {
  return Math.abs(v * 4 - Math.round(v * 4)) < 1e-9
}

/** 解析任意盘口写法 → 主让为正的数值 */
export function parseHandicap(input: unknown): ParsedLine {
  if (input == null || input === '') return { value: null, raw: null, nonStandard: false }
  if (typeof input === 'number') {
    if (!Number.isFinite(input)) return { value: null, raw: String(input), nonStandard: true }
    return { value: input, raw: String(input), nonStandard: !isQuarter(input) }
  }
  if (typeof input === 'object' && input !== null && 'handicap' in input) {
    return parseHandicap((input as { handicap: unknown }).handicap)
  }
  const raw = String(input).trim()
  let s = raw.replace(/\s+/g, '').replace(/[／]/g, '/').replace(/[−–—]/g, '-')
  // 「受让」「受」「*」前缀 → 主受让（负）
  let sign = 1
  const recv = /^(主?受让|受|\*)/.exec(s)
  if (recv) {
    sign = -1
    s = s.slice(recv[0].length)
  } else if (/^主?让/.test(s)) {
    s = s.replace(/^主?让/, '')
  }
  const num = parseAbsNum(s)
  let v: number | null = num != null ? num * sign : null
  if (v == null) {
    const cn = parseAbsCn(s)
    v = cn != null ? cn * sign : null
  }
  if (v == null) return { value: null, raw, nonStandard: true }
  if (Object.is(v, -0)) v = 0
  return { value: v, raw, nonStandard: !isQuarter(v) }
}

/**
 * 表格主显示：雷速式数字（主队视角，主让为正、主受让为负，0.25 步进）。
 * 例：平手 `0`、主让半球 `+0.5`、主受让半球 `-0.5`、平/半 `+0.25`、半/一 `+0.75`。
 * 正数带 `+`；内部解析仍是 home_gives_positive，这里不翻号。
 */
export function formatHandicap(v: number | null | undefined): string {
  return formatHandicapLine(v)
}

/** 与 formatHandicap 相同；集中出口，便于以后对齐雷速完整规范 */
export function formatHandicapLine(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (Object.is(v, -0) || Math.abs(v) < 1e-9) return '0'
  // 去掉多余尾零：1 → 1，0.50 → 0.5，1.75 → 1.75
  const abs = Math.abs(v)
  const body = Number(abs.toFixed(2)).toString()
  return v > 0 ? `+${body}` : `-${body}`
}

/** 旧汉字盘口（悬停对照用）；主格请用 formatHandicap / formatHandicapLine */
export function formatHandicapCn(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const abs = Math.abs(v)
  const q = Math.round(abs * 4)
  if (!isQuarter(abs) || q >= NAME_BY_QUARTERS.length) {
    return v < 0 ? `受${abs}` : String(abs)
  }
  const name = NAME_BY_QUARTERS[q]
  if (q === 0) return name
  return v < 0 ? `受${name}` : name
}

/** 让球方：1=主让，-1=客让（主受让），0=平手 */
export function givingSide(v: number): 1 | -1 | 0 {
  if (Math.abs(v) < 1e-9) return 0
  return v > 0 ? 1 : -1
}

export type LineMove =
  | { kind: 'none' }
  | { kind: 'up'; amount: number }
  | { kind: 'down'; amount: number }
  /** 换边：让球方变了（按降盘处理 + 橙填充） */
  | { kind: 'switch'; amount: number }

/**
 * 升降盘（hl_v0.1 §1）：按「让球方」看，让得更多=升，让得更少=降。
 * - 主让/客让互换 → 换边（amount = |临−初|）。
 * - 平手 → 有一方让：视为升；有一方让 → 平手：视为降。（§8-1 已确认）
 */
export function lineMove(open: number | null, close: number | null): LineMove | null {
  if (open == null || close == null) return null
  const so = givingSide(open)
  const sc = givingSide(close)
  if (so !== 0 && sc !== 0 && so !== sc) return { kind: 'switch', amount: Math.abs(close - open) }
  const d = Math.abs(close) - Math.abs(open)
  if (Math.abs(d) < 1e-9) return { kind: 'none' }
  return d > 0 ? { kind: 'up', amount: d } : { kind: 'down', amount: -d }
}

export function formatMove(m: LineMove | null): string {
  if (!m) return '—'
  switch (m.kind) {
    case 'none':
      return '不变'
    case 'up':
      return `↑${m.amount}`
    case 'down':
      return `↓${m.amount}`
    case 'switch':
      return `↓换边 ${m.amount}`
  }
}
