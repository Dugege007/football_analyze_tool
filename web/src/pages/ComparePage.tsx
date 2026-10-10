import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Empty,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import ReactECharts from 'echarts-for-react'
import {
  compareStrategies,
  listStrategies,
  type CompareResponse,
  type StackConflict,
  type StrategyDef,
} from '../api/client'
import {
  FALLBACK_RATE_WARN_HINT,
  formatConflictMatchup,
  formatCoverage,
  formatFallbackRate,
  formatJcId,
  formatKickoffAt,
  formatMaxDrawdown,
  formatPnlAmount,
  formatRoi,
  isFallbackRateHigh,
  isSampleInsufficient,
  LEAK_SUSPECT_TAG,
  readLedgerNote,
  readLeakSuspect,
  type LeakInfo,
  labelCompareStatus,
  labelResultCode,
  labelReused,
  labelScope,
  labelSettleBook,
  labelSettlementVersion,
  labelShadowScheme,
  labelStakeMode,
} from '../labels'
import {
  ALL_SOURCES_LABEL,
  ALL_SOURCES_TIP,
  ODDS_SOURCES,
  ODDS_SOURCE_LABEL,
  ODDS_SOURCE_TIP,
  CLOSE_BASIS_KEYS,
  CLOSE_BASIS_LABEL,
  OPEN_BASIS_KEYS,
  OPEN_BASIS_LABEL,
  SPLIT_BACKFILLED_TIP,
  closeBasisSplit,
  hasOddsSourceSplit,
  notEvaluableLines,
  openBasisSplit,
  sourceSummary,
  type OddsSource,
} from './compareSplit'
import { BasisTip } from './BasisTip'
import RunViewer from './RunViewer'
import {
  apiBase,
  fetchHealthMeta,
  metaMismatch,
  REPLICA_READONLY_TIP,
  useDbSource,
  type DbMeta,
} from '../api/dataSource'
import { DataSourceSwitch, ReplicaBanner } from '../api/DataSourceBar'

const { Title, Text, Paragraph } = Typography

const LINE_COLORS = [
  '#1677ff',
  '#52c41a',
  '#fa8c16',
  '#722ed1',
  '#eb2f96',
  '#13c2c2',
  '#faad14',
  '#2f54eb',
]

const STACK_LINE_NAME = '组合资金'
const STACK_LINE_PRIMARY = '组合资金（主结算）'
const STACK_LINE_SENS = '组合资金（真实水位）'
const STACK_SENS_COLOR = '#08979c'

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

type SummaryRow = {
  key: string
  /** source = 主行下按 by_odds_source 拆出的来源行 */
  kind: 'strategy' | 'stack' | 'source'
  label: string
  /** 主行：后端给了分来源（标「全部来源」） */
  allSources?: boolean
  splitBackfilled?: boolean
  source?: OddsSource
  status?: string
  run_id?: number | null
  reused?: boolean | null
  summary?: Record<string, unknown> | null
  /** 后端 leak_suspect（方案 / 汇总上任一处给出即算）：命中、覆盖率、盈亏、回报率置灰，不引用 */
  leak?: LeakInfo
  /** 后端 ledger_note（如旧冻结 S2 / N4「触发依据是换算水位」） */
  ledgerNote?: string | null
}

/** 疑似泄漏行：数字灰显 + 悬停原因 */
function LeakGrey({ leak, children }: { leak?: LeakInfo; children: React.ReactNode }) {
  if (!leak?.suspect) return <>{children}</>
  return (
    <Tooltip title={`${LEAK_SUSPECT_TAG}${leak.reason ? `：${leak.reason}` : ''}`}>
      <Text type="secondary" style={{ textDecoration: 'line-through dotted', cursor: 'help' }}>
        {children}
      </Text>
    </Tooltip>
  )
}

type ConflictRow = {
  key: string
  conflict: StackConflict
  jcLabel: string
  matchupLabel: string
  whenLabel: string
  directionsLabel: string
}

export default function ComparePage() {
  const [loading, setLoading] = useState(false)
  const [comparing, setComparing] = useState(false)
  const [items, setItems] = useState<StrategyDef[]>([])
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [autoValidate, setAutoValidate] = useState(true)
  const [includeStack, setIncludeStack] = useState(true)
  const [includeSettlementSensitivity, setIncludeSettlementSensitivity] =
    useState(false)
  const [stakeMode, setStakeMode] = useState<'per_strategy' | 'unified'>(
    'per_strategy',
  )
  const [result, setResult] = useState<CompareResponse | null>(null)
  // 0.3.18：数据源（现网 / 副本），标识以 /health 返回的 meta 为准；副本只读，不能对比（对比会写入验证记录，副本一律 403）
  const [dbSource, setDbSource] = useDbSource()
  const [dbMeta, setDbMeta] = useState<DbMeta | null>(null)
  const [metaError, setMetaError] = useState<string | null>(null)
  const replicaMode = dbSource === 'replica' || dbMeta?.readonly === true
  const readonlyLock = replicaMode || metaError != null

  const refresh = useCallback(async () => {
    setLoading(true)
    setMetaError(null)
    setDbMeta(null)
    setItems([])
    try {
      const meta = await fetchHealthMeta(dbSource)
      const bad = metaMismatch(dbSource, meta)
      if (bad) {
        setMetaError(bad)
        return
      }
      setDbMeta(meta)
      const res = await listStrategies(apiBase(dbSource))
      setItems(res.data.items)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '加载方案失败')
    } finally {
      setLoading(false)
    }
  }, [dbSource])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const selected = useMemo(
    () => items.filter((s) => selectedIds.includes(s.id)),
    [items, selectedIds],
  )

  const options = useMemo(
    () =>
      items.map((s) => ({
        value: s.id,
        label: `${s.strategy_key}@${s.version}${s.is_default ? '（默认）' : ''}${(() => {
          const zh = labelShadowScheme(s.strategy_key, s.status)
          const sub = zh || s.display_name
          return sub ? ` · ${sub}` : ''
        })()}${readLeakSuspect(s).suspect ? ` · ${LEAK_SUSPECT_TAG}` : ''}`,
      })),
    [items],
  )


  const onCompare = async () => {
    if (readonlyLock) {
      message.warning(REPLICA_READONLY_TIP)
      return
    }
    if (selectedIds.length === 0) {
      message.warning('请先选择至少一个方案')
      return
    }
    setComparing(true)
    try {
      const body: Parameters<typeof compareStrategies>[0] = {
        strategy_ids: selectedIds,
        scope: 'jingcai',
        settle_book: 'macau_close',
        shadow: true,
        auto_validate: autoValidate,
        include_stack: includeStack,
      }
      if (includeSettlementSensitivity) {
        body.include_settlement_sensitivity = true
      }
      if (includeStack) {
        body.stake_mode = stakeMode
        if (stakeMode === 'unified') {
          body.stake_rule = {
            default_units: 1,
            use_prediction_stake: false,
            below_min: 'skip',
          }
        }
      }
      const data = await compareStrategies(body)
      setResult(data)
      const ok = data.items.filter((i) => i.status === 'ok').length
      const miss = data.items.length - ok
      const stackOk = data.stack && data.stack.status === 'ok'
      message.success(
        miss === 0
          ? `对比完成：${ok} 个方案${stackOk ? ' · 已含方案叠加' : ''}`
          : `对比完成：${ok} 个可用，${miss} 个不可用`,
      )
    } catch (e) {
      message.error(e instanceof Error ? e.message : '对比失败')
    } finally {
      setComparing(false)
    }
  }

  const stackActive =
    !!result?.stack &&
    result.stack !== null &&
    (result.stack.series?.bankroll?.length ?? 0) > 0

  const sensStackSeries =
    includeSettlementSensitivity &&
    result?.sensitivity?.stack?.series &&
    (result.sensitivity.stack.series.bankroll?.length ?? 0) > 0
      ? result.sensitivity.stack.series
      : null

  const dualStack = stackActive && !!sensStackSeries

  const chartOption = useMemo(() => {
    const okItems = (result?.items ?? []).filter(
      (i) => i.status === 'ok' && (i.series?.cumulative_pnl?.length ?? 0) > 0,
    )
    const stackSeries = stackActive ? result!.stack!.series! : null
    if (okItems.length === 0 && !stackSeries) return null

    const primaryStackName = dualStack ? STACK_LINE_PRIMARY : STACK_LINE_NAME
    const axisSource =
      stackSeries && (stackSeries.x?.length ?? 0) > 0
        ? stackSeries
        : okItems.reduce((a, b) =>
            (a.series.x?.length ?? 0) >= (b.series.x?.length ?? 0) ? a : b,
          ).series

    const categories = (axisSource.x ?? []).map((d, idx) => {
      const mid = axisSource.match_ids?.[idx]
      return mid != null ? `${d} #${mid}` : d
    })

    const legendData = [
      ...okItems.map(
        (i) => `${i.strategy_key ?? i.strategy_def_id}@${i.version ?? ''}`,
      ),
      ...(stackSeries ? [primaryStackName] : []),
      ...(sensStackSeries ? [STACK_LINE_SENS] : []),
    ]

    const stackTooltip = (
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      p: any,
      seriesData: NonNullable<typeof stackSeries>,
      name: string,
    ) => {
      const idx = p.dataIndex as number
      const date = seriesData.x?.[idx] ?? ''
      const mid = seriesData.match_ids?.[idx]
      const pnl = seriesData.pnl?.[idx]
      const conflict = seriesData.conflict?.[idx]
      return (
        `<div><span style="color:${p.color}">●</span> <b>${name}</b>` +
        `<br/>权益 ${p.value}` +
        (pnl != null ? ` · 本场盈亏 ${pnl}` : '') +
        (conflict ? ' · <span style="color:#cf1322">冲突</span>' : '') +
        `<br/><span style="color:#888">${date}${mid != null ? ` · 场次 ${mid}` : ''}</span></div>`
      )
    }

    const series = [
      ...okItems.map((item, i) => ({
        name: `${item.strategy_key ?? item.strategy_def_id}@${item.version ?? ''}`,
        type: 'line' as const,
        yAxisIndex: 0,
        showSymbol: item.series.cumulative_pnl.length <= 60,
        symbolSize: 4,
        data: item.series.cumulative_pnl,
        itemStyle: { color: LINE_COLORS[i % LINE_COLORS.length] },
        lineStyle: { width: 2 },
      })),
      ...(stackSeries
        ? [
            {
              name: primaryStackName,
              type: 'line' as const,
              yAxisIndex: 1,
              showSymbol: (stackSeries.bankroll?.length ?? 0) <= 60,
              symbolSize: 5,
              data: stackSeries.bankroll ?? [],
              itemStyle: { color: '#cf1322' },
              lineStyle: { width: 3, type: 'dashed' as const },
              z: 10,
            },
          ]
        : []),
      ...(sensStackSeries
        ? [
            {
              name: STACK_LINE_SENS,
              type: 'line' as const,
              yAxisIndex: 1,
              showSymbol: (sensStackSeries.bankroll?.length ?? 0) <= 60,
              symbolSize: 5,
              data: sensStackSeries.bankroll ?? [],
              itemStyle: { color: STACK_SENS_COLOR },
              lineStyle: { width: 2.5, type: 'dotted' as const },
              z: 11,
            },
          ]
        : []),
    ]

    return {
      tooltip: {
        trigger: 'axis',
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (params: any) => {
          const arr = Array.isArray(params) ? params : [params]
          if (!arr.length) return ''
          const idx = arr[0].dataIndex as number
          const lines: string[] = []
          for (const p of arr) {
            if (p.seriesName === primaryStackName && stackSeries) {
              lines.push(stackTooltip(p, stackSeries, primaryStackName))
              continue
            }
            if (p.seriesName === STACK_LINE_SENS && sensStackSeries) {
              lines.push(stackTooltip(p, sensStackSeries, STACK_LINE_SENS))
              continue
            }
            const item = okItems.find(
              (it) =>
                `${it.strategy_key ?? it.strategy_def_id}@${it.version ?? ''}` ===
                p.seriesName,
            )
            if (!item) continue
            const date = item.series.x?.[idx] ?? ''
            const mid = item.series.match_ids?.[idx]
            const pnl = item.series.pnl?.[idx]
            const code = item.series.result_codes?.[idx]
            const codeLabel = code ? labelResultCode(code) : ''
            lines.push(
              `<div><span style="color:${p.color}">●</span> <b>${p.seriesName}</b>` +
                `<br/>累计盈亏 ${p.value}` +
                (pnl != null ? ` · 本场盈亏 ${pnl}` : '') +
                (codeLabel ? ` · ${codeLabel}` : '') +
                `<br/><span style="color:#888">${date}${mid != null ? ` · 场次 ${mid}` : ''}</span></div>`,
            )
          }
          return lines.join('<br/>')
        },
      },
      legend: { data: legendData, type: 'scroll' },
      grid: { left: 56, right: stackSeries ? 64 : 24, top: 48, bottom: 72 },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: {
          rotate: 45,
          fontSize: 10,
          formatter: (v: string) => {
            const space = v.indexOf(' ')
            return space > 0 ? v.slice(0, space) : v
          },
        },
      },
      yAxis: [
        {
          type: 'value',
          name: '累计盈亏',
          scale: true,
        },
        {
          type: 'value',
          name: '组合资金',
          scale: true,
          show: !!stackSeries,
        },
      ],
      series,
    }
  }, [result, stackActive, sensStackSeries, dualStack])

  const conflictRows: ConflictRow[] = useMemo(() => {
    const conflicts = result?.stack?.conflicts ?? []
    return conflicts.map((c, idx) => {
      const dirs = (c.legs ?? [])
        .map((leg) => `${leg.strategy_key ?? '—'}：${leg.side ?? '—'}`)
        .join('；')
      const datePart = (c.jingcai_date ?? '').trim()
      const kick = formatKickoffAt(c.kickoff_at)
      let whenLabel = ''
      if (datePart && kick) {
        // kick already includes date when ISO parses; avoid duplicating if same day prefix
        whenLabel = kick.startsWith(datePart) ? kick : `${datePart} · ${kick}`
      } else {
        whenLabel = kick || datePart
      }
      return {
        key: `${c.match_id}-${idx}`,
        conflict: c,
        jcLabel: formatJcId(c.jc_id, c.match_id),
        matchupLabel: formatConflictMatchup(c.home_team, c.away_team),
        whenLabel,
        directionsLabel: dirs || '—',
      }
    })
  }, [result])

  /** summary 优先，缺则回落顶层 fair 字段 */
  const mergeFair = (
    summary: Record<string, unknown> | null | undefined,
    top?: {
      n_actual_water?: number
      n_fallback_095?: number
      fallback_rate?: number | null
    } | null,
  ): Record<string, unknown> | null => {
    if (!summary && !top) return null
    const base: Record<string, unknown> = { ...(summary ?? {}) }
    if (base.n_actual_water == null && top?.n_actual_water != null) {
      base.n_actual_water = top.n_actual_water
    }
    if (base.n_fallback_095 == null && top?.n_fallback_095 != null) {
      base.n_fallback_095 = top.n_fallback_095
    }
    if (base.fallback_rate == null && top && 'fallback_rate' in top) {
      base.fallback_rate = top.fallback_rate
    }
    return base
  }

  const summaryRows: SummaryRow[] = useMemo(() => {
    if (!result) return []
    const rows: SummaryRow[] = result.items.map((r) => {
      const def = items.find((s) => s.id === r.strategy_def_id)
      const base = `${r.strategy_key ?? r.strategy_def_id}${r.version ? `@${r.version}` : ''}`
      const zh = labelShadowScheme(r.strategy_key, def?.status)
      return {
        key: `s-${r.strategy_def_id}-${r.run_id ?? r.status}`,
        kind: 'strategy' as const,
        label: zh ? `${base} · ${zh}` : base,
        status: r.status,
        run_id: r.run_id,
        reused: r.reused,
        summary: r.summary,
        // 不硬编码方案：只看后端字段（item / summary / 方案定义 / 方案 config 任一处）
        leak: readLeakSuspect(r, r.summary, def, def?.config),
        ledgerNote: readLedgerNote(r, r.summary),
      }
    })
    if (result.stack && result.stack.status === 'ok') {
      rows.push({
        key: 'stack',
        kind: 'stack',
        label: dualStack ? '方案叠加（主结算）' : '方案叠加（组合）',
        status: result.stack.status,
        summary: mergeFair(result.stack.summary, result.stack),
      })
    }
    const sensStack = result.sensitivity?.stack
    if (
      includeSettlementSensitivity &&
      sensStack &&
      (sensStack.status === 'ok' || sensStack.summary)
    ) {
      rows.push({
        key: 'stack-sens',
        kind: 'stack',
        label: '方案叠加（真实水位）',
        status: sensStack.status ?? 'ok',
        summary: mergeFair(sensStack.summary, {
          n_actual_water: result.sensitivity?.n_actual_water ?? sensStack.n_actual_water,
          n_fallback_095: result.sensitivity?.n_fallback_095 ?? sensStack.n_fallback_095,
          fallback_rate: result.sensitivity?.fallback_rate ?? sensStack.fallback_rate,
        }),
      })
    }
    // 分来源：每个主行后面按 live / hist / unknown 各拆一行（不出合计行；主行即「全部来源」）
    const out: SummaryRow[] = []
    for (const row of rows) {
      const split = hasOddsSourceSplit(row.summary)
      out.push({
        ...row,
        allSources: split,
        splitBackfilled: row.summary?.odds_source_split_backfilled === true,
      })
      if (!split) continue
      for (const src of ODDS_SOURCES) {
        out.push({
          key: `${row.key}-src-${src}`,
          kind: 'source',
          label: ODDS_SOURCE_LABEL[src],
          source: src,
          summary: sourceSummary(row.summary, src),
          leak: row.leak,
        })
      }
    }
    return out
  }, [result, items, dualStack, includeSettlementSensitivity])

  const sensFallbackHigh = isFallbackRateHigh(
    result?.sensitivity?.fallback_rate ??
      result?.sensitivity?.stack?.fallback_rate ??
      result?.sensitivity?.stack?.summary?.fallback_rate,
  )

  const summaryColumns: ColumnsType<SummaryRow> = [
    {
      title: '方案',
      dataIndex: 'label',
      width: 280,
      render: (label: string, r) => {
        if (r.kind === 'source') {
          return (
            <Tooltip title={r.source ? ODDS_SOURCE_TIP[r.source] : undefined}>
              <Text type="secondary" style={{ paddingLeft: 16 }}>
                └ {label}
              </Text>
            </Tooltip>
          )
        }
        const ob = openBasisSplit(r.summary)
        const cb = closeBasisSplit(r.summary)
        return (
          <Space size={4} wrap>
            {r.kind === 'stack' ? <Text strong>{label}</Text> : <Text>{label}</Text>}
            {r.leak?.suspect ? (
              <Tooltip title={r.leak.reason ?? '后端标记疑似泄漏'}>
                <Tag color="default" style={{ marginInlineEnd: 0, color: '#8c8c8c' }}>
                  {LEAK_SUSPECT_TAG}
                </Tag>
              </Tooltip>
            ) : null}
            {r.ledgerNote ? (
              <Tooltip title={r.ledgerNote}>
                <Tag style={{ marginInlineEnd: 0 }}>台账备注</Tag>
              </Tooltip>
            ) : null}
            {r.allSources ? (
              <Tooltip title={r.splitBackfilled ? `${ALL_SOURCES_TIP}。${SPLIT_BACKFILLED_TIP}` : ALL_SOURCES_TIP}>
                <Tag style={{ marginInlineEnd: 0 }}>{ALL_SOURCES_LABEL}</Tag>
              </Tooltip>
            ) : null}
            {ob ? <BasisTip title="初盘口径" split={ob} keys={OPEN_BASIS_KEYS} labels={OPEN_BASIS_LABEL} /> : null}
            {cb ? <BasisTip title="临盘口径" split={cb} keys={CLOSE_BASIS_KEYS} labels={CLOSE_BASIS_LABEL} /> : null}
          </Space>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (s: string | undefined, r) => {
        if (r.kind === 'source') return <Tag>分来源</Tag>
        if (!s) return '—'
        if (r.kind === 'stack') return <Tag color="purple">组合</Tag>
        const color =
          s === 'ok' ? 'success' : s === 'missing_run' ? 'warning' : 'error'
        return <Tag color={color}>{labelCompareStatus(s)}</Tag>
      },
    },
    {
      title: '验证记录',
      width: 140,
      render: (_, r) => {
        if (r.kind === 'source') return ''
        if (r.kind === 'stack') return '实时'
        const reusedLabel = labelReused(r.reused)
        return r.run_id != null ? (
          <Text>
            #{r.run_id}
            {reusedLabel ? (
              <Tag color={r.reused ? 'blue' : undefined} style={{ marginLeft: 4 }}>
                {reusedLabel}
              </Tag>
            ) : null}
          </Text>
        ) : (
          '—'
        )
      },
    },
    {
      title: '场次',
      width: 64,
      render: (_, r) => num(r.summary?.n) ?? '—',
    },
    {
      title: '可评场次',
      width: 88,
      render: (_, r) => num(r.summary?.n_eligible) ?? '—',
    },
    // 「不可评估」：仅当接口返回 n_not_evaluable 时出列（如缺平博数据的场）
    ...(summaryRows.some((r) => r.summary?.n_not_evaluable != null)
      ? [
          {
            title: '不可评估',
            width: 88,
            render: (_: unknown, r: SummaryRow) => {
              const n = num(r.summary?.n_not_evaluable)
              if (n == null) return '—'
              const lines = notEvaluableLines(r.summary)
              if (!lines) return n
              return (
                <Tooltip
                  title={
                    <div>
                      {lines.map((l) => (
                        <div key={l}>{l}</div>
                      ))}
                    </div>
                  }
                >
                  <span style={{ borderBottom: '1px dashed #bfbfbf', cursor: 'help' }}>{n}</span>
                </Tooltip>
              )
            },
          },
        ]
      : []),
    {
      title: '命中数',
      width: 72,
      render: (_, r) => <LeakGrey leak={r.leak}>{num(r.summary?.hits) ?? '—'}</LeakGrey>,
    },
    {
      title: '覆盖率',
      width: 80,
      render: (_, r) => <LeakGrey leak={r.leak}>{formatCoverage(r.summary?.coverage)}</LeakGrey>,
    },
    {
      title: '真实水位场次',
      width: 112,
      render: (_, r) => num(r.summary?.n_actual_water) ?? '—',
    },
    {
      title: '回落0.95场次',
      width: 112,
      render: (_, r) => num(r.summary?.n_fallback_095) ?? '—',
    },
    {
      title: '回落比例',
      width: 96,
      render: (_, r) => {
        const text = formatFallbackRate(r.summary?.fallback_rate)
        if (text === '—') return text
        if (isFallbackRateHigh(r.summary?.fallback_rate)) {
          return (
            <Text type="warning" title={FALLBACK_RATE_WARN_HINT}>
              {text}
            </Text>
          )
        }
        return text
      },
    },
    {
      title: '盈亏',
      width: 88,
      render: (_, r) => <LeakGrey leak={r.leak}>{formatPnlAmount(r.summary?.pnl_amount ?? r.summary?.pnl)}</LeakGrey>,
    },
    {
      title: '回报率',
      width: 140,
      render: (_, r) => {
        const v = num(r.summary?.roi)
        if (v == null) return '—'
        const text = formatRoi(v)
        if (r.leak?.suspect) return <LeakGrey leak={r.leak}>{text}</LeakGrey>
        // L1：每行只看自己的命中数（hits，不是场次），来源之间不合并
        if (isSampleInsufficient(r.summary?.hits)) {
          return (
            <Space size={4} wrap>
              <Text type="secondary">{text}</Text>
              <Tag>样本不足</Tag>
            </Space>
          )
        }
        if (r.allSources) {
          return (
            <Tooltip title={ALL_SOURCES_TIP}>
              <Text type="secondary">{text}</Text>
            </Tooltip>
          )
        }
        return text
      },
    },
    {
      title: '下注数',
      width: 72,
      render: (_, r) => num(r.summary?.bet_count) ?? '—',
    },
    {
      title: '最大回撤',
      width: 140,
      render: (_, r) => formatMaxDrawdown(r.summary?.max_drawdown),
    },
  ]

  const conflictColumns: ColumnsType<ConflictRow> = [
    {
      title: '编号',
      width: 88,
      render: (_, r) => r.jcLabel,
    },
    {
      title: '对阵',
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text>{r.matchupLabel}</Text>
          {r.whenLabel ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {r.whenLabel}
            </Text>
          ) : null}
        </Space>
      ),
    },
    {
      title: '各方方向',
      render: (_, r) => r.directionsLabel,
    },
    {
      title: '双边下注',
      width: 96,
      render: (_, r) =>
        r.conflict.staked_both == null
          ? '—'
          : r.conflict.staked_both
            ? '是'
            : '否',
    },
    {
      title: '净盈亏',
      width: 88,
      render: (_, r) => formatPnlAmount(r.conflict.net_pnl),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <ReplicaBanner meta={dbMeta} />
      {metaError ? <Alert type="error" showIcon message="数据源标识不符" description={metaError} /> : null}
      <Card>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
          <Title level={3} style={{ marginTop: 0 }}>
            对比
          </Title>
          <DataSourceSwitch
            value={dbSource}
            onChange={(s) => {
              setDbSource(s)
              setSelectedIds([])
              setResult(null)
            }}
          />
        </div>
        <Paragraph type="secondary" style={{ marginBottom: 12 }}>
          多选方案后拉取对比数据；可开启方案叠加与结算灵敏度（同注单真实水位副表，分色叠曲线）。
        </Paragraph>
        <Space wrap style={{ width: '100%', marginBottom: 12 }} align="start">
          <Select
            mode="multiple"
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="选择要对比的方案（可多选）"
            loading={loading}
            style={{ minWidth: 360, maxWidth: 720, flex: 1 }}
            options={options}
            value={selectedIds}
            onChange={(ids: number[]) => {
              setSelectedIds(ids)
              setResult(null)
            }}
          />
          <Space wrap>
            <Text type="secondary">自动验证</Text>
            <Tooltip title={readonlyLock ? REPLICA_READONLY_TIP : undefined}>
              <Switch checked={autoValidate && !readonlyLock} disabled={readonlyLock} onChange={setAutoValidate} />
            </Tooltip>
            <Text type="secondary">方案叠加</Text>
            <Switch
              checked={includeStack}
              onChange={(v) => {
                setIncludeStack(v)
                setResult(null)
              }}
            />
            <Text type="secondary">结算灵敏度</Text>
            <Switch
              checked={includeSettlementSensitivity}
              onChange={(v) => {
                setIncludeSettlementSensitivity(v)
                setResult(null)
              }}
            />
            <Select
              value={stakeMode}
              disabled={!includeStack}
              style={{ width: 128 }}
              options={[
                { value: 'per_strategy', label: labelStakeMode('per_strategy') },
                { value: 'unified', label: labelStakeMode('unified') },
              ]}
              onChange={(v: 'per_strategy' | 'unified') => {
                setStakeMode(v)
                setResult(null)
              }}
            />
            <Tooltip
              title={
                readonlyLock
                  ? `${REPLICA_READONLY_TIP}：对比会新建验证记录，副本实例拒绝一切写入；可在下方按编号查看已有验证记录`
                  : undefined
              }
            >
              <Button
                type="primary"
                loading={comparing}
                disabled={selectedIds.length === 0 || readonlyLock}
                onClick={() => void onCompare()}
              >
                {readonlyLock ? `开始对比（${REPLICA_READONLY_TIP}）` : '开始对比'}
              </Button>
            </Tooltip>
          </Space>
        </Space>
        {selected.length > 0 ? (
          <Space wrap>
            {selected.map((s) => {
              const zh = labelShadowScheme(s.strategy_key, s.status)
              return (
                <Tag key={s.id} color={s.is_default ? 'blue' : undefined}>
                  #{s.id} {s.strategy_key}@{s.version}
                  {zh ? ` · ${zh}` : ''}
                </Tag>
              )
            })}
          </Space>
        ) : null}
      </Card>

      <RunViewer source={dbSource} />

      <Card
        title={stackActive ? '累计盈亏 / 组合资金' : '累计盈亏折线'}
        extra={
          result ? (
            <Space size={8} wrap>
              <Tag>范围 {labelScope(result.scope)}</Tag>
              <Tag>结算 {labelSettleBook(result.settle_book)}</Tag>
              {result.settlement_version ? (
                <Tag>
                  结算版本 {labelSettlementVersion(result.settlement_version)}
                </Tag>
              ) : null}
              {result.stake_mode ? (
                <Tag>注额 {labelStakeMode(result.stake_mode)}</Tag>
              ) : null}
              {stackActive ? (
                <Tag color="red">
                  {dualStack ? '方案叠加 · 双结算' : '方案叠加 · 已接'}
                </Tag>
              ) : includeStack ? (
                <Tag color="default">方案叠加 · 无组合线</Tag>
              ) : (
                <Tag color="default">方案叠加 · 关闭</Tag>
              )}
              {includeSettlementSensitivity ? (
                <Tag color={result.sensitivity ? 'cyan' : 'default'}>
                  {result.sensitivity ? '结算灵敏度 · 已接' : '结算灵敏度 · 无副表'}
                </Tag>
              ) : null}
            </Space>
          ) : null
        }
      >
        {result?.fair_compare_note ? (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="公平对照"
            description={result.fair_compare_note}
          />
        ) : null}
        {result && (result.sensitivity || result.n_actual_water != null) ? (
          <Space wrap style={{ marginBottom: 12 }}>
            <Text type="secondary">
              主表 · 真实水位场次 {num(result.n_actual_water) ?? '—'} · 回落0.95场次{' '}
              {num(result.n_fallback_095) ?? '—'} · 回落比例{' '}
              {formatFallbackRate(result.fallback_rate)}
            </Text>
            {result.sensitivity ? (
              <Text type="secondary">
                灵敏度 · 真实水位场次{' '}
                {num(result.sensitivity.n_actual_water) ?? '—'} · 回落0.95场次{' '}
                {num(result.sensitivity.n_fallback_095) ?? '—'} · 回落比例{' '}
                {formatFallbackRate(result.sensitivity.fallback_rate)}
                {result.sensitivity.settlement_version
                  ? ` · ${labelSettlementVersion(result.sensitivity.settlement_version)}`
                  : ''}
              </Text>
            ) : null}
          </Space>
        ) : null}
        {sensFallbackHigh ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={FALLBACK_RATE_WARN_HINT}
            description="回落比例偏高，真实水位副表仅供同注单灵敏度参考，勿当作主对比结论。"
          />
        ) : null}
        {result?.stack?.conflict_policy ? (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="冲突处理"
            description={result.stack.conflict_policy}
          />
        ) : null}

        {!result ? (
          <div
            style={{
              minHeight: 280,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              border: '1px dashed var(--ant-color-border, #d9d9d9)',
              borderRadius: 8,
              background: 'var(--ant-color-fill-alter, #fafafa)',
            }}
          >
            <Empty
              description={
                selectedIds.length === 0
                  ? '先在上方多选方案，再点「开始对比」'
                  : `已选 ${selectedIds.length} 个方案，点击「开始对比」拉取序列`
              }
            />
          </div>
        ) : chartOption ? (
          <ReactECharts
            option={chartOption}
            style={{ height: 380, width: '100%' }}
            notMerge
            lazyUpdate
          />
        ) : (
          <Empty description="无可绘制序列（方案可能缺验证记录或方案不存在）" />
        )}
      </Card>

      {result ? (
        <Card title="对比摘要">
          <Table
            size="small"
            rowKey="key"
            columns={summaryColumns}
            dataSource={summaryRows}
            pagination={false}
            rowClassName={(r) => (r.kind === 'stack' ? 'compare-stack-row' : '')}
          />
        </Card>
      ) : null}

      {result && includeStack ? (
        <Card
          title="冲突场次"
          extra={
            result.stack?.summary &&
            num(result.stack.summary.conflict_count) != null ? (
              <Text type="secondary">
                冲突 {String(result.stack.summary.conflict_count)} · 双边下注{' '}
                {String(result.stack.summary.conflict_staked_both_count ?? '—')} · 冲突净盈亏{' '}
                {formatPnlAmount(result.stack.summary.conflict_net_pnl)}
              </Text>
            ) : null
          }
        >
          <Table
            size="small"
            rowKey="key"
            columns={conflictColumns}
            dataSource={conflictRows}
            pagination={false}
            locale={{
              emptyText: (
                <Empty
                  description={
                    result.stack
                      ? '当前组合无冲突场次'
                      : '未返回方案叠加（或已关闭）'
                  }
                />
              ),
            }}
          />
        </Card>
      ) : null}
    </Space>
  )
}
