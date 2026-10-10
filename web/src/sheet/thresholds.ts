/**
 * 数据表页 · 自动高亮阈值（唯一出处）。
 *
 * 口径来源：docs/schema/v2_0-data-table-highlight-rules.md（足球分析师 2026-10-08 定）。
 * 规则：改任何阈值都要升 CONFIG_VERSION，并同步改口径文档；不改历史口径说明。
 *
 * 版本记录：
 * - hl_v0   首版。
 * - hl_v0.1 （2026-10-08）公司分歧收紧（0.25 只提示不标色；≥0.5 轻、≥0.75 中）；
 *           §8 七条细则确认；初盘改按各公司首开（open）；例外场（后端 phase_exception；2026-10-08 起有竞彩编号的场按编号判、无 11:30 上限，前端不判）中盘/临盘分「（规则）」「（真实）」。
 * - hl_v0.2 （2026-10-08）返还率上色只看真实水位：指纹 return_hl_water=real_only。
 *           只对 water_source=actual 的返还率格子上色；tier_midpoint（档位中点换算）照常显示数值、灰字、不上色；
 *           无 water_source / 未知值 → 当作未标注，不上色。「档位换算也上色（仅排查用）」开关默认关，排查色不进任何导出。
 *           阈值数值本身不变（仍是 hl_v0.1 的基准偏离 / 兜底绝对值）。
 *           补充（第二批口径 E/F）：§2 水位异动同样只认 actual，档位换算只悬停「跨档：X→Y（档位换算，幅度不精确）」；
 *           欧赔格子也按 water_source 判（actual 上色，tier_midpoint / null 不上色）。
 * - hl_v0.3.1（2026-10-08，API 0.3.20）：hl_v0.3 补丁。返还率兜底改「公司×盘种×阶段」P10/P25/P90（n=场次），
 *           直接读 fallback_hl_level（light/medium/high）；high=中性色「返还率偏高」不进告警。
 *           凯利仍是 hl_v0.3 的 +0.02 过渡，重档留给 hl_v0.4。11:10 自采 ±10 分钟窗；kickoff_drift_min；leak_suspect。
 * - hl_v0.3 （2026-10-08，API 0.3.19，只管上色、不进策略）：
 *           凯利轻档 = 凯利 − 返还率 ≥ 0.02、中档 = 凯利 ≥ 1.02；不设重档（等 hl_v0.4 的 P97 一起定义）。
 *           阈值表按主 / 平 / 客各存一套（为 hl_v0.4 的 kelly_thr_{h,d,a} / kelly_n_{h,d,a} 预留），现在三套同值。
 *           返还率兜底不再统一用 0.90 / 0.93 等固定值：改读每格后端给的 fallback_p25 / fallback_n / fallback_hl_eligible
 *           （本家真实水位返还率分位数，as-of）：低于 P25 轻、低于 P10 中（有 fallback_p10 字段才判）、不低于 P90 中性色
 *           「返还率偏高」（有 fallback_p90 字段才判，不算风险、不进导出）；样本不足（eligible=false 或 n<100）灰字不上色。
 *           水位异动统一只比初盘→临盘（后端 close 格 water_move_eligible），去掉 0.3.18「中盘也可比时比中→临」分支。
 */

export const CONFIG_VERSION = 'hl_v0.3.1'

/**
 * hl_v0.2 返还率上色的水位口径（指纹 return_hl_water=real_only）。
 * 取值以 v2_0-table-matches-api.md §4.1 / §13 为准：actual | tier_midpoint | null。
 */
export const RETURN_HL_WATER = 'real_only' as const
/** 真实水位（API 报价） */
export const WATER_SOURCE_REAL = 'actual'
/** 旧手工档位 t 按 0.70+0.05t 换算的中点水位（近似） */
export const WATER_SOURCE_TIER = 'tier_midpoint'

/** §1 升降盘（临盘 − 初盘，按让球方看） */
export const LINE_MOVE = {
  /** |变化| ≥ 此值：标字色（红升 / 绿降） */
  font: 0.25,
  /** |变化| ≥ 此值：再叠黄填充（大幅变盘） */
  fillYellow: 0.5,
  /** 换边：按降盘绿字 + 橙填充（填充级别 2）。§8-2：换边且 ≥0.5 时只标橙，不再叠黄 */
  switchFillLevel: 2 as const,
}

/**
 * §2 水位异动：max(|Δ主水|, |Δ客水|)，仅盘口不变时判。
 * v0 先用这组数；补数库出分位后按各公司分布校准（目标约 轻10% / 中5% / 严重2%）。
 */
export const WATER_MOVE = {
  level1: 0.1,
  level2: 0.15,
  level3: 0.2,
  /** 盘口变了水位会重置，不判 */
  /** 0.3.18 起同盘口由后端 water_move_eligible 判定，前端不再自己比盘口；保留作口径记录 */
  requireSameLine: true,
}

/**
 * §3 公司分歧（同一时间点、同一阶段；初/中/临每个阶段都判）。
 * §8-3：按区间判，下限包含、上限不包含。hl_v0.1 收紧（hl_v0 为 0.25 轻 / 0.5 中）。
 */
export const DISAGREE = {
  /** 最大−最小 ≥ 0.25 且 < 0.5：不标色，只在悬停提示里显示 */
  hintOnly: 0.25,
  /** ≥ 0.5 且 < 0.75：轻（黄） */
  level1: 0.5,
  /** ≥ 0.75：中（橙） */
  level2: 0.75,
  /** 平博与澳门让球方向相反：红（级别 3）。§8-4：平手视为没有方向，不算反向 */
  pinnacleMacauOppositeLevel: 3 as const,
}

/** §4/§5 返还率：按公司 as-of 滚动中位数基准偏离 */
export const RETURN_RATE_BASELINE_DELTA = {
  lowLevel1: 0.015,
  lowLevel2: 0.03,
  highLevel1: 0.015,
}

/** §4 亚盘返还率兜底绝对值（基准为 null 时） */
export const AH_RETURN_RATE_FALLBACK: Record<
  'pinnacle' | 'macau' | 'crown' | 'william',
  { lowLevel1: number; lowLevel2: number; highLevel1: number | null }
> = {
  macau: { lowLevel1: 0.93, lowLevel2: 0.91, highLevel1: 0.97 },
  crown: { lowLevel1: 0.93, lowLevel2: 0.91, highLevel1: 0.97 },
  william: { lowLevel1: 0.93, lowLevel2: 0.91, highLevel1: 0.97 },
  /** 平博：高位不标 */
  pinnacle: { lowLevel1: 0.955, lowLevel2: 0.94, highLevel1: null },
}

/** §5 欧赔返还率兜底绝对值（基准为 null 时）。§8-5：没有偏高兜底值，偏高不标 */
export const X1X2_RETURN_RATE_FALLBACK: Record<
  'pinnacle' | 'macau' | 'crown' | 'william',
  { lowLevel1: number; lowLevel2: number; highLevel1: number | null }
> = {
  macau: { lowLevel1: 0.9, lowLevel2: 0.88, highLevel1: null },
  crown: { lowLevel1: 0.9, lowLevel2: 0.88, highLevel1: null },
  william: { lowLevel1: 0.9, lowLevel2: 0.88, highLevel1: null },
  pinnacle: { lowLevel1: 0.95, lowLevel2: 0.93, highLevel1: null },
}

/** 凯利三项（胜 / 平 / 负）；hl_v0.4 后端字段后缀 h / d / a */
export const KELLY_OUTCOMES = ['home', 'draw', 'away'] as const
export type KellyOutcome = (typeof KELLY_OUTCOMES)[number]

/** 单项凯利阈值。hl_v0.3：轻 = 凯利 − 返还率 ≥ lightOverReturnRate；中 = 凯利 ≥ medium；重档 null = 不设 */
export interface KellyOutcomeThreshold {
  lightOverReturnRate: number
  medium: number
  /** hl_v0.3 不设重档（分析师 2026-10-08 18:24 定：等 hl_v0.4 的 P97 一起定义） */
  heavy: number | null
}

/** hl_v0.3 凯利固定余量（后端 config.kelly_highlight.margin） */
export const KELLY_MARGIN = 0.02

const KELLY_V03: KellyOutcomeThreshold = { lightOverReturnRate: KELLY_MARGIN, medium: 1 + KELLY_MARGIN, heavy: null }

/**
 * §6 凯利阈值表：按主 / 平 / 客各存一套。hl_v0.3 三套取同一个值；
 * TODO(hl_v0.4)：改读后端每格 kelly_thr_{h,d,a}（相对偏差 r = 凯利 ÷ 返还率 − 1 的 P85 / P92 / P97）与 kelly_n_{h,d,a}，
 *   单项 n < 100 灰字「样本不足，暂不判断」；跟后端 config_version 一起切。
 */
export const KELLY_THRESHOLDS: Record<KellyOutcome, KellyOutcomeThreshold> = {
  home: { ...KELLY_V03 },
  draw: { ...KELLY_V03 },
  away: { ...KELLY_V03 },
}
export const KELLY = {
  overReturnRateLevel: 1 as const,
  overOneLevel: 2 as const,
}

/**
 * hl_v0.3 返还率兜底（替代 AH_/X1X2_RETURN_RATE_FALLBACK 的固定绝对值；那两张表只留给「档位换算也上色（仅排查用）」）。
 * 分位数与样本数由后端每格给出（fallback_p25 / fallback_n / fallback_hl_eligible；P10 / P90 字段有就用），前端只读。
 * 低于 P25 → 轻；低于 P10 → 中；不设重档。不低于 P90 → 中性色「返还率偏高」（不算风险、不进导出、不计入风险统计）。
 */
export const RR_FALLBACK = {
  /** 后端 config.return_rate_fallback_hl_v03.min_n；前端也按 n < 100 兜一道 */
  minN: 100,
  belowP25Level: 1 as const,
  belowP10Level: 2 as const,
}

/**
 * 快照阶段时刻（初盘取哪条、中盘/临盘的目标时间、例外场范围、11:10 即时快照）不在前端计算：
 * 由后端 /table/matches 给出每个阶段的 target_time / recorded_at、phase_exception 与 live[].label，前端只展示。
 */
