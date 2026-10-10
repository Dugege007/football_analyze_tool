export type Direction = '主' | '客' | '不下注'

export interface JcInfo {
  id: string
  weekday?: string
  no: string
}

export interface Match {
  date: string
  weekday?: string
  scope: 'jingcai' | 'extra'
  jc: JcInfo | null
  competition?: { name?: string; type?: string; stage?: string }
  kickoff_hour?: number
  kickoff_at?: string | null
  teams: { home: string; away: string }
  ids?: Record<string, unknown>
}

export interface Result {
  home_goals: number
  away_goals: number
  total_goals: number
  wdl: '胜' | '平' | '负'
}

export interface AsianLineFull {
  home_water?: number | null
  handicap?: number | null
  away_water?: number | null
}

export interface AsianPhases {
  open?: number | AsianLineFull | null
  mid?: AsianLineFull | null
  close?: number | AsianLineFull | null
}

export interface JcHhadLine {
  goal_line?: string | number | null
  decision_line?: number | null
  current_line?: number | null
  post_decision_line_change?: boolean | null
  from_hist?: boolean | null
  home?: number | null
  draw?: number | null
  away?: number | null
  jc_1x2_incomplete?: boolean | null
  missing_reason?: string | null
}

export interface Odds {
  asian?: {
    macau?: AsianPhases
    crown?: AsianPhases
    william?: AsianPhases
    bet365?: AsianPhases
    [book: string]: AsianPhases | undefined
  }
  euro_home_win?: Record<string, { open?: number | null; close?: number | null }>
  jc_home_win?: { open?: number | null; close?: number | null }
  jc_hhad?: {
    open?: JcHhadLine | null
    mid?: JcHhadLine | null
    close?: JcHhadLine | null
  }
  _snap?: {
    open_at?: string | null
    mid_at?: string | null
    close_at?: string | null
  }
}

export interface StatsMetaEntry {
  source?: string | null
  as_of?: string | null
}

export interface StatsObsEntry {
  as_of?: string | null
  source?: string | null
  payload?: unknown
  quality?: string | null
  fetched_at?: string | null
  /** 仅源明确无伤停时 true；未采到不得写 true */
  known_empty?: boolean | null
}

export interface Stats {
  recent?: Record<string, unknown>
  h2h?: Record<string, unknown>
  rank?: { home?: number | null; away?: number | null }
  popularity_diff?: number | null
  support_proxy_odds?: number | null
  /** 未采到 = null；禁止把空当成 []+known_empty */
  injury?: unknown
  weather?: unknown
  /** 0.3.21：各 kind 的 source / as_of（如 manual_seed） */
  meta?: Partial<Record<'recent' | 'h2h' | 'rank' | 'injury' | 'weather' | 'popularity', StatsMetaEntry | null>> | null
  /** 0.3.21：stats_obs 最新行 */
  obs?: Partial<Record<string, StatsObsEntry | null>> | null
  [key: string]: unknown
}

export interface Meta {
  source_file?: string
  month?: string
  schema_version?: string
  pipeline?: string
  note?: string
}

export interface Prediction {
  direction: Direction
  strategy: string
  settle_book: string
  rationale: string[]
  confidence?: number | null
  stake?: number | null
  stake_rule?: string | null
  produced_at?: string | null
  updated_at?: string | null
  message_sent_at?: string | null
  /** 0.3.19：旧冻结 S2 / N4 条目「触发依据是换算水位」（后端读出时加，不改库） */
  ledger_note?: string | null
  ledger_note_reason?: string | null
  /** 预留：后端标疑似泄漏时有值 */
  leak_suspect?: unknown
}

export interface MatchListItem {
  id: string
  match: Match
  has_prediction?: boolean
  direction?: Direction | null
  /** 列表赛果；后端补齐前可能缺，前端可回落详情 */
  result?: Result | null
}

export interface MatchListResponse {
  date: string
  scope: string
  items: MatchListItem[]
}

export interface MatchDetail {
  match: Match
  result: Result | null
  stats: Stats
  odds: Odds
  meta: Meta
}
