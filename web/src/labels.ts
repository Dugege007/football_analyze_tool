/** 用户可见中文标签（API 字段名仍用英文） */

export const STRATEGY_STATUS_LABEL: Record<string, string> = {
  active: '启用',
  shadow: '影子',
  draft: '草稿',
  archived: '已归档',
}

export const COMPARE_STATUS_LABEL: Record<string, string> = {
  ok: '正常',
  missing_run: '缺验证记录',
  missing_def: '方案不存在',
}

/** 不可评估 / 跳过原因（如对比页以后展示 ineligible 原因时用） */
export const SKIP_REASON_LABEL: Record<string, string> = {
  pinnacle_missing: '缺平博数据（不可评估）',
}

export function labelSkipReason(reason: string | null | undefined): string {
  if (!reason) return '—'
  return SKIP_REASON_LABEL[reason] ?? reason
}

export const RESULT_CODE_LABEL: Record<string, string> = {
  win: '赢',
  win_half: '赢半',
  push: '走水',
  lose_half: '输半',
  lose: '输',
  skip: '不下注',
  no_stake: '未下注',
}

export const RUN_STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  ok: '正常',
  failed: '失败',
}

export function labelStrategyStatus(status: string): string {
  return STRATEGY_STATUS_LABEL[status] ?? status
}

export function labelCompareStatus(status: string): string {
  return COMPARE_STATUS_LABEL[status] ?? status
}

export function labelResultCode(code: string): string {
  return RESULT_CODE_LABEL[code] ?? code
}

export function labelRunStatus(status: string): string {
  return RUN_STATUS_LABEL[status] ?? status
}

export function labelReused(reused: boolean | null | undefined): string | null {
  if (reused === true) return '复用缓存'
  if (reused === false) return '新建验证'
  return null
}

export function labelDataSource(source: 'api' | 'mock'): string {
  return source === 'api' ? '已连本地接口' : '假数据'
}

/** 结算盘口展示：常见值译中文，其余原样 */
export function labelSettleBook(book: string | null | undefined): string {
  if (!book) return '—'
  const map: Record<string, string> = {
    macau_close: '澳门收盘',
    macau_open: '澳门初盘',
    macau_mid: '澳门中盘',
  }
  return map[book] ?? book
}

export function labelScope(scope: string | null | undefined): string {
  if (!scope) return '—'
  const map: Record<string, string> = {
    jingcai: '竞彩',
    extra: '扩展',
    all: '全部',
  }
  return map[scope] ?? scope
}

/** 方案模板展示：中文（英文名） */
export function labelTemplate(name: string | null | undefined): string {
  if (!name) return '—'
  const map: Record<string, string> = {
    default: '默认',
    simple_gate: '简单门槛',
  }
  const zh = map[name]
  return zh ? `${zh}（${name}）` : name
}

export const STAKE_MODE_LABEL: Record<string, string> = {
  per_strategy: '各自规则',
  unified: '统一规则',
}

export function labelStakeMode(mode: string | null | undefined): string {
  if (!mode) return '—'
  return STAKE_MODE_LABEL[mode] ?? mode
}

export function formatMaxDrawdown(dd: unknown): string {
  if (!dd || typeof dd !== 'object') return '—'
  const o = dd as { amount?: unknown; pct?: unknown }
  const amount = typeof o.amount === 'number' && Number.isFinite(o.amount) ? o.amount : null
  const pct = typeof o.pct === 'number' && Number.isFinite(o.pct) ? o.pct : null
  if (amount == null && pct == null) return '—'
  const a = amount == null ? '—' : amount.toFixed(2)
  const p = pct == null ? '' : `（${(pct * 100).toFixed(2)}%）`
  return `${a}${p}`
}

export function formatPnlAmount(v: unknown): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  return v.toFixed(2)
}


/** 冲突对阵：主队 vs 客队；缺名时回退（消息文本除外，消息用「-」连接） */
export function formatConflictMatchup(
  home: string | null | undefined,
  away: string | null | undefined,
): string {
  const h = (home ?? '').trim()
  const a = (away ?? '').trim()
  if (h && a) return `${h} vs ${a}`
  if (h) return `${h} vs —`
  if (a) return `— vs ${a}`
  return '对阵未知'
}

/** 竞彩编号展示 */
export function formatJcId(jcId: string | null | undefined, matchId?: number): string {
  const id = (jcId ?? '').trim()
  if (id) return id
  return matchId != null ? `场次 #${matchId}` : '—'
}

/** 开赛时间：尽量可读；失败则原样 */
export function formatKickoffAt(iso: string | null | undefined): string {
  if (!iso) return ''
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(iso)
  if (m) return `${m[1]} ${m[2]}`
  return iso
}


/** L1 互斥桶最小命中阈值（mutex-buckets-min-n）；低于此回报率示「样本不足」 */
export const L1_MIN_HITS = 80

export function formatCoverage(v: unknown): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  // API 现为 0–1 比例；若已是百分数（>1 且 ≤100）则按百分数展示
  if (v >= 0 && v <= 1) return `${(v * 100).toFixed(1)}%`
  if (v > 1 && v <= 100) return `${v.toFixed(1)}%`
  return String(v)
}

export function isSampleInsufficient(hits: unknown, minHits: number = L1_MIN_HITS): boolean {
  return typeof hits === 'number' && Number.isFinite(hits) && hits < minHits
}


/** 影子方案规则卡中文名（仅 status=shadow 时展示；勿用于日用白名单） */
export const SHADOW_SCHEME_LABEL: Record<string, string> = {
  S1: '实力相符·水位平稳买下',
  S2: '升盘路径选边',
  S8: '看不清就过滤',
  N1: '顺分布抬水排除让球',
  N2: '平赔低于中庸（暂不启用）',
  N3: '单拉平阻',
  N4: '高水深开阻／浅开诱',
  // 0.3.19：输入口径改为真实水位后的新台账（SHADOW_S2_V2 / SHADOW_N4_V2）；旧 S2 / N4 冻结
  'S2-V2': '升盘路径选边（真实水位口径）',
  'N4-V2': '高水深开阻／浅开诱（真实水位口径）',
  N5: '泊松相对市场偏差',
  'N5-PIN': '泊松相对平博偏差',
  'IP-B': '滚球状态校准（stub）',
  'IP-A': '滚球泊松衰减（stub）',
  'XG-BSD-1': 'BSD xG 特征（stub）',
}

const SHADOW_SCHEME_IDS = Object.keys(SHADOW_SCHEME_LABEL).sort((a, b) => b.length - a.length)

function matchShadowSchemeId(strategyKey: string): string | null {
  if (SHADOW_SCHEME_LABEL[strategyKey]) return strategyKey
  // 连字符与下划线视为等同（N5-PIN / N5_PIN）；长 id 先匹配，避免 N5-PIN 被认成 N5
  const key = strategyKey.replace(/-/g, '_')
  for (const id of SHADOW_SCHEME_IDS) {
    const nid = id.replace(/-/g, '_')
    if (
      key === nid ||
      key.endsWith('_' + nid) ||
      key.startsWith(nid + '_') ||
      key.includes('_' + nid + '_')
    ) {
      return id
    }
  }
  return null
}

/**
 * 影子方案展示名。仅当 status 为 shadow（或未传 status 但明确只要查表）且 key 能对上规则卡时返回「中文（KEY）」；
 * 非 shadow 一律返回 null，避免混入日用白名单展示。
 */
export function labelShadowScheme(
  strategyKey: string | null | undefined,
  status?: string | null,
): string | null {
  if (status != null && status !== 'shadow') return null
  if (!strategyKey) return null
  const id = matchShadowSchemeId(strategyKey)
  if (!id) return null
  return `${SHADOW_SCHEME_LABEL[id]}（${id}）`
}


/** 结算版本展示（API 值仍用英文） */
export const SETTLEMENT_VERSION_LABEL: Record<string, string> = {
  ah_v4_water_midpoint: '固定0.95（主结算）',
  ah_v4_macau_actual_or_095: '真实水位或回落0.95',
}

export function labelSettlementVersion(v: string | null | undefined): string {
  if (!v) return '—'
  return SETTLEMENT_VERSION_LABEL[v] ?? v
}

/** 回落比例：0–1 → 百分比；null/非法 → — */
export function formatFallbackRate(v: unknown): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  if (v >= 0 && v <= 1) return `${(v * 100).toFixed(1)}%`
  return v.toFixed(2)
}

/** fallback_rate ≥ 此阈值时温和提示真实水位结论可能不可靠 */
export const FALLBACK_RATE_WARN_THRESHOLD = 0.5

export const FALLBACK_RATE_WARN_HINT = '真实水位结论可能不可靠'

export function isFallbackRateHigh(v: unknown, threshold = FALLBACK_RATE_WARN_THRESHOLD): boolean {
  return typeof v === 'number' && Number.isFinite(v) && v >= threshold
}


/** 不可评估原因（validate/compare 的 n_not_evaluable_by_reason；API 键保持英文） */
export const NOT_EVALUABLE_REASON_LABEL: Record<string, string> = {
  pinnacle_missing: '缺平博数据',
  market_insufficient: '参与机构不足',
  model_insufficient: '模型样本不足',
  model_scope: '超出模型范围（杯赛/国际赛）',
  open_unusable: '初盘不可用',
  /** 第二批口径 C：N1 用到报价时刻未知的收盘价（api_closing） */
  close_unusable: '临盘不可用',
  unspecified: '未写原因',
}

/** 主原因的补充说明（没有子原因时附在括号里） */
export const NOT_EVALUABLE_REASON_HINT: Record<string, string> = {
  close_unusable: '收盘价时间未知',
}

/** 悬停里固定先列的主原因（后端四键恒在；open_unusable 等其它原因有值时附后） */
export const NOT_EVALUABLE_REASON_ORDER = [
  'pinnacle_missing',
  'market_insufficient',
  'model_insufficient',
  'model_scope',
]

/** 不可评估子原因（n_not_evaluable_by_subreason{原因: {子原因: 场数}}） */
export const NOT_EVALUABLE_SUBREASON_LABEL: Record<string, string> = {
  no_tick: '无报价',
  stale_tick: '报价过旧',
  water_missing: '水位缺失',
  water_invalid: '水位异常',
  fixture_unmapped: '赛事未对上',
  // ── market_insufficient 下 ──
  books_lt_min: '有效机构不足 3 家',
  /** N5 只认真实水位（n5_water=real_only）：真实水位机构不足 3 家 */
  real_water_lt_3: '真实水位机构不足 3 家',
  // ── model_insufficient 下（同级）──
  team_n_lt_min: '球队样本不足',
  league_n_lt_min: '联赛样本不足',
  /** N5 拟合未收敛（快照 fit_message 存原文） */
  fit_not_converged: '模型未收敛',
  /** 按时间衰减权重求和的有效样本门槛（min_team_wn / min_league_wn） */
  team_wn_lt_min: '球队有效样本不足',
  league_wn_lt_min: '联赛有效样本不足',
  // ── open_unusable 下 ──
  open_time_unknown_after_decision_possible: '初盘时间未知，可能晚于决策时点',
  // 0.3.18：close_unusable 子原因（N1 威廉 1X2 收盘全是接口收盘价）
  close_time_unknown_api_closing: '收盘价时间未知（API 收盘）',
  cup: '杯赛',
  international: '国际赛',
  friendly: '友谊赛',
  non_league: '非联赛',
}

export function labelNotEvaluableReason(k: string): string {
  return NOT_EVALUABLE_REASON_LABEL[k] ?? k
}

export function labelNotEvaluableSubreason(k: string): string {
  return NOT_EVALUABLE_SUBREASON_LABEL[k] ?? k
}


/**
 * 疑似泄漏（leak_suspect）：后端在方案 / 汇总 / 台账上给出时，命中率与回报率置灰、标「疑似泄漏，暂不引用」。
 * 前端不硬编码方案，只读字段；原因代码 → 中文，未知代码原样显示。
 */
export const LEAK_SUSPECT_TAG = '疑似泄漏，暂不引用'
export const LEAK_REASON_LABEL: Record<string, string> = {
  // 文案同后端 config/leak_suspect.json 的 values（0.3.20 起接口会带）
  cutoff_plus24:
    '可用历史赛果的截止钟点被套了「0–10 点 +24」规则：11–14 点开赛的场会放进同日全部比赛（含凌晨场），回测 / 复盘数字疑似泄漏；修好前不引用、不和 V3 比',
}

export interface LeakInfo {
  suspect: boolean
  reason: string | null
}

/**
 * 通用读取 leak_suspect（0.3.20 起后端在 /strategies、compare、runs、预测上带；前端不硬编码方案）：
 *   leak_suspect: 原因代码字符串（如 cutoff_plus24）| true | {reason|code}；说明在 leak_suspect_note（有就附上）。
 * 依次看传入的对象，任一处非空即算疑似泄漏；原因代码取第一处，说明取第一个非空的 note。
 */
export function readLeakSuspect(...objs: unknown[]): LeakInfo {
  let suspect = false
  let code: string | null = null
  let note: string | null = null
  for (const o of objs) {
    if (!o || typeof o !== 'object') continue
    const r = o as Record<string, unknown>
    const v = r.leak_suspect
    if (v != null && v !== false) {
      suspect = true
      if (!code) {
        if (typeof v === 'string') code = v
        else if (typeof v === 'object') {
          const vv = v as Record<string, unknown>
          code = typeof (vv.reason ?? vv.code) === 'string' ? ((vv.reason ?? vv.code) as string) : null
        }
      }
    }
    const n = r.leak_suspect_note ?? r.leak_suspect_reason ?? r.leak_reason
    if (!note && typeof n === 'string' && n) note = n
  }
  if (!suspect) return { suspect: false, reason: null }
  const parts = [code ? (LEAK_REASON_LABEL[code] ?? `后端原因代码：${code}`) : null, note].filter(Boolean)
  return { suspect: true, reason: parts.length ? parts.join('；') : null }
}

/** 台账备注（0.3.19 旧冻结 S2 / N4「触发依据是换算水位」）：原因代码 → 补充说明 */
export const LEDGER_NOTE_REASON_LABEL: Record<string, string> = {
  tier_water_trigger: '旧冻结条目，触发依据是档位换算水位；以后不和真实水位口径（V2）比较',
}

/** 回报率（后端为比例，如 -0.123684）→「-12.37%」 */
export function formatRoi(v: unknown): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  return `${(v * 100).toFixed(2)}%`
}

/** 通用读取台账备注（ledger_note / ledger_note_reason），有就悬停 */
export function readLedgerNote(...objs: unknown[]): string | null {
  for (const o of objs) {
    if (!o || typeof o !== 'object') continue
    const r = o as Record<string, unknown>
    const note = typeof r.ledger_note === 'string' && r.ledger_note ? r.ledger_note : null
    if (!note) continue
    const reason = typeof r.ledger_note_reason === 'string' ? LEDGER_NOTE_REASON_LABEL[r.ledger_note_reason] : undefined
    return reason ? `${note}（${reason}）` : note
  }
  return null
}
