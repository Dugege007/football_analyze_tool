/**
 * 赛前盘口／水位折线：只读 GET /matches/{match_id}/odds/timeline。
 * 不写回、不落库全时序。横轴主路径用服务端返回的 x 与 axis_ticks。
 * 契约：odds-data/schema/v2_0-prematch-odds-timeline-chart.md
 */

import { apiBase, readDbSource, type DbSource } from './dataSource'

/** 阶段锚点：初盘 / 中盘 / 临盘（来自表格快照，非 timeline ticks） */
export type PhaseAnchorKind = 'open' | 'mid' | 'close'

export interface OddsTimelineAxisTick {
  label: string
  tau_min: number
  x: number
}

export interface OddsTimelineTick {
  recorded_at: string
  tau_min: number
  /** 服务端已算好的横轴坐标：x = −log10(max(τ, 1)) */
  x: number
  line: number | null
  home_water: number | null
  away_water: number | null
  over_water?: number | null
  under_water?: number | null
  home?: number | null
  draw?: number | null
  away?: number | null
}

export interface OddsTimelineDisplay {
  recommended_layout?: string
  charts?: Array<{ id: string; title: string; y_fields: string[] }>
}

/** 成功响应（与后端 0.3.23 契约对齐） */
export interface OddsTimelineResponse {
  match_id: string | null
  match_pk?: number | null
  fixture_id?: number | null
  kickoff_at: string
  kickoff_minute_known?: boolean
  book: string
  book_label?: string
  market: string
  line_convention?: string
  water_convention?: string
  as_of?: string
  source: string
  cached: boolean
  cache_expires_at?: string | null
  calls_used?: number
  pages_fetched?: number
  axis?: {
    x_transform?: string
    x_formula?: string
    tau_unit?: string
  }
  axis_ticks: OddsTimelineAxisTick[]
  ticks: OddsTimelineTick[]
  display?: OddsTimelineDisplay
  notes?: string[]
}

export interface OddsTimelineErrorBody {
  error?: {
    code?: string
    message?: string
    paused_reason?: string
    retry_after_sec?: number
    hint?: string
    stale_available?: boolean
  }
  detail?: string | { code?: string; message?: string }
}

export class OddsTimelineHttpError extends Error {
  readonly status: number
  readonly code: string | null
  readonly retryAfterSec: number | null
  readonly body: OddsTimelineErrorBody | null

  constructor(
    status: number,
    message: string,
    code: string | null = null,
    retryAfterSec: number | null = null,
    body: OddsTimelineErrorBody | null = null,
  ) {
    super(message)
    this.name = 'OddsTimelineHttpError'
    this.status = status
    this.code = code
    this.retryAfterSec = retryAfterSec
    this.body = body
  }
}

/**
 * 机构选择：展示中文名，请求用 5DollarFootballAPI 庄家 slug。
 * 默认平博 pinnacle；列表仍含澳门／皇冠／威廉希尔／平博／Bet365。
 * 澳门请求可用 macauslot（别名 macau，服务端会归一化）。
 */
export const ODDS_TIMELINE_BOOKS = [
  { value: 'macauslot', label: '澳门', aliases: ['macau'] as const },
  { value: 'crown', label: '皇冠', aliases: [] as const },
  { value: 'williamhill', label: '威廉希尔', aliases: ['william'] as const },
  { value: 'pinnacle', label: '平博', aliases: [] as const },
  { value: 'bet365', label: 'Bet365', aliases: [] as const },
] as const

export type OddsTimelineBook = (typeof ODDS_TIMELINE_BOOKS)[number]['value']

export const DEFAULT_ODDS_TIMELINE_BOOK: OddsTimelineBook = 'pinnacle'

/** 对数时间轴系数：x = −coef × log10(max(τ_min, 1))；默认 5，可调范围 1～10 */
export const DEFAULT_LOG_TIME_COEF = 5
export const LOG_TIME_COEF_MIN = 1
export const LOG_TIME_COEF_MAX = 10

/**
 * 折线机构 → 表格快照 ah 键（同路叠锚点）。
 * 澳门折线来自 5Dollar 历史，锚点只用 ah.macau_5df，禁止叠手工 ah.macau。
 */
export function timelineBookToAhKey(book: string): string {
  const b = book.toLowerCase()
  if (b === 'macauslot' || b === 'macau') return 'macau_5df'
  if (b === 'williamhill' || b === 'william') return 'william'
  if (b === 'crown') return 'crown'
  if (b === 'pinnacle') return 'pinnacle'
  if (b === 'bet365') return 'bet365'
  return b
}

/**
 * 横轴坐标：x = −coef × log10(max(τ_min, 1))。
 * 主路径用 ticks[].tau_min／axis_ticks[].tau_min 现算，便于滑动条调节系数；
 * 不要死用服务端预乘过的 x（那相当于 coef=1）。
 */
export function minutesToAxisX(minutesToKickoff: number, coef: number = DEFAULT_LOG_TIME_COEF): number {
  const c = Math.min(LOG_TIME_COEF_MAX, Math.max(LOG_TIME_COEF_MIN, coef))
  return -c * Math.log10(Math.max(minutesToKickoff, 1))
}

export function axisXToMinutes(x: number, coef: number = DEFAULT_LOG_TIME_COEF): number {
  const c = Math.min(LOG_TIME_COEF_MAX, Math.max(LOG_TIME_COEF_MIN, coef))
  if (c === 0) return 1
  return Math.pow(10, -x / c)
}

/** 由开赛时间与采样时刻算横轴 x（用于初／中／临锚点） */
export function axisXFromKickoffAndRecorded(
  kickoffAt: string,
  recordedAt: string,
  coef: number = DEFAULT_LOG_TIME_COEF,
): number | null {
  const kick = Date.parse(kickoffAt)
  const rec = Date.parse(recordedAt)
  if (!Number.isFinite(kick) || !Number.isFinite(rec)) return null
  const tauMin = (kick - rec) / 60_000
  if (!(tauMin > 0)) return null
  return minutesToAxisX(tauMin, coef)
}

/** 由开赛与采样时刻得到距开赛剩余分钟，供锚点随系数重算 */
export function tauMinFromKickoffAndRecorded(
  kickoffAt: string,
  recordedAt: string,
): number | null {
  const kick = Date.parse(kickoffAt)
  const rec = Date.parse(recordedAt)
  if (!Number.isFinite(kick) || !Number.isFinite(rec)) return null
  const tauMin = (kick - rec) / 60_000
  if (!(tauMin > 0)) return null
  return tauMin
}

export interface PhaseAnchorMark {
  phase: PhaseAnchorKind
  label: string
  recorded_at: string
  /** 距开赛剩余分钟；绘图时用 minutesToAxisX(tau_min, coef) 现算横轴 */
  tau_min: number
  line: number | null
  home_water: number | null
  away_water: number | null
  /** 表格 ah 分路键，例如 macau_5df／pinnacle */
  ah_key: string
}

const PHASE_LABEL: Record<PhaseAnchorKind, string> = {
  open: '初盘',
  mid: '中盘',
  close: '临盘',
}

function parseMatchDate(matchId: string): string | null {
  const m = /^(\d{4}-\d{2}-\d{2})\|/.exec(matchId)
  return m ? m[1] : null
}

/**
 * 从 /table/matches 取与当前折线同路的初／中／临快照，叠到图上。
 * 澳门 → ah.macau_5df；皇冠／威廉等 → 对应 ah 键；无则跳过该锚点。
 */
export async function fetchPhaseAnchorsForBook(
  matchId: string,
  book: string,
  kickoffAt: string,
  src: DbSource = readDbSource(),
): Promise<PhaseAnchorMark[]> {
  const ahKey = timelineBookToAhKey(book)
  const date = parseMatchDate(matchId)
  const base = apiBase(src)
  const q = new URLSearchParams()
  if (date) {
    q.set('date', date)
    q.set('date_from', date)
    q.set('date_to', date)
  }
  q.set('limit', '200')
  q.set('scope', 'jingcai')

  let res: Response
  try {
    res = await fetch(`${base}/table/matches?${q}`)
  } catch {
    return []
  }
  if (!res.ok) return []
  const body = (await res.json()) as {
    items?: Array<{
      match_id?: string
      ah?: Record<
        string,
        Partial<
          Record<
            PhaseAnchorKind,
            {
              recorded_at?: string | null
              target_at?: string | null
              line?: number | null
              home_water?: number | null
              away_water?: number | null
              available?: boolean | null
            }
          >
        >
      >
      schedule?: {
        kickoff_at?: string | null
        open_target_time?: string | null
        mid_target_time?: string | null
        close_target_time?: string | null
      }
      match?: { kickoff_at?: string | null }
    }>
  }
  const row = (body.items ?? []).find((it) => it.match_id === matchId)
  if (!row) return []

  const phases = row.ah?.[ahKey]
  if (!phases) return []

  const kick =
    kickoffAt ||
    row.match?.kickoff_at ||
    (row.schedule as { kickoff_at?: string } | undefined)?.kickoff_at ||
    ''

  const out: PhaseAnchorMark[] = []
  for (const phase of ['open', 'mid', 'close'] as PhaseAnchorKind[]) {
    const cell = phases[phase]
    if (!cell) continue
    const recorded =
      (cell.recorded_at && String(cell.recorded_at)) ||
      (cell.target_at && String(cell.target_at)) ||
      null
    if (!recorded) continue
    const tau = kick ? tauMinFromKickoffAndRecorded(kick, recorded) : null
    if (tau == null) continue
    out.push({
      phase,
      label: PHASE_LABEL[phase],
      recorded_at: recorded,
      tau_min: tau,
      line: cell.line ?? null,
      home_water: cell.home_water ?? null,
      away_water: cell.away_water ?? null,
      ah_key: ahKey,
    })
  }
  return out
}

function extractErrorMessage(status: number, body: OddsTimelineErrorBody | null): {
  message: string
  code: string | null
  retryAfterSec: number | null
} {
  const err = body?.error
  const detail = body?.detail
  const code =
    err?.code ??
    (typeof detail === 'object' && detail ? detail.code : null) ??
    null
  const retryAfterSec =
    typeof err?.retry_after_sec === 'number' ? err.retry_after_sec : null

  if (status === 503 && (code === 'live_capture_busy' || !code)) {
    const base =
      err?.message ||
      '今日采集忙，请稍后。实时采集需要额度时，赛前折线暂不拉取变盘历史。'
    // 硬性要求界面出现「今日采集忙，请稍后」
    const message = base.includes('今日采集忙')
      ? base
      : `今日采集忙，请稍后。${base}`
    return { message, code: code ?? 'live_capture_busy', retryAfterSec }
  }

  if (status === 422 || code === 'kickoff_unknown') {
    return {
      message:
        err?.message ||
        (typeof detail === 'string' ? detail : null) ||
        '开赛时间未知，无法计算距开赛剩余时间横轴。请补全开赛分钟后再试。',
      code: code ?? 'kickoff_unknown',
      retryAfterSec,
    }
  }

  if (status === 404) {
    return {
      message:
        err?.message ||
        (typeof detail === 'string' ? detail : null) ||
        '未找到该场比赛或对阵编号尚未映射，无法拉取赛前折线。',
      code: code ?? 'match_not_found',
      retryAfterSec,
    }
  }

  if (status === 400) {
    return {
      message:
        err?.message ||
        (typeof detail === 'string' ? detail : null) ||
        '请求参数不合法（请检查机构或玩法取值）。',
      code: code ?? 'bad_request',
      retryAfterSec,
    }
  }

  if (status === 502 || code === 'upstream_error') {
    return {
      message:
        err?.message ||
        '上游赔率历史接口异常，请稍后重试。',
      code: code ?? 'upstream_error',
      retryAfterSec,
    }
  }

  if (status === 503 && code === 'rate_limit_exhausted') {
    return {
      message:
        err?.message ||
        '上游额度已用尽或被限流，请稍后重试。',
      code,
      retryAfterSec,
    }
  }

  const fallback =
    err?.message ||
    (typeof detail === 'string' ? detail : null) ||
    `拉取赛前折线失败（HTTP ${status}）`
  return { message: fallback, code, retryAfterSec }
}

/**
 * 拉取赛前盘口／水位时序（真接口）。
 * 走现网／副本 dataSource 基址；展开 Collapse 后再调用；切换 book 再请求。
 */
export async function fetchOddsTimeline(
  matchId: string,
  book: OddsTimelineBook | string = DEFAULT_ODDS_TIMELINE_BOOK,
  options?: {
    market?: string
    forceRefresh?: boolean
    src?: DbSource
  },
): Promise<OddsTimelineResponse> {
  const src = options?.src ?? readDbSource()
  const base = apiBase(src)
  const market = options?.market ?? 'asian'
  const q = new URLSearchParams()
  q.set('book', book)
  q.set('market', market)
  if (options?.forceRefresh) q.set('force_refresh', 'true')

  const path = `${base}/matches/${encodeURIComponent(matchId)}/odds/timeline?${q}`
  let res: Response
  try {
    res = await fetch(path)
  } catch {
    throw new OddsTimelineHttpError(
      0,
      '无法连接赛前折线接口，请确认所选数据源实例已启动。',
    )
  }

  let body: OddsTimelineResponse | OddsTimelineErrorBody | null = null
  try {
    body = (await res.json()) as OddsTimelineResponse | OddsTimelineErrorBody
  } catch {
    body = null
  }

  if (!res.ok) {
    const parsed = extractErrorMessage(res.status, body as OddsTimelineErrorBody | null)
    throw new OddsTimelineHttpError(
      res.status,
      parsed.message,
      parsed.code,
      parsed.retryAfterSec,
      body as OddsTimelineErrorBody | null,
    )
  }

  const ok = body as OddsTimelineResponse
  if (!ok || !Array.isArray(ok.ticks) || !Array.isArray(ok.axis_ticks)) {
    throw new OddsTimelineHttpError(
      res.status,
      '赛前折线响应缺少 ticks 或 axis_ticks，无法绘图。',
    )
  }
  return ok
}
