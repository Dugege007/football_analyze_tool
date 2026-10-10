import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  message,
  Button,
  Card,
  DatePicker,
  Empty,
  Segmented,
  Space,
  Switch,
  Collapse,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs, { type Dayjs } from 'dayjs'
import { listMatches, type DataSource } from '../api/client'
import { labelDataSource } from '../labels'
import { formatScore } from '../api/formatScore'
import type { MatchListItem } from '../api/types'
import { PICK_COLUMNS, formatMarketCell, hasEstimatedStake } from '../api/pickFormat'
import { buildDailyMessage, type MessageMatch } from '../api/dailyMessageTemplate'
import { HelpTip, LabelWithHelp } from '../components/HelpTip'

const PICK_TIPS: Record<string, string> = {
  ah: '亚盘按雷速体育的规则显示：+ 表示所下注的一方让球，- 表示所下注的一方受让。\nx 后面的数字是下注份数；份数后面带 * 表示冻结预测没有记录份数，份数是按方案规则推算的，复制出去的消息不带 *。\n不下注显示「-」。',
  '1x2': '欧盘方向，例如「胜 x3」「平负 x1」；x 后面的数字是下注份数。不下注显示「-」。',
  ou: '大小球方向，例如「2.25大 x2」；x 后面的数字是下注份数。不下注显示「-」。',
  jc_had: '竞彩胜平负方向；x 后面的数字是下注份数。不下注显示「-」。',
  jc_hhad: '竞彩让球按竞彩官方的规则显示：+ 表示主队受让，- 表示主队让球，例如「-1让负 x2」。不下注显示「-」。',
}

const SHOW_PICKS_KEY = 'schedule.showPredictionColumns'

const { Title, Text, Paragraph } = Typography

/**
 * 北京时间（UTC+8）此刻所属的竞彩日。
 * 竞彩日口径：北京时间 00:00 到 11:30（含 11:30）开赛的场次属于前一个竞彩日，
 * 所以 11:30 之前打开页面时默认显示前一天，11:30 之后显示当天。
 */
function currentJingcaiDate(): Dayjs {
  const bj = new Date(Date.now() + 8 * 3600 * 1000)
  const minutes = bj.getUTCHours() * 60 + bj.getUTCMinutes()
  if (minutes <= 11 * 60 + 30) bj.setUTCDate(bj.getUTCDate() - 1)
  return dayjs(bj.toISOString().slice(0, 10))
}

/** 把接口返回的开赛时间换算成北京时间的「年-月-日 时:分」。 */
function formatKickoff(row: MatchListItem): string {
  const m = row.match
  if (m.kickoff_at) {
    const t = new Date(new Date(m.kickoff_at).getTime() + 8 * 3600 * 1000)
    if (!Number.isNaN(t.getTime())) {
      const s = t.toISOString()
      const text = `${s.slice(0, 10)} ${s.slice(11, 16)}`
      return m.kickoff_minute_known === false ? `${s.slice(0, 10)} ${s.slice(11, 13)}时（分钟未知）` : text
    }
  }
  if (m.kickoff_hour != null) {
    return `竞彩日 ${m.date} 的 ${String(m.kickoff_hour).padStart(2, '0')}时（日期和分钟未知）`
  }
  return '—'
}

export default function MatchListPage() {
  const navigate = useNavigate()
  const [date, setDate] = useState<Dayjs>(() => currentJingcaiDate())
  const [scope, setScope] = useState<string>('jingcai')
  const [loading, setLoading] = useState(false)
  const [items, setItems] = useState<MatchListItem[]>([])
  const [source, setSource] = useState<DataSource>('mock')
  const [error, setError] = useState<string | null>(null)
  const [showPicks, setShowPicks] = useState<boolean>(
    () => typeof localStorage !== 'undefined' && localStorage.getItem(SHOW_PICKS_KEY) === '1',
  )
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 60 * 1000)
    return () => clearInterval(t)
  }, [])

  const toggleShowPicks = (v: boolean) => {
    setShowPicks(v)
    try {
      localStorage.setItem(SHOW_PICKS_KEY, v ? '1' : '0')
    } catch {
      /* 浏览器禁止本地存储时只在本次页面内生效 */
    }
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    listMatches(date.format('YYYY-MM-DD'), scope)
      .then((res) => {
        if (cancelled) return
        setItems(res.data.items)
        setSource(res.source)
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [date, scope])

  const columns: ColumnsType<MatchListItem> = [
    {
      title: '竞彩号',
      width: 88,
      render: (_, row) => row.match.jc?.id ?? '—',
    },
    {
      title: '联赛',
      width: 96,
      render: (_, row) => row.match.competition?.name ?? '—',
    },
    {
      title: '主队',
      align: 'right',
      render: (_, row) => <Text strong>{row.match.teams.home}</Text>,
    },
    {
      title: '',
      width: 56,
      align: 'center',
      render: () => (
        <Text type="secondary" style={{ whiteSpace: 'nowrap' }}>
          vs
        </Text>
      ),
    },
    {
      title: '客队',
      render: (_, row) => <Text strong>{row.match.teams.away}</Text>,
    },
    {
      title: <LabelWithHelp label="开赛时间" tip="北京时间。分钟未知时只显示到小时。" />,
      width: 190,
      render: (_, row) => formatKickoff(row),
    },
    {
      title: '赛果',
      width: 72,
      render: (_, row) => formatScore(row.result),
    },
    {
      title: '总进球数',
      width: 88,
      render: (_, row) =>
        row.result ? row.result.home_goals + row.result.away_goals : '-',
    },
    {
      title: '使用方案',
      width: 150,
      render: (_, row) =>
        row.strategies && row.strategies.length > 0 ? (
          <Space size={4} wrap>
            {row.strategies.map((k) => (
              <Tag key={k}>{k}</Tag>
            ))}
          </Space>
        ) : (
          '-'
        ),
    },
  ]

  const pickColumns: ColumnsType<MatchListItem> = PICK_COLUMNS.map((c) => ({
    title: <LabelWithHelp label={c.title} tip={PICK_TIPS[c.market]} />,
    width: 112,
    render: (_: unknown, row: MatchListItem) => {
      const text = formatMarketCell(row.picks, c.market)
      if (text === '-') return '-'
      const est = c.market === 'ah' && hasEstimatedStake(row.picks?.filter((p) => p.market === 'ah'))
      return (
        <Text strong style={{ whiteSpace: 'nowrap' }}>
          {text}
          {est && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              *
            </Text>
          )}
        </Text>
      )
    },
  }))

  const shownColumns = showPicks ? [...columns, ...pickColumns] : columns

  const messageMatches: MessageMatch[] = items.map((row) => {
    const ms = row.match.kickoff_at ? new Date(row.match.kickoff_at).getTime() : NaN
    return {
      jcId: row.match.jc?.id,
      league: row.match.competition?.name,
      home: row.match.teams.home,
      away: row.match.teams.away,
      kickoffText: formatKickoff(row),
      kickoffMs: Number.isNaN(ms) ? null : ms,
      picks: row.picks,
    }
  })
  const dateText = date.format('YYYY-MM-DD')
  const msgAll = buildDailyMessage(dateText, messageMatches)
  const msgPending = buildDailyMessage(dateText, messageMatches, { onlyNotStarted: true, nowMs: now })
  const copyText = async (text: string | null, label: string) => {
    if (!text) {
      message.info(`${label}：没有可以复制的场次。`)
      return
    }
    try {
      await navigator.clipboard.writeText(text)
      message.success(`${label}：已复制到剪贴板。`)
    } catch {
      message.warning('浏览器不允许写入剪贴板，请手动选中预览里的文字复制。')
    }
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <Title level={3} style={{ marginBottom: 4 }}>
            当日赛程
            <HelpTip tip={'默认显示北京时间今天所属的竞彩日：北京时间 00:00 到 11:30 开赛的场次属于前一个竞彩日；有竞彩编号的场次以编号里的星期为准。\n同一竞彩日内按竞彩编号升序排列；没有编号的比赛排在最后，再按开赛时间排列。\n默认只看竞彩场。'} />
          </Title>
        </div>
        <Tag color={source === 'api' ? 'success' : 'warning'}>
          {labelDataSource(source)}
        </Tag>
      </div>

      <Space wrap>
        <Button onClick={() => setDate((d) => d.subtract(1, 'day'))}>上一日</Button>
        <DatePicker
          value={date}
          onChange={(d) => d && setDate(d)}
          allowClear={false}
        />
        <Button onClick={() => setDate((d) => d.add(1, 'day'))}>下一日</Button>
        <Button onClick={() => setDate(currentJingcaiDate())}>今日</Button>
        <Segmented
          value={scope}
          onChange={(v) => setScope(String(v))}
          options={[
            { label: '竞彩', value: 'jingcai' },
            { label: '扩展', value: 'extra' },
            { label: '全部', value: 'all' },
          ]}
        />
      </Space>

      <Collapse
        size="small"
        items={[
          {
            key: 'msg',
            label: (
              <span>
                当日消息预览（{msgAll ? msgAll.split('\n').length - 1 : 0} 场下注）
                <HelpTip tip="消息只在页面上显示和复制，不会自动发送。「只复制未开赛」只复制当前时间还没有开赛的场次。" />
              </span>
            ),
            extra: (
              <Space size={8} onClick={(e) => e.stopPropagation()}>
                <Button size="small" onClick={() => copyText(msgAll, '复制全部')}>
                  复制全部
                </Button>
                <Button size="small" onClick={() => copyText(msgPending, '只复制未开赛')}>
                  只复制未开赛
                </Button>
              </Space>
            ),
            children: (
              <Paragraph
                style={{
                  marginBottom: 0,
                  whiteSpace: 'pre-wrap',
                  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                  fontSize: 13,
                }}
              >
                {msgAll ?? '这一天没有下注的场次。'}
              </Paragraph>
            ),
          },
        ]}
      />

      <Space>
        <Switch checked={showPicks} onChange={toggleShowPicks} />
        <span>
          显示预测方向
          <HelpTip tip="展开亚盘、欧盘、大小、竞彩、竞彩让球五列。方向来自当时正式方案的冻结预测，不下注显示「-」。" />
        </span>
      </Space>

      <Card styles={{ body: { padding: 0 } }}>
        <Table
          rowKey="id"
          loading={loading}
          scroll={{ x: 'max-content' }}
          columns={shownColumns}
          dataSource={items}
          pagination={false}
          locale={{
            emptyText: error ? (
              <Empty description={error} />
            ) : (
              <Empty description="这一天正式库里还没有比赛数据。" />
            ),
          }}
          onRow={(row) => ({
            onClick: () => navigate(`/matches/${encodeURIComponent(row.id)}`),
            style: { cursor: 'pointer' },
          })}
        />
      </Card>
    </Space>
  )
}
