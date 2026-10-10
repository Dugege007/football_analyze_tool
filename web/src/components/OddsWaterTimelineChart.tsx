import { HelpTip, LabelWithHelp } from './HelpTip'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Empty, Select, Slider, Space, Spin, Typography } from 'antd'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import {
  DEFAULT_LOG_TIME_COEF,
  DEFAULT_ODDS_TIMELINE_BOOK,
  LOG_TIME_COEF_MAX,
  LOG_TIME_COEF_MIN,
  ODDS_TIMELINE_BOOKS,
  OddsTimelineHttpError,
  fetchOddsTimeline,
  fetchPhaseAnchorsForBook,
  minutesToAxisX,
  timelineBookToAhKey,
  type OddsTimelineBook,
  type OddsTimelineResponse,
  type PhaseAnchorMark,
} from '../api/oddsTimeline'
import { useDbSource } from '../api/dataSource'

const { Text } = Typography

function formatLine(v: number): string {
  if (Object.is(v, -0) || v === 0) return '0'
  if (v > 0) return `+${v}`
  return String(v)
}

/** 盘口纵轴：按 0.25 取整包住数据 */
function lineYRange(values: number[]): { min: number; max: number } {
  if (!values.length) return { min: -0.5, max: 0.75 }
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const min = Math.floor(lo / 0.25) * 0.25 - 0.25
  const max = Math.ceil(hi / 0.25) * 0.25 + 0.25
  return { min, max: max <= min ? min + 0.5 : max }
}

/**
 * 水位纵轴：以 0.95 为中心，步长 0.05，对称包住数据；
 * 无数据时给 0.95 ± 3 档（0.80～1.10）。
 */
function waterYRange(values: number[]): { min: number; max: number } {
  const center = 0.95
  const step = 0.05
  if (!values.length) return { min: center - 3 * step, max: center + 3 * step }
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const downSteps = Math.max(1, Math.ceil((center - lo) / step))
  const upSteps = Math.max(1, Math.ceil((hi - center) / step))
  const span = Math.max(downSteps, upSteps)
  return {
    min: Number((center - span * step).toFixed(2)),
    max: Number((center + span * step).toFixed(2)),
  }
}

function roundNice(n: number): number {
  return Math.round(n * 1e9) / 1e9
}

function mapWithCoef(
  data: OddsTimelineResponse,
  anchors: PhaseAnchorMark[],
  coef: number,
) {
  const tickPts = data.ticks.map((t) => ({
    ...t,
    x: minutesToAxisX(t.tau_min, coef),
  }))
  const axisTicks = data.axis_ticks.map((t) => ({
    ...t,
    x: minutesToAxisX(t.tau_min === 0 ? 1 : t.tau_min, coef),
  }))
  const anchorPts = anchors.map((a) => ({
    ...a,
    x: minutesToAxisX(a.tau_min, coef),
  }))
  const xs = [...tickPts.map((t) => t.x), ...axisTicks.map((t) => t.x)]
  const xMin = xs.length ? Math.min(...xs) - 0.05 * coef : -3 * coef
  const xMax = xs.length ? Math.max(...xs) + 0.05 * coef : 0.05 * coef
  return { tickPts, axisTicks, anchorPts, xMin, xMax }
}

function buildSharedXAxis(
  axisTicks: Array<{ label: string; x: number }>,
  xMin: number,
  xMax: number,
  coef: number,
): EChartsOption['xAxis'] {
  const tickXs = axisTicks.map((t) => roundNice(t.x))
  const tickLabel = new Map(axisTicks.map((t) => [roundNice(t.x), t.label]))
  return {
    type: 'value',
    name: `距开赛（对数系数 ${coef}）`,
    nameLocation: 'middle',
    nameGap: 28,
    min: xMin,
    max: xMax,
    axisLabel: {
      customValues: tickXs,
      formatter: (v: number) => tickLabel.get(roundNice(v)) ?? '',
      hideOverlap: true,
      fontSize: 11,
    },
    axisTick: { customValues: tickXs },
    splitLine: { show: true, lineStyle: { type: 'dotted', opacity: 0.4 } },
  }
}

function lineChangeXs(
  ticks: Array<{ x: number; line: number | null }>,
): number[] {
  const sorted = [...ticks].sort((a, b) => a.x - b.x)
  const out: number[] = []
  for (let i = 1; i < sorted.length; i++) {
    const prev = sorted[i - 1].line
    const cur = sorted[i].line
    if (prev != null && cur != null && prev !== cur) out.push(sorted[i].x)
  }
  return out
}

function buildLineChartOption(
  data: OddsTimelineResponse,
  anchors: PhaseAnchorMark[],
  coef: number,
): EChartsOption {
  const { tickPts, axisTicks, anchorPts, xMin, xMax } = mapWithCoef(data, anchors, coef)
  const sorted = [...tickPts].sort((a, b) => a.x - b.x)
  const lineData = sorted
    .filter((t) => t.line != null)
    .map((t) => [t.x, t.line!] as [number, number])
  const { min: yMin, max: yMax } = lineYRange(lineData.map((d) => d[1]))

  const changeXs = lineChangeXs(sorted)
  const markLineData = changeXs.map((x) => ({
    xAxis: x,
    label: { show: false },
    lineStyle: { type: 'dashed' as const, color: '#8c8c8c', width: 1 },
  }))

  const markPointData = anchorPts
    .filter((a) => a.line != null)
    .map((a) => ({
      name: a.label,
      coord: [a.x, a.line!] as [number, number],
      value: a.label,
      symbol: 'pin',
      symbolSize: 40,
      itemStyle: {
        color: a.phase === 'open' ? '#1677ff' : a.phase === 'mid' ? '#fa8c16' : '#52c41a',
      },
      label: { show: true, formatter: '{b}', color: '#fff', fontSize: 11 },
    }))

  const anchorLines = anchorPts
    .filter((a) => a.line == null)
    .map((a) => ({
      xAxis: a.x,
      label: { show: true, formatter: a.label, position: 'insideEndTop' as const },
      lineStyle: { type: 'dotted' as const, color: '#595959', width: 1 },
    }))

  return {
    animation: false,
    title: { text: '盘口（主队视角）', left: 0, textStyle: { fontSize: 13, fontWeight: 500 } },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: (params: unknown) => {
        const list = Array.isArray(params) ? params : [params]
        const x = (list[0] as { value?: [number, number] })?.value?.[0]
        if (typeof x !== 'number') return ''
        const nearest = sorted.reduce((best, t) =>
          Math.abs(t.x - x) < Math.abs(best.x - x) ? t : best,
        )
        const tau = nearest.tau_min
        const tauText =
          tau >= 60 ? `约还剩 ${(tau / 60).toFixed(1)} 小时` : `约还剩 ${Math.max(1, Math.round(tau))} 分钟`
        return [
          `<div><b>${tauText}</b></div>`,
          nearest.line != null ? `<div>盘口 ${formatLine(nearest.line)}</div>` : '',
          `<div style="color:#888">${nearest.recorded_at}</div>`,
        ]
          .filter(Boolean)
          .join('')
      },
    },
    grid: { left: 56, right: 24, top: 40, bottom: 48 },
    xAxis: buildSharedXAxis(axisTicks, xMin, xMax, coef),
    yAxis: {
      type: 'value',
      name: '盘口',
      min: yMin,
      max: yMax,
      interval: 0.25,
      axisLabel: { formatter: (v: number) => formatLine(Number(v)) },
    },
    series: [
      {
        name: '亚盘盘口',
        type: 'line',
        step: 'end',
        showSymbol: true,
        symbolSize: 6,
        data: lineData,
        lineStyle: { width: 2, color: '#1677ff' },
        itemStyle: { color: '#1677ff' },
        markLine: {
          symbol: 'none',
          silent: true,
          data: [...markLineData, ...anchorLines],
          emphasis: { disabled: true },
        },
        markPoint: { data: markPointData },
      },
    ],
  }
}

function buildWaterChartOption(
  data: OddsTimelineResponse,
  anchors: PhaseAnchorMark[],
  coef: number,
): EChartsOption {
  const { tickPts, axisTicks, anchorPts, xMin, xMax } = mapWithCoef(data, anchors, coef)
  const sorted = [...tickPts].sort((a, b) => a.x - b.x)
  const homeData = sorted
    .filter((t) => t.home_water != null)
    .map((t) => [t.x, t.home_water!] as [number, number])
  const awayData = sorted
    .filter((t) => t.away_water != null)
    .map((t) => [t.x, t.away_water!] as [number, number])
  const { min: yMin, max: yMax } = waterYRange([...homeData, ...awayData].map((d) => d[1]))

  const changeXs = lineChangeXs(sorted)
  const markLineData = changeXs.map((x) => ({
    xAxis: x,
    label: { show: false },
    lineStyle: { type: 'dashed' as const, color: '#8c8c8c', width: 1 },
  }))

  const markPointData = anchorPts.flatMap((a) => {
    const pts: Array<{
      name: string
      coord: [number, number]
      symbol: string
      symbolSize: number
      itemStyle: { color: string }
      label: { show: boolean; formatter: string; color: string; fontSize: number }
    }> = []
    const color = a.phase === 'open' ? '#1677ff' : a.phase === 'mid' ? '#fa8c16' : '#52c41a'
    if (a.home_water != null) {
      pts.push({
        name: `${a.label}·主`,
        coord: [a.x, a.home_water],
        symbol: 'pin',
        symbolSize: 36,
        itemStyle: { color },
        label: { show: true, formatter: a.label, color: '#fff', fontSize: 10 },
      })
    }
    return pts
  })

  return {
    animation: false,
    title: { text: '主／客水位', left: 0, textStyle: { fontSize: 13, fontWeight: 500 } },
    legend: { data: ['主队水位', '客队水位'], top: 0, right: 0 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: (params: unknown) => {
        const list = Array.isArray(params) ? params : [params]
        const x = (list[0] as { value?: [number, number] })?.value?.[0]
        if (typeof x !== 'number') return ''
        const nearest = sorted.reduce((best, t) =>
          Math.abs(t.x - x) < Math.abs(best.x - x) ? t : best,
        )
        return [
          nearest.home_water != null ? `<div>主队水位 ${nearest.home_water}</div>` : '',
          nearest.away_water != null ? `<div>客队水位 ${nearest.away_water}</div>` : '',
          `<div style="color:#888">${nearest.recorded_at}</div>`,
        ]
          .filter(Boolean)
          .join('')
      },
    },
    grid: { left: 56, right: 24, top: 40, bottom: 48 },
    xAxis: buildSharedXAxis(axisTicks, xMin, xMax, coef),
    yAxis: {
      type: 'value',
      name: '水位',
      min: yMin,
      max: yMax,
      interval: 0.05,
      axisLabel: {
        formatter: (v: number) => Number(v).toFixed(2),
      },
    },
    series: [
      {
        name: '主队水位',
        type: 'line',
        showSymbol: true,
        symbolSize: 5,
        data: homeData,
        lineStyle: { width: 1.5, color: '#fa541c' },
        itemStyle: { color: '#fa541c' },
        markLine: {
          symbol: 'none',
          silent: true,
          data: markLineData,
          emphasis: { disabled: true },
        },
        markPoint: { data: markPointData },
      },
      {
        name: '客队水位',
        type: 'line',
        showSymbol: true,
        symbolSize: 5,
        data: awayData,
        lineStyle: { width: 1.5, color: '#13c2c2' },
        itemStyle: { color: '#13c2c2' },
      },
    ],
  }
}

export interface OddsWaterTimelineChartProps {
  matchId: string
  /** 开赛时间（详情页已有时可传入，锚点计算回退用） */
  kickoffAt?: string | null
  defaultBook?: OddsTimelineBook
  /** 是否处于展开可见状态；为 false 时不请求 */
  active?: boolean
  heightPerChart?: number
}

/**
 * 赛前盘口／水位折线（真接口）。
 * 上图盘口阶梯（纵轴步长 0.25）、下图主／客水位（纵轴步长 0.05、以 0.95 为中心）；
 * 横轴 x = −coef × log10(max(τ,1))，coef 默认 5、可调 1～10，滑动即时重算、不重新请求；
 * 初／中／临锚点与当前 book 同路（澳门叠 ah.macau_5df；平博叠 ah.pinnacle）。
 */
export default function OddsWaterTimelineChart({
  matchId,
  kickoffAt,
  defaultBook = DEFAULT_ODDS_TIMELINE_BOOK,
  active = true,
  heightPerChart = 260,
}: OddsWaterTimelineChartProps) {
  const [dbSource] = useDbSource()
  const [book, setBook] = useState<OddsTimelineBook>(defaultBook)
  const [logCoef, setLogCoef] = useState(DEFAULT_LOG_TIME_COEF)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<OddsTimelineHttpError | Error | null>(null)
  const [data, setData] = useState<OddsTimelineResponse | null>(null)
  const [anchors, setAnchors] = useState<PhaseAnchorMark[]>([])
  const [reloadKey, setReloadKey] = useState(0)

  const load = useCallback(() => {
    if (!active || !matchId) return
    let cancelled = false
    setLoading(true)
    setError(null)
    ;(async () => {
      try {
        const res = await fetchOddsTimeline(matchId, book, { src: dbSource })
        if (cancelled) return
        setData(res)
        const kick = res.kickoff_at || kickoffAt || ''
        const marks = kick
          ? await fetchPhaseAnchorsForBook(matchId, res.book || book, kick, dbSource)
          : []
        if (!cancelled) setAnchors(marks)
      } catch (e) {
        if (cancelled) return
        setData(null)
        setAnchors([])
        setError(e instanceof Error ? e : new Error(String(e)))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [active, matchId, book, dbSource, kickoffAt, reloadKey])

  useEffect(() => {
    const cleanup = load()
    return cleanup
  }, [load])

  const lineOption = useMemo(
    () => (data && data.ticks.length ? buildLineChartOption(data, anchors, logCoef) : null),
    [data, anchors, logCoef],
  )
  const waterOption = useMemo(
    () => (data && data.ticks.length ? buildWaterChartOption(data, anchors, logCoef) : null),
    [data, anchors, logCoef],
  )

  const busy =
    error instanceof OddsTimelineHttpError &&
    error.status === 503 &&
    (error.code === 'live_capture_busy' || error.code == null)
  const ahKey = timelineBookToAhKey(book)

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <span style={{ flex: 1, minWidth: 240 }}>
          <LabelWithHelp
            label="公司"
            tip={'展开本区块后才会请求变盘历史。默认公司为平博亚盘，可以切换澳门、皇冠、威廉希尔、平博、Bet365。\n初盘、中盘、临盘锚点与当前公司来自同一数据路径：澳门叠加接口抓取的澳门数据，不叠加手工记录的澳门数据；平博叠加平博数据。'}
          />
        </span>
        <Select
          size="small"
          value={book}
          style={{ minWidth: 140 }}
          options={ODDS_TIMELINE_BOOKS.map((b) => ({ value: b.value, label: b.label }))}
          onChange={(v) => setBook(v)}
        />
      </div>

      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4, gap: 8 }}>
          <Text>
            对数时间轴系数（当前 {logCoef}）
            <HelpTip
              tip={`横轴按公式「x 等于负的系数乘以 log10（距开赛剩余分钟与 1 的较大值）」绘制；调节滑动条只重新计算坐标，不会重新请求接口。\n系数越大，曲线越向开赛端压缩。范围 ${LOG_TIME_COEF_MIN} 到 ${LOG_TIME_COEF_MAX}，默认 ${DEFAULT_LOG_TIME_COEF}。`}
            />
          </Text>
        </div>
        <Slider
          min={LOG_TIME_COEF_MIN}
          max={LOG_TIME_COEF_MAX}
          step={1}
          value={logCoef}
          marks={{
            1: '1',
            5: '5',
            10: '10',
          }}
          onChange={(v) => setLogCoef(typeof v === 'number' ? v : DEFAULT_LOG_TIME_COEF)}
          tooltip={{ formatter: (v) => `系数 ${v}` }}
        />
      </div>

      {loading ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin tip="正在加载赛前盘口／水位折线…" />
        </div>
      ) : error ? (
        <Alert
          type={busy ? 'warning' : 'error'}
          showIcon
          message={
            busy
              ? '今日采集忙，请稍后'
              : error instanceof OddsTimelineHttpError && error.status === 422
                ? '开赛时间未知'
                : '赛前折线加载失败'
          }
          description={
            <Space direction="vertical" size={8}>
              <span>{error.message}</span>
              {error instanceof OddsTimelineHttpError && error.retryAfterSec != null ? (
                <Text type="secondary">建议约 {error.retryAfterSec} 秒后再试。</Text>
              ) : null}
              <Button size="small" type="primary" onClick={() => setReloadKey((k) => k + 1)}>
                稍后重试
              </Button>
            </Space>
          }
        />
      ) : !data ? (
        <Empty description="暂无盘口／水位时序" />
      ) : data.ticks.length === 0 ? (
        <Alert
          type="info"
          showIcon
          message="暂无变盘历史"
          description={
            (data.notes && data.notes.length ? data.notes.join('；') : null) ||
            '上游无赛前变盘记录，不绘制假线。'
          }
        />
      ) : (
        <>
          <Space wrap size="small">
            {data.cached ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                缓存
                {data.cache_expires_at ? `（约至 ${data.cache_expires_at}）` : ''}
              </Text>
            ) : null}
            {data.book_label ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                机构 {data.book_label}（{data.book}）· 玩法 {data.market}
              </Text>
            ) : null}
            {anchors.length ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                已叠加锚点 {anchors.map((a) => a.label).join('／')}
                <HelpTip tip={`锚点数据路径：ah.${anchors[0].ah_key}`} />
              </Text>
            ) : (
              <Text type="secondary" style={{ fontSize: 12 }}>
                锚点：-
                <HelpTip tip={`当前公司没有同一数据路径的初盘、中盘、临盘快照可以叠加（数据路径 ah.${ahKey}）。`} />
              </Text>
            )}
            {data.notes?.length ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {data.notes.join('；')}
              </Text>
            ) : null}
          </Space>
          {lineOption ? (
            <ReactECharts option={lineOption} style={{ height: heightPerChart, width: '100%' }} notMerge lazyUpdate />
          ) : null}
          {waterOption ? (
            <ReactECharts option={waterOption} style={{ height: heightPerChart, width: '100%' }} notMerge lazyUpdate />
          ) : null}
        </>
      )}
    </Space>
  )
}
