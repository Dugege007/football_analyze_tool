import type {
  MatchDetail,
  MatchListResponse,
  Odds,
  Prediction,
} from './types'
import { MOCK_DETAILS, mockListItems } from '../mock/data'

const USE_MOCK_FALLBACK = import.meta.env.VITE_USE_MOCK !== '0'

export type DataSource = 'api' | 'mock'

export interface WithSource<T> {
  data: T
  source: DataSource
}

async function tryFetch<T>(path: string, base = '/api'): Promise<T | null> {
  try {
    const res = await fetch(`${base}${path}`)
    if (!res.ok) return null
    return (await res.json()) as T
  } catch {
    return null
  }
}

export async function listMatches(
  date: string,
  scope: string = 'jingcai',
): Promise<WithSource<MatchListResponse>> {
  const live = await tryFetch<MatchListResponse>(
    `/matches?date=${encodeURIComponent(date)}&scope=${encodeURIComponent(scope)}`,
  )
  if (live) return { data: live, source: 'api' }
  if (!USE_MOCK_FALLBACK) throw new Error('接口不可用且未启用假数据')
  return {
    data: { date, scope, items: mockListItems(date, scope) },
    source: 'mock',
  }
}

export async function getMatch(id: string, base = '/api'): Promise<WithSource<MatchDetail>> {
  const live = await tryFetch<MatchDetail>(`/matches/${encodeURIComponent(id)}`, base)
  if (live) return { data: live, source: 'api' }
  const mock = MOCK_DETAILS[id]
  if (mock) {
    const { prediction: _p, ...detail } = mock
    return { data: detail, source: 'mock' }
  }
  throw new Error('比赛不存在')
}

export async function getMatchOdds(id: string, base = '/api'): Promise<WithSource<Odds>> {
  const live = await tryFetch<Odds>(`/matches/${encodeURIComponent(id)}/odds`, base)
  if (live) return { data: live, source: 'api' }
  const mock = MOCK_DETAILS[id]
  if (mock) return { data: mock.odds, source: 'mock' }
  throw new Error('盘口不存在')
}

export async function getMatchPrediction(
  id: string,
  strategy?: string,
  base = '/api',
): Promise<WithSource<Prediction>> {
  const q = strategy ? `?strategy=${encodeURIComponent(strategy)}` : ''
  const live = await tryFetch<Prediction>(
    `/matches/${encodeURIComponent(id)}/prediction${q}`,
    base,
  )
  if (live) return { data: live, source: 'api' }
  const mock = MOCK_DETAILS[id]?.prediction
  if (mock) return { data: mock, source: 'mock' }
  throw new Error('暂无预测')
}

export interface BankrollConfigResponse {
  items: Record<string, { value: Record<string, unknown>; updated_at?: string; note?: string }>
  amount_todo?: string[]
  latest_snapshot?: { balance?: number } | null
}

export interface CalcItemInput {
  match_id?: string
  strategy?: string
  stake_units?: number
  label?: string
}

export interface CalcItemResult {
  index: number
  label?: string | null
  match_id?: string | null
  strategy?: string | null
  jingcai_date?: string | null
  stake_units_requested?: number | null
  stake_units?: number | null
  amount?: number | null
  capped?: boolean
  raised?: boolean
  capped_amount?: boolean
  error?: string | null
  cap_reasons?: string[]
  amount_limit_reasons?: string[]
  remaining_before?: number | null
}

export interface CalcResponse {
  base_bankroll: number
  base_source: string
  unit_fraction: number
  unit_amount: number
  caps: { per_match: number; per_day: number; mode: string }
  amount_limits: {
    min_stake_amount: number
    max_stake_pct_of_remaining: number
    currency: string
  }
  remaining_bankroll: { start: number; source: string; after: number }
  capped: boolean
  raised: boolean
  totals: { units: number; amount: number }
  warnings: string[]
  items: CalcItemResult[]
}

export async function getBankrollConfig(): Promise<WithSource<BankrollConfigResponse>> {
  const live = await tryFetch<BankrollConfigResponse>('/bankroll/config')
  if (live) return { data: live, source: 'api' }
  throw new Error('无法读取资金配置（请确认 8787 已启动）')
}

export async function calcBankroll(body: {
  bankroll?: number
  remaining_bankroll?: number
  items: CalcItemInput[]
}): Promise<WithSource<CalcResponse>> {
  try {
    const res = await fetch('/api/bankroll/calc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      const text = await res.text()
      throw new Error(text || `注额计算失败 HTTP ${res.status}`)
    }
    return { data: (await res.json()) as CalcResponse, source: 'api' }
  } catch (e) {
    throw e instanceof Error ? e : new Error('注额计算请求失败')
  }
}

export interface StrategyConfig {
  settle_book?: string
  juice?: number
  vote?: { members?: string[]; threshold?: number }
  gates?: unknown[]
  stake_rule?: string
  state_machine?: string | null
  feature_refs?: string[]
  extras?: Record<string, unknown>
  [key: string]: unknown
}

export interface StrategyDef {
  id: number
  strategy_key: string
  version: string
  display_name?: string | null
  markets: string[]
  config: StrategyConfig
  config_fingerprint?: string | null
  status: string
  is_default: boolean
  notes?: string | null
  created_at?: string
  updated_at?: string
}

export interface StrategyTemplate {
  name: string
  description?: string
  config: StrategyConfig
}

export interface ValidationRunResponse {
  id: number
  strategy_def_id: number
  run_label?: string | null
  scope?: string
  settle_book?: string
  params?: Record<string, unknown>
  run_fingerprint?: string | null
  started_at?: string
  finished_at?: string | null
  status: string
  summary?: Record<string, unknown> | null
  error_text?: string | null
  reused: boolean
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const text = await res.text()
  if (!res.ok) {
    throw new Error(text || `HTTP ${res.status}`)
  }
  return (text ? JSON.parse(text) : {}) as T
}

export async function listStrategies(base = '/api'): Promise<WithSource<{ items: StrategyDef[] }>> {
  const live = await tryFetch<{ items: StrategyDef[] }>('/strategies', base)
  if (live) return { data: live, source: 'api' }
  throw new Error(`无法读取方案列表（请确认${base === '/api' ? ' 8787' : '副本 8788'} 已启动）`)
}

export async function listStrategyTemplates(): Promise<
  WithSource<{ items: StrategyTemplate[] }>
> {
  const live = await tryFetch<{ items: StrategyTemplate[] }>('/strategies/templates')
  if (live) return { data: live, source: 'api' }
  throw new Error('无法读取方案模板')
}

export async function composeStrategy(body: {
  strategy_key: string
  version: string
  display_name?: string
  template?: string
  status?: string
  markets?: string[]
  config_overrides?: Record<string, unknown>
  notes?: string
}): Promise<StrategyDef> {
  return postJson<StrategyDef>('/strategies/compose', body)
}

export async function validateStrategy(
  id: number,
  body: {
    shadow?: boolean
    scope?: string
    settle_book?: string
    run_label?: string
    params?: Record<string, unknown>
  } = { shadow: true },
): Promise<ValidationRunResponse> {
  return postJson<ValidationRunResponse>(`/strategies/${id}/validate`, body)
}

export interface MaxDrawdown {
  amount?: number
  pct?: number
  peak_equity?: number
  trough_equity?: number
  peak_index?: number
  trough_index?: number
  peak_at?: string | null
  trough_at?: string | null
}

/** validate/compare/stack summary 中与展示相关的可选字段（其它键仍在 Record 里） */
export interface SummaryMetrics {
  n?: number
  hits?: number
  coverage?: number
  n_eligible?: number
  /** 不可评估场次（如 pinnacle_missing 缺平博数据）；不计入 n_eligible。后端未返回时为 undefined */
  n_not_evaluable?: number | null
  multi_hit_count?: number | null
  pnl?: number
  pnl_amount?: number
  roi?: number | null
  bet_count?: number
  max_drawdown?: MaxDrawdown
  /** 真实水位场次（juice_source=actual） */
  n_actual_water?: number
  /** 回落 0.95 场次（juice_source=fallback） */
  n_fallback_095?: number
  /** n_fallback_095 / (n_actual_water + n_fallback_095)；分母 0 → null */
  fallback_rate?: number | null
  juice_actual_count?: number
  juice_fixed_macau_count?: number
  juice_tier_midpoint_count?: number
  juice_fallback_count?: number
  /** 不可评估按原因：pinnacle_missing / market_insufficient / model_insufficient / model_scope（四键恒在），其它如 open_unusable 另列 */
  n_not_evaluable_by_reason?: Record<string, number> | null
  /** {原因: {子原因: 场数}} */
  n_not_evaluable_by_subreason?: Record<string, Record<string, number>> | null
  /** 分来源已拆（顶层仍是全部合计；L1 只看 by_odds_source.live） */
  odds_source_split?: boolean
  odds_source_split_backfilled?: boolean
  by_odds_source?: Partial<Record<'live' | 'hist' | 'unknown', SummaryMetrics>> | null
  open_basis_split?: boolean
  open_basis_split_backfilled?: boolean
  by_open_basis?: Partial<Record<'first_tick' | 'api_opening' | 'unknown', SummaryMetrics>> | null
  [key: string]: unknown
}


export interface StakeRule {
  default_units?: number
  use_prediction_stake?: boolean
  below_min?: 'skip' | 'raise' | string
  unit_fraction?: number | null
}

export interface CompareSeries {
  x: string[]
  match_ids: number[]
  pnl: number[]
  cumulative_pnl: number[]
  stake?: number[]
  bankroll?: number[]
  drawdown_pct?: number[]
  initial_bankroll?: number
  result_codes?: string[]
  conflict?: boolean[]
  sides?: Array<Record<string, string>>
}

export interface CompareItem {
  strategy_def_id: number
  strategy_key?: string
  version?: string
  status: 'ok' | 'missing_def' | 'missing_run' | string
  run_id?: number | null
  run_fingerprint?: string | null
  summary?: (SummaryMetrics & Record<string, unknown>) | null
  series: CompareSeries
  reused?: boolean | null
  note?: string | null
}

export interface StackConflictLeg {
  strategy_key?: string
  side?: string
  amount?: number
  result_code?: string
  pnl?: number
}

export interface StackConflict {
  match_id: number
  match_uid?: string
  jc_id?: string | null
  home_team?: string | null
  away_team?: string | null
  jingcai_date?: string | null
  kickoff_at?: string | null
  legs?: StackConflictLeg[]
  staked_both?: boolean
  net_pnl?: number
  net_stake?: number
}

export interface CompareStack {
  status?: string
  stake_mode?: string
  stake_rule?: StakeRule | null
  strategies?: string[]
  bankroll_rules?: Record<string, unknown>
  settlement_version?: string
  conflict_policy?: string
  /** 公平对照字段（可在 stack 顶层或 summary 内） */
  n_actual_water?: number
  n_fallback_095?: number
  fallback_rate?: number | null
  summary?: (SummaryMetrics & Record<string, unknown>) | null
  series?: CompareSeries | null
  conflicts?: StackConflict[]
  persisted?: boolean
}

/** compare 可选：同注单真实水位灵敏度副表（不替代主表选边） */
export interface SettlementSensitivity {
  role?: string
  note?: string
  settlement_version?: string
  n_actual_water?: number
  n_fallback_095?: number
  fallback_rate?: number | null
  items?: CompareItem[]
  stack?: CompareStack | null
  delta_vs_primary?: Array<{
    strategy_key?: string
    delta_pnl_amount?: number
    delta_roi?: number | null
    primary_settlement_version?: string
    sensitivity_settlement_version?: string
    [key: string]: unknown
  }>
  [key: string]: unknown
}

export interface CompareResponse {
  scope?: string
  settle_book?: string
  params?: Record<string, unknown>
  stake_mode?: string
  settlement_version?: string
  /** 公平对照（主表口径汇总） */
  n_actual_water?: number
  n_fallback_095?: number
  fallback_rate?: number | null
  fair_compare_note?: string
  items: CompareItem[]
  stack: CompareStack | null
  /** include_settlement_sensitivity=true 时返回 */
  sensitivity?: SettlementSensitivity | null
  /** @deprecated M4 stub; M5 用 stack 对象 */
  stack_note?: string
}

export interface CompareRequest {
  strategy_ids?: number[]
  strategy_keys?: string[]
  scope?: string
  settle_book?: string
  shadow?: boolean
  params?: Record<string, unknown>
  auto_validate?: boolean
  run_label?: string | null
  include_stack?: boolean
  stake_mode?: 'per_strategy' | 'unified' | string
  stake_rule?: StakeRule
  /** 主表结算版本；默认后端 ah_v4_water_midpoint（固定 0.95） */
  settlement_version?: string
  /** 同注单再跑真实水位副表（sensitivity） */
  include_settlement_sensitivity?: boolean
}

export async function compareStrategies(
  body: CompareRequest,
): Promise<CompareResponse> {
  return postJson<CompareResponse>('/strategies/compare', body)
}

/** 只读：按编号读已有验证记录（GET，不新建任何记录）；base 为 /api 或 /api-replica */
export interface ValidationRunDetail {
  id: number
  strategy_def_id?: number | null
  run_label?: string | null
  scope?: string | null
  settle_book?: string | null
  started_at?: string | null
  finished_at?: string | null
  status: string
  summary?: Record<string, unknown> | null
  error_text?: string | null
}

export async function getValidationRun(runId: number, base = '/api'): Promise<ValidationRunDetail> {
  const res = await fetch(`${base}/strategies/runs/${encodeURIComponent(String(runId))}`)
  if (res.status === 404) throw new Error(`没有编号 ${runId} 的验证记录`)
  if (!res.ok) throw new Error(`读取验证记录失败（HTTP ${res.status}）`)
  return (await res.json()) as ValidationRunDetail
}
