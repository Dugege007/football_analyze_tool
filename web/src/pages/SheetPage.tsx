import { HelpTip, LabelWithHelp } from '../components/HelpTip'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Divider,
  Dropdown,
  Input,
  Popover,
  Segmented,
  Select,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
  message,
  theme as antdTheme,
} from 'antd'
import {
  BgColorsOutlined,
  CopyOutlined,
  DownloadOutlined,
  ReloadOutlined,
  UndoOutlined,
} from '@ant-design/icons'
import dayjs, { type Dayjs } from 'dayjs'
import {
  CellStyleModule,
  ClientSideRowModelApiModule,
  ClientSideRowModelModule,
  ColumnApiModule,
  ColumnAutoSizeModule,
  CsvExportModule,
  EventApiModule,
  ExternalFilterModule,
  LocaleModule,
  ModuleRegistry,
  NumberFilterModule,
  QuickFilterModule,
  RenderApiModule,
  RowApiModule,
  RowSelectionModule,
  RowStyleModule,
  TextFilterModule,
  TooltipModule,
  ValidationModule,
  themeQuartz,
  type CellContextMenuEvent,
  type ColumnState,
  type GridApi,
  type IRowNode,
  type ProcessHeaderForExportParams,
} from 'ag-grid-community'
import { AgGridReact } from 'ag-grid-react'
import { AG_GRID_LOCALE_CN } from '@ag-grid-community/locale'
import { DEFAULT_STRATEGY } from '../api/strategies'
import { loadSheet } from '../sheet/adapter'
import { useDbSource, type DbMeta } from '../api/dataSource'
import { DataSourceSwitch, ReplicaBanner } from '../api/DataSourceBar'
import { COL_META, buildColumnDefs, computePresence } from '../sheet/columns'
import {
  CONFIG_VERSION,
  FILL_CLASS,
  RULES,
  countRuleHits,
  defaultEnabled,
  ruleAvailable,
  type RuleContext,
  type RuleHitCount,
} from '../sheet/rules'
import {
  AH_BOOKS,
  BOOK_LABEL,
  DAILY_CHECK_LABEL,
  EXCEPTION_DESC,
  SETTLE_LABEL,
  VIEW_LABEL,
  type AhBook,
  type DailyCheckSummary,
  type IncludeLive,
  type SettleCode,
  type SheetRow,
  type ViewId,
} from '../sheet/types'
import '../sheet/sheet.css'

ModuleRegistry.registerModules([
  ClientSideRowModelModule,
  ClientSideRowModelApiModule,
  RenderApiModule,
  CellStyleModule,
  RowStyleModule,
  ColumnApiModule,
  ColumnAutoSizeModule,
  TextFilterModule,
  NumberFilterModule,
  QuickFilterModule,
  ExternalFilterModule,
  CsvExportModule,
  TooltipModule,
  LocaleModule,
  RowSelectionModule,
  RowApiModule,
  EventApiModule,
  ...(import.meta.env.DEV ? [ValidationModule] : []),
])

const { Title, Text } = Typography
const { RangePicker } = DatePicker

// .r2：返还率/凯利改为「有数据即激活」，旧存档里的 false 不沿用
// .r3（hl_v0.2）：新增「档位换算也上色（仅排查用）」开关（键 RR_TIER_DEBUG），默认关
// hl_v0.3：键随 CONFIG_VERSION 变成 sheet.rules.hl_v0.3.r3（旧 hl_v0.2 存档不沿用）
const LS_RULES = `sheet.rules.${CONFIG_VERSION}.r3`
/** hl_v0.2 排查开关在 enabled 里的键（不是 RULES 里的规则） */
const RR_TIER_DEBUG = 'rr_tier_debug'
/** v5：只持久化顺序/固定/排序，不存 width；亚盘盘口/水位夹紧 */
const lsCols = (view: ViewId) => `sheet.cols.v5.${view}`

type SettleFilter = SettleCode | 'pending'
const SETTLE_OPTIONS: { value: SettleFilter; label: string }[] = [
  ...(Object.keys(SETTLE_LABEL) as SettleCode[]).map((k) => ({ value: k, label: SETTLE_LABEL[k] })),
  { value: 'pending', label: '未结算' },
]

function readJson<T>(key: string, fallback: T): T {
  try {
    const s = localStorage.getItem(key)
    return s ? (JSON.parse(s) as T) : fallback
  } catch {
    return fallback
  }
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  }
}

/** 数据截至：ISO（带时区）→ 北京时间 YYYY-MM-DD HH:mm */
function fmtAsOf(s: string): string {
  const d = dayjs(s)
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm') : s
}

interface Stats {
  total: number
  shown: number
  settle: Record<SettleFilter, number>
  pnl: number
  hits: Record<string, RuleHitCount>
}

/** 推迟作废没有赛果也算已结算（显示「推迟作废」） */
const settledCode = (r: SheetRow): SettleCode | null =>
  r.settleCode && (r.finished || r.settleCode === 'void_postponed') ? r.settleCode : null

export default function SheetPage() {
  const navigate = useNavigate()
  const { token } = antdTheme.useToken()
  const gridRef = useRef<AgGridReact<SheetRow>>(null)

  const [range, setRange] = useState<[Dayjs, Dayjs]>([dayjs('2026-06-01'), dayjs('2026-06-30')])
  const [scope, setScope] = useState<'jingcai' | 'extra' | 'all'>('jingcai')
  const [view, setView] = useState<ViewId>('snapshot')
  const [quick, setQuick] = useState('')
  const [leagues, setLeagues] = useState<string[]>([])
  const [books, setBooks] = useState<AhBook[]>([...AH_BOOKS])
  const [settles, setSettles] = useState<SettleFilter[]>([])
  const [enabled, setEnabled] = useState<Record<string, boolean>>(() => ({
    ...defaultEnabled(),
    [RR_TIER_DEBUG]: false,
    ...readJson<Record<string, boolean>>(LS_RULES, {}),
  }))

  const [rows, setRows] = useState<SheetRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [asOf, setAsOf] = useState<string | null>(null)
  const [backendCfg, setBackendCfg] = useState<string | null>(null)
  const [settlementVersion, setSettlementVersion] = useState<string | null>(null)
  // 0.3.18：数据源（现网 / 副本），默认现网，存本地；是否副本以响应 meta 为准
  const [dbSource, setDbSource] = useDbSource()
  const [dbMeta, setDbMeta] = useState<DbMeta | null>(null)
  // 0.3.19：响应顶层 daily_check_summary（本页待核对计数）
  const [dailyCheck, setDailyCheck] = useState<DailyCheckSummary | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [reloadTick, setReloadTick] = useState(0)
  const [showAllLive, setShowAllLive] = useState<boolean>(() => readJson<boolean>('sheet.showAllLive', false))
  const [showReal, setShowReal] = useState<boolean>(() => readJson<boolean>('sheet.showReal', false))
  const lastKey = useRef<string>('')
  const [menu, setMenu] = useState<{ x: number; y: number; row: SheetRow } | null>(null)
  const [selectedCount, setSelectedCount] = useState(0)

  // ── 取数 ──
  // 只走批量接口；即时快照 live[] 只在「盘口快照」视图请求
  const includeLive: IncludeLive =
    view !== 'snapshot' ? 'none' : showAllLive ? 'all' : 'none'
  useEffect(() => {
    const key = `${dbSource}|${range[0].format('YYYY-MM-DD')}|${range[1].format('YYYY-MM-DD')}|${scope}|${includeLive}`
    if (key === lastKey.current) return
    let cancelled = false
    setLoading(true)
    setError(null)
    loadSheet({
      dateFrom: range[0].format('YYYY-MM-DD'),
      dateTo: range[1].format('YYYY-MM-DD'),
      scope,
      strategy: DEFAULT_STRATEGY,
      includeLive,
      source: dbSource,
    })
      .then((res) => {
        if (cancelled) return
        lastKey.current = key
        setRows(res.rows)
        setAsOf(res.asOf)
        setBackendCfg(res.backendConfigVersion)
        setSettlementVersion(res.settlementVersion)
        setDbMeta(res.meta)
        setDailyCheck(res.dailyCheckSummary)
      })
      .catch((e: Error) => {
        if (!cancelled) {
          lastKey.current = ''
          setError(e.message)
          setRows([])
          setDbMeta(null)
          setDailyCheck(null)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [range, scope, includeLive, reloadTick, dbSource])

  const presence = useMemo(() => computePresence(rows), [rows])
  const columnDefs = useMemo(
    () => buildColumnDefs({ view, presence, books, showReal }),
    [view, presence, books, showReal],
  )
  useEffect(() => {
    localStorage.setItem('sheet.showAllLive', JSON.stringify(showAllLive))
    localStorage.setItem('sheet.showReal', JSON.stringify(showReal))
  }, [showAllLive, showReal])
  // 返还率 / 凯利规则：后端有值才激活（无数据继续灰着）
  const dataAvailable = useMemo<Record<string, boolean>>(
    () => ({
      ah_return_rate: AH_BOOKS.some((b) => presence.ahRr[b]),
      x1x2_return_rate: AH_BOOKS.some((b) => presence.x1x2Rr[b]),
      kelly: AH_BOOKS.some((b) => presence.kelly[b]),
    }),
    [presence],
  )
  const debugTier = !!enabled[RR_TIER_DEBUG]
  // 排查开关作用于返还率（亚盘/欧赔）和 §2 水位异动；水位异动规则恒可用，所以开关恒可点
  const ruleCtx = useMemo<RuleContext>(
    () => ({ view, enabled, dataAvailable, debugTier }),
    [view, enabled, dataAvailable, debugTier],
  )
  const reload = () => {
    lastKey.current = ''
    setReloadTick((t) => t + 1)
  }
  const exceptionCount = useMemo(() => rows.filter((r) => r.phaseException).length, [rows])
  const leagueOptions = useMemo(
    () => [...new Set(rows.map((r) => r.league))].sort().map((l) => ({ value: l, label: l })),
    [rows],
  )

  const gridTheme = useMemo(
    () =>
      themeQuartz.withParams({
        fontFamily: token.fontFamily,
        fontSize: 13,
        headerFontSize: 13,
        headerFontWeight: 600,
        accentColor: token.colorPrimary,
        foregroundColor: token.colorText,
        headerBackgroundColor: token.colorFillAlter,
        borderColor: token.colorBorderSecondary,
        rowHoverColor: token.colorPrimaryBg,
        selectedRowBackgroundColor: token.colorPrimaryBgHover,
        wrapperBorderRadius: token.borderRadiusLG,
        spacing: 5,
        rowHeight: 32,
        headerHeight: 34,
        cellHorizontalPadding: 8,
      }),
    [token],
  )

  // ── 统计（筛选后）──
  const recomputeStats = useCallback(
    (api: GridApi<SheetRow>) => {
      const shownRows: SheetRow[] = []
      api.forEachNodeAfterFilter((n: IRowNode<SheetRow>) => {
        if (n.data) shownRows.push(n.data)
      })
      const settle = Object.fromEntries(SETTLE_OPTIONS.map((o) => [o.value, 0])) as Record<SettleFilter, number>
      let pnl = 0
      for (const r of shownRows) {
        const sc = settledCode(r)
        if (sc) settle[sc]++
        else settle.pending++
        if (r.finished && r.pnlUnits != null) pnl += r.pnlUnits
      }
      setStats({
        total: rows.length,
        shown: shownRows.length,
        settle,
        pnl,
        // 只统计当前可见列（折叠分组里的列不算）
        hits: countRuleHits(
          shownRows,
          api
            .getAllDisplayedColumns()
            .map((c) => COL_META.get(c.getColId()))
            .filter((m): m is NonNullable<typeof m> => m != null),
          ruleCtx,
        ),
      })
    },
    [rows.length, ruleCtx],
  )

  // ── 规则开关 → 重新着色 ──
  useEffect(() => {
    localStorage.setItem(LS_RULES, JSON.stringify(enabled))
    const api = gridRef.current?.api
    if (api) {
      api.refreshCells({ force: true })
      recomputeStats(api)
    }
  }, [enabled, view, recomputeStats])

  // ── 外部筛选 ──
  const filterRef = useRef({ leagues, settles })
  useEffect(() => {
    filterRef.current = { leagues, settles }
    gridRef.current?.api?.onFilterChanged()
  }, [leagues, settles])
  const isExternalFilterPresent = useCallback(
    () => filterRef.current.leagues.length > 0 || filterRef.current.settles.length > 0,
    [],
  )
  const doesExternalFilterPass = useCallback((node: IRowNode<SheetRow>) => {
    const r = node.data
    if (!r) return false
    const { leagues: ls, settles: ss } = filterRef.current
    if (ls.length && !ls.includes(r.league)) return false
    if (ss.length) {
      const k: SettleFilter = settledCode(r) ?? 'pending'
      if (!ss.includes(k)) return false
    }
    return true
  }, [])

  // ── 列状态持久化（顺序/固定/排序；不存 width。显示与否由视图和公司筛选决定）──
  const restoringRef = useRef(false)
  const autosizingRef = useRef(false)
  /** 按内容自适应；亚盘盘口/水位 skipHeader 后再夹 maxWidth（不用 sizeColumnsToFit） */
  const autoSizeColumns = useCallback((api: GridApi<SheetRow> | undefined | null) => {
    if (!api) return
    autosizingRef.current = true
    try {
      api.autoSizeAllColumns(false)
      const tight =
        api
          .getColumns()
          ?.map((c) => c.getColId())
          .filter((id) => /\.(line|hw|aw)$/.test(id) && id.startsWith('ah.')) ?? []
      if (tight.length) {
        api.autoSizeColumns(tight, true)
        api.applyColumnState({
          state: api.getColumnState().map((s) => {
            const id = s.colId ?? ''
            if (!tight.includes(id)) return s
            const isLine = id.endsWith('.line')
            const max = isLine ? 88 : 64
            const min = isLine ? 56 : 48
            return { ...s, width: Math.min(max, Math.max(min, s.width ?? min)) }
          }),
          applyOrder: false,
        })
      }
    } finally {
      requestAnimationFrame(() => {
        autosizingRef.current = false
      })
    }
  }, [])
  const restoreColumns = useCallback((api: GridApi<SheetRow>, v: ViewId) => {
    const saved = readJson<ColumnState[] | null>(lsCols(v), null)
    if (!saved) return
    restoringRef.current = true
    // 丢弃旧 width，避免锁死；hide 仍由视图/筛选管
    // marryChildren 分组不可打乱顺序；列序以 columnDefs（主水→盘口→客水）为准，只恢复 pin/sort 等
    api.applyColumnState({
      state: saved.map(({ hide: _hide, width: _w, ...s }) => s),
      applyOrder: false,
    })
    restoringRef.current = false
  }, [])
  const saveColumns = useCallback(() => {
    const api = gridRef.current?.api
    if (!api || restoringRef.current || autosizingRef.current) return
    // 只持久化顺序/固定/排序等，不写 width（内容自适应优先）
    const slim = api.getColumnState().map(({ width: _w, ...s }) => s)
    localStorage.setItem(lsCols(view), JSON.stringify(slim))
  }, [view])
  useEffect(() => {
    const api = gridRef.current?.api
    if (!api) return
    restoreColumns(api, view)
    // 列定义变化后按内容重算宽
    autoSizeColumns(api)
  }, [view, columnDefs, restoreColumns, autoSizeColumns])

  // 行数据 / 数据源变化后也要再 autoSize（首次渲染见 onFirstDataRendered）
  useEffect(() => {
    const api = gridRef.current?.api
    if (!api || loading) return
    // 等 AG Grid 吃完 rowData 再量宽
    const t = window.setTimeout(() => autoSizeColumns(api), 0)
    return () => window.clearTimeout(t)
  }, [rows, dbSource, loading, autoSizeColumns])

  const resetColumns = () => {
    localStorage.removeItem(lsCols(view))
    const api = gridRef.current?.api
    api?.resetColumnState()
    autoSizeColumns(api)
    message.success('已重置列')
  }

  // ── 复制 / 导出 ──
  // 表头带上分组名（如「澳门亚盘·初盘」），避免多家公司的同名列分不清
  const headerWithGroup = (p: ProcessHeaderForExportParams<SheetRow>) => {
    const name = p.column.getColDef().headerName ?? p.column.getColId()
    const group = p.column.getParent()?.getColGroupDef()?.headerName
    return group ? `${group}·${name}` : name
  }

  const tsvOf = (api: GridApi<SheetRow>, onlyIds?: Set<string>) =>
    api.getDataAsCsv({
      columnSeparator: '\t',
      suppressQuotes: true,
      skipColumnGroupHeaders: true,
      processHeaderCallback: headerWithGroup,
      shouldRowBeSkipped: onlyIds ? (p) => !p.node.data || !onlyIds.has(p.node.data.id) : undefined,
    }) ?? ''

  const copyRows = async (ids: Set<string>) => {
    const api = gridRef.current?.api
    if (!api || !ids.size) return
    const ok = await copyText(tsvOf(api, ids))
    if (ok) message.success(`已复制 ${ids.size} 行（制表符分隔，可直接粘贴到表格软件）`)
    else message.error('复制失败：浏览器不允许访问剪贴板')
  }

  const copySelected = () => {
    const api = gridRef.current?.api
    if (!api) return
    const ids = new Set(api.getSelectedRows().map((r) => r.id))
    if (!ids.size) {
      message.info('先勾选要复制的行')
      return
    }
    void copyRows(ids)
  }

  // EXPORT-NOTE(hl_v0.2)：CSV 只导数值、不带颜色。将来 exceljs 带样式导出时，取色必须用 rules.ts 的
  // exportableHits()（去掉排查色 debug 与只提示的命中），排查模式打开时也不能把排查色写进文件。
  const exportCsv = () => {
    gridRef.current?.api?.exportDataAsCsv({
      fileName: `数据表_${VIEW_LABEL[view]}_${range[0].format('YYYYMMDD')}-${range[1].format('YYYYMMDD')}.csv`,
      skipColumnGroupHeaders: true,
      processHeaderCallback: headerWithGroup,
    })
  }

  const onCellContextMenu = (e: CellContextMenuEvent<SheetRow>) => {
    const me = e.event as MouseEvent | null
    if (!e.data || !me) return
    setMenu({ x: me.clientX, y: me.clientY, row: e.data })
  }

  // ── hl_v0.2 排查开关：档位换算的返还率格、水位异动格也按原阈值上色（只在页面显示，带排查标记，不进导出）──
  const rrDebugSwitch = (
    <div style={{ marginTop: 8, padding: '6px 8px', background: '#fff7e6', borderRadius: 6 }}>
      <Space size={6} wrap>
        <Switch
          size="small"
          checked={debugTier}
          onChange={(v) => setEnabled((prev) => ({ ...prev, [RR_TIER_DEBUG]: v }))}
        />
        <Text strong>档位换算也上色</Text>
        <HelpTip tip="只用于排查，默认关闭。打开后，档位中点换算的返还率格（亚盘、欧赔）和水位异动格按原阈值上色，并加红色虚线框和「排查」角标；这些颜色不进导出，也不进方案计算。生效的视图与各自规则相同。" />
        <span className="sheet-legend-swatch sheet-legend-debug-mark">轻</span>
      </Space>
    </div>
  )

  // ── 图例 + 规则开关 ──
  const legend = (
    <div style={{ width: 520, maxHeight: 560, overflow: 'auto' }}>
      <Space style={{ marginBottom: 8 }} wrap>
        <Tag color="blue">口径版本 {CONFIG_VERSION}</Tag>
        <HelpTip tip={`只有在视图「${VIEW_LABEL[view]}」里生效的规则才会着色。每条规则右边的数字是当前筛选后、可见列里命中的格子数和场次数。`} />
      </Space>
      <Space orientation="vertical" size={2} style={{ marginBottom: 8, fontSize: 12 }}>
        <Space size={6} wrap>
          <span className="sheet-legend-swatch" style={{ color: '#cf1322', fontWeight: 600 }}>↑升盘</span>
          <span className="sheet-legend-swatch" style={{ color: '#389e0d', fontWeight: 600 }}>↓降盘</span>
          <span className="sheet-legend-swatch" style={{ background: '#fff1b8' }}>黄 · 轻</span>
          <span className="sheet-legend-swatch" style={{ background: '#ffd591' }}>橙 · 中</span>
          <span className="sheet-legend-swatch" style={{ background: '#ffa39e' }}>红 · 严重</span>
          <span className="sheet-legend-swatch" style={{ color: '#8c8c8c', fontStyle: 'italic' }}>灰斜体 · 近似/缺</span>
          <span className="sheet-legend-swatch" style={{ color: '#8c8c8c' }}>0.904 · 档位换算返还率</span>
          <Tooltip title="返还率不低于后端兜底线 P90：中性色，不算风险，不进导出，不计入风险统计">
            <span className="sheet-legend-swatch" style={{ background: '#e6f0fa' }}>浅灰蓝 · 返还率偏高</span>
          </Tooltip>
          <Tooltip title="后端兜底样本不足 100 或不满足条件：不上色">
            <span className="sheet-legend-swatch" style={{ color: '#8c8c8c' }}>0.931 · 样本不足，暂不判断</span>
          </Tooltip>
        </Space>
        <Space size={6} wrap>
          {(['win', 'win_half', 'push', 'lose_half', 'lose', 'no_bet', 'void_postponed'] as SettleCode[]).map((c) => (
            <span key={c} className={`sheet-legend-swatch ag-cell hl-settle-${c}`} style={{ position: 'static' }}>
              {SETTLE_LABEL[c]}
            </span>
          ))}
          <span className="sheet-legend-swatch">🔒 已冻结</span>
        </Space>
        <Space size={6} wrap>
          <span className="sheet-legend-swatch sheet-legend-rule-mark">+0.5</span>
          <HelpTip tip={`格子右上角的「规」：${EXCEPTION_DESC}；不影响红绿字和填充色。`} />
          <span className="sheet-legend-swatch sheet-legend-api-mark">+0.5</span>
          <HelpTip tip="格子右上角的「API」：初盘来自接口的开盘价，开盘时间未知。「规」和「API」都有时，「规」在前。" />
          <span className="sheet-legend-swatch">+0.5 (0.80/1.00)</span>
          <HelpTip tip="盘口格写作「数字盘口 (主水/客水)」。主队让球为正：+0.5 表示主队让半球，-0.5 表示主队受让半球。皇冠和威廉希尔的水位在现网大多是档位中点换算的近似值；水位异动高亮只给真实水位上色，档位换算的水位默认不上色，可以用排查开关临时上色。" />
          <span className="sheet-legend-swatch" style={{ color: '#8c8c8c' }}>+0.5</span>
          <HelpTip tip="灰字盘口表示这一格没有水位：接口返回了主水和客水时会显示在盘口后面，没有时悬停显示「暂无水位」。澳门没有水位时可以回落到 0.95。" />
          <span className="sheet-legend-swatch sheet-legend-check-mark">10:00</span>
          <HelpTip tip="格子右上角灰色的「核」表示待核对：开赛时间格是竞彩官方时刻与开赛时间相差超过 90 分钟。只做提示，不改颜色。" />
          <span className="sheet-legend-swatch" style={{ color: '#8c8c8c', fontStyle: 'italic' }}>待归阶段</span>
          <HelpTip tip="开赛时间还没确认时显示灰字「待归阶段」；依赖开赛时间的中盘和临盘格，悬停会提示「按占位开赛时间推算，不参与特征计算」等说明。" />
        </Space>
      </Space>
      <Space orientation="vertical" size={4} style={{ fontSize: 12 }}>
        <Space size={6}>
          <Switch size="small" checked={showAllLive} disabled={view !== 'snapshot'} onChange={setShowAllLive} />
          <span>显示「即时（最新）」列</span>
          <HelpTip tip="只在盘口快照视图可用。会请求全部即时快照，加载较慢。" />
        </Space>
      </Space>
      <Divider style={{ margin: '8px 0' }} />
      {RULES.map((r) => {
        const inView = r.views.includes(view)
        const hit = stats?.hits[r.id]
        const swatch =
          r.style.kind === 'fill'
            ? r.style.levels.map((l) => (
                <span
                  key={l}
                  className="sheet-legend-swatch"
                  style={{ background: { 1: '#fff1b8', 2: '#ffd591', 3: '#ffa39e' }[l], minWidth: 18 }}
                  title={FILL_CLASS[l]}
                >
                  {{ 1: '轻', 2: '中', 3: '重' }[l]}
                </span>
              ))
            : null
        return (
          <div key={r.id} style={{ padding: '6px 0', borderBottom: '1px dashed #f0f0f0' }}>
            <Space align="start" style={{ width: '100%', justifyContent: 'space-between' }}>
              <Space size={6} wrap>
                <Switch
                  size="small"
                  checked={ruleAvailable(r, ruleCtx) && !!enabled[r.id]}
                  disabled={!ruleAvailable(r, ruleCtx)}
                  onChange={(v) => setEnabled((prev) => ({ ...prev, [r.id]: v }))}
                />
                <Text strong>{r.name}</Text>
                {r.note ? <HelpTip tip={r.note} /> : null}
                {swatch}
                {!ruleAvailable(r, ruleCtx) ? <Tag>暂无数据</Tag> : null}
                {ruleAvailable(r, ruleCtx) && !inView ? <Tag>本视图不用</Tag> : null}
              </Space>
              {hit ? (
                <Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                  {hit.cells} 格 / {hit.rows} 场{hit.debugCells ? `（其中排查 ${hit.debugCells} 格）` : ''}
                  {hit.neutralCells ? `；偏高 ${hit.neutralCells} 格（不计）` : ''}
                  {hit.insufficientCells ? `；样本不足 ${hit.insufficientCells} 格` : ''}
                </Text>
              ) : null}
            </Space>
            <div style={{ fontSize: 12, marginTop: 2 }}>{r.condition}</div>
            {r.id === 'x1x2_return_rate' ? rrDebugSwitch : null}
          </div>
        )
      })}
      <div style={{ fontSize: 12, marginTop: 8 }}>
        <LabelWithHelp label="术语说明" tip={<>术语：初盘 = 各公司第一次开出的盘（各家开盘时间不同，悬停看开盘时间）；中盘 = 开赛前 8h；临盘 = 开赛前 1h；
        即时盘口 = 比赛结束前任一时刻抓到的当时盘口，「即时（最新）」列显示最新一条，悬停可看抓取时间。
        例外场（后端标记）= 所属竞彩日当晚 23:00 及以后开赛（含次日开赛）的场：中盘（规则）= 该竞彩日 15:00、临盘（规则）= 该竞彩日 22:00；打开「对照真实时点」可看中盘（真实）= 赛前 8h、临盘（真实）= 赛前 1h。
        欧赔「收盘（时间未知）」为接口收盘价，报价时刻未知，只在完赛后作参考显示，不参与任何规则。
        高亮与结算默认用（规则）。各阶段时刻由后端给出，悬停盘口格子可看「目标时间 / 抓取时间」。只比较同一阶段的快照，缺快照的格子留空不判。
        <br />
        口径文档：docs/schema/v2_0-data-table-highlight-rules.md</>} />
      </div>
    </div>
  )

  return (
    <Space orientation="vertical" size={12} className="sheet-page">
      <ReplicaBanner meta={dbMeta} />
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <Title level={3} style={{ marginBottom: 4 }}>
            数据表
            <HelpTip tip={`盘口、预测和赛果在一张表里看全。高亮规则已经预设好（口径 ${CONFIG_VERSION}）；预测只读，已经冻结。`} />
          </Title>
        </div>
        <Space wrap>
          <DataSourceSwitch value={dbSource} onChange={setDbSource} />
          {asOf ? <Tag>数据截至 {fmtAsOf(asOf)}</Tag> : null}
          {backendCfg && backendCfg !== CONFIG_VERSION ? (
            <Tooltip title={`后端接口返回的口径版本是 ${backendCfg}，前端高亮按 ${CONFIG_VERSION}；返还率基准等后端字段以后端为准`}>
              <Tag color="orange">后端口径 {backendCfg}</Tag>
            </Tooltip>
          ) : null}
        </Space>
      </div>

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space wrap size={[12, 8]}>
          <Space size={4}>
            <Text>日期</Text>
            <RangePicker
              value={range}
              allowClear={false}
              onChange={(v) => {
                if (v?.[0] && v[1]) setRange([v[0], v[1]])
              }}
            />
          </Space>
          <Segmented
            value={scope}
            onChange={(v) => setScope(v as typeof scope)}
            options={[
              { label: '竞彩', value: 'jingcai' },
              { label: '扩展', value: 'extra' },
              { label: '全部', value: 'all' },
            ]}
          />
          <Space size={4}>
            <Text>方案</Text>
            <Tooltip title="默认方案只读；预测已冻结，不在此页修改">
              <Select size="middle" value={DEFAULT_STRATEGY} disabled style={{ width: 150 }} options={[{ value: DEFAULT_STRATEGY, label: DEFAULT_STRATEGY }]} />
            </Tooltip>
          </Space>
          <Segmented
            value={view}
            onChange={(v) => setView(v as ViewId)}
            options={(Object.keys(VIEW_LABEL) as ViewId[]).map((k) => ({ label: VIEW_LABEL[k], value: k }))}
          />
        </Space>
        <Divider style={{ margin: '10px 0' }} />
        <Space wrap size={[12, 8]}>
          <Input.Search
            allowClear
            placeholder="快速搜索：球队 / 联赛 / 编号"
            style={{ width: 220 }}
            value={quick}
            onChange={(e) => setQuick(e.target.value)}
          />
          <Select
            mode="multiple"
            allowClear
            placeholder="联赛"
            style={{ minWidth: 160, maxWidth: 320 }}
            maxTagCount="responsive"
            value={leagues}
            onChange={setLeagues}
            options={leagueOptions}
          />
          <Tooltip title="只显示所选公司的盘口列">
            <Select
              mode="multiple"
              placeholder="公司"
              style={{ minWidth: 200 }}
              maxTagCount="responsive"
              value={books}
              onChange={(v: AhBook[]) => setBooks(v.length ? v : [...AH_BOOKS])}
              options={AH_BOOKS.map((b) => ({ value: b, label: BOOK_LABEL[b] }))}
            />
          </Tooltip>
          <Select
            mode="multiple"
            allowClear
            placeholder="结算"
            style={{ minWidth: 140 }}
            maxTagCount="responsive"
            value={settles}
            onChange={setSettles}
            options={SETTLE_OPTIONS}
          />
          <Tooltip
            title={
              exceptionCount
                ? `例外场（后端标记，本区间 ${exceptionCount} 场；所属竞彩日当晚 23:00 及以后开赛）的中盘/临盘默认取该竞彩日 15:00 / 22:00 规则时刻，格子右上角标「规」。打开后在规则列旁显示（真实）列：赛前 8h / 1h，只作对照，不参与高亮`
                : '本区间没有例外场'
            }
          >
            <Space size={6}>
              <Switch checked={showReal} onChange={setShowReal} disabled={!exceptionCount} />
              <Text>对照真实时点{exceptionCount ? `（${exceptionCount} 场）` : ''}</Text>
            </Space>
          </Tooltip>
          <Popover content={legend} title="高亮规则与图例" trigger="click" placement="bottomLeft">
            <Button icon={<BgColorsOutlined />}>规则与图例</Button>
          </Popover>
          <Button icon={<CopyOutlined />} onClick={copySelected} disabled={!selectedCount}>
            复制所选行{selectedCount ? `（${selectedCount}）` : ''}
          </Button>
          <Button icon={<DownloadOutlined />} onClick={exportCsv} disabled={!rows.length}>
            导出 CSV
          </Button>
          <Button icon={<UndoOutlined />} onClick={resetColumns}>
            重置列
          </Button>
          <Button icon={<ReloadOutlined />} onClick={reload} loading={loading}>
            刷新
          </Button>
        </Space>
      </Card>

      {error ? (
        <Alert
          type="error"
          showIcon
          message="数据读取失败"
          description={error}
          action={
            <Button size="small" danger loading={loading} onClick={reload}>
              重试
            </Button>
          }
        />
      ) : null}

      <div>
        <div style={{ height: 'calc(100vh - 330px)', minHeight: 420 }}>
          <AgGridReact<SheetRow>
            ref={gridRef}
            theme={gridTheme}
            localeText={AG_GRID_LOCALE_CN}
            rowData={rows}
            columnDefs={columnDefs}
            getRowId={(p) => p.data.id}
            context={ruleCtx}
            loading={loading}
            defaultColDef={{
              sortable: true,
              resizable: true,
              filter: true,
              floatingFilter: true,
              suppressHeaderMenuButton: true,
              minWidth: 64,
            }}
            defaultColGroupDef={{ headerClass: 'sheet-group-hdr' }}
            rowSelection={{ mode: 'multiRow', checkboxes: true, headerCheckbox: true, enableClickSelection: false }}
            selectionColumnDef={{ pinned: 'left', width: 44 }}
            quickFilterText={quick}
            isExternalFilterPresent={isExternalFilterPresent}
            doesExternalFilterPass={doesExternalFilterPass}
            suppressDragLeaveHidesColumns
            enableCellTextSelection
            tooltipShowDelay={300}
            preventDefaultOnContextMenu
            onCellContextMenu={onCellContextMenu}
            onRowDoubleClicked={(e) => e.data && navigate(`/matches/${encodeURIComponent(e.data.id)}`)}
            onSelectionChanged={(e) => setSelectedCount(e.api.getSelectedRows().length)}
            onModelUpdated={(e) => recomputeStats(e.api)}
            onGridReady={(e) => {
              restoreColumns(e.api, view)
              autoSizeColumns(e.api)
            }}
            onFirstDataRendered={(e) => autoSizeColumns(e.api)}
            onColumnMoved={(e) => e.finished && saveColumns()}
            onColumnResized={(e) => e.finished && !autosizingRef.current && saveColumns()}
            onColumnPinned={saveColumns}
            onColumnGroupOpened={(e) => recomputeStats(e.api)}
            onSortChanged={saveColumns}
            overlayNoRowsTemplate={error ? '读取失败' : '该区间暂无比赛'}
          />
        </div>
        <div className="sheet-statusbar">
          <span>总行数 {stats?.total ?? 0}</span>
          <span>筛选后 {stats?.shown ?? 0}</span>
          {stats
            ? SETTLE_OPTIONS.map((o) => (
                <span key={o.value} className={o.value !== 'pending' ? `ag-cell hl-settle-${o.value}` : undefined} style={{ position: 'static', padding: 0 }}>
                  {o.label} {stats.settle[o.value]}
                </span>
              ))
            : null}
          {stats ? (
            <Tooltip title={`后端结算${settlementVersion ? `（口径 ${settlementVersion}）` : ''}`}>
              <span>
                盈亏合计 {stats.pnl > 0 ? '+' : ''}
                {stats.pnl.toFixed(2)} 份
              </span>
            </Tooltip>
          ) : null}
          {dailyCheck && (dailyCheck.n_matches ?? 0) > 0 ? (
            <Tooltip
              title={
                <div>
                  <div>本页进日核对的场次（后端 daily_check_summary）：</div>
                  {Object.entries(dailyCheck.by_reason ?? {}).map(([k, v]) => (
                    <div key={k}>
                      {DAILY_CHECK_LABEL[k] ?? k}：{v} 场
                    </div>
                  ))}
                </div>
              }
            >
              <Tag style={{ marginInlineEnd: 0, fontSize: 11, lineHeight: '18px', color: '#595959' }}>
                待核对 {dailyCheck.n_matches} 场
              </Tag>
            </Tooltip>
          ) : null}
          {debugTier ? (
            <Tooltip title="「档位换算也上色（仅排查用）」已打开：带红色虚线框和「排查」角标的颜色只在页面显示，不进导出、不进方案计算。在「规则与图例」里关闭">
              <span className="sheet-debug-tip">排查模式：颜色不进导出</span>
            </Tooltip>
          ) : null}
          <span style={{ marginLeft: 'auto' }}>
            <HelpTip tip={'右键点击一行可以复制本行或打开详情；双击一行打开详情。\n数据来源：批量接口。'} />
          </span>
        </div>
      </div>

      <Dropdown
        open={!!menu}
        onOpenChange={(o) => !o && setMenu(null)}
        trigger={['contextMenu']}
        menu={{
          items: [
            { key: 'copy', label: '复制本行', icon: <CopyOutlined /> },
            { key: 'copySel', label: `复制所选行（${selectedCount}）`, disabled: !selectedCount },
            { key: 'open', label: '打开详情' },
          ],
          onClick: ({ key }) => {
            const row = menu?.row
            setMenu(null)
            if (!row) return
            if (key === 'copy') void copyRows(new Set([row.id]))
            if (key === 'copySel') copySelected()
            if (key === 'open') navigate(`/matches/${encodeURIComponent(row.id)}`)
          },
        }}
      >
        <div style={{ position: 'fixed', left: menu?.x ?? -9999, top: menu?.y ?? -9999, width: 1, height: 1 }} />
      </Dropdown>
    </Space>
  )
}
