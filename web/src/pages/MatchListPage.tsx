import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button,
  Card,
  DatePicker,
  Empty,
  Segmented,
  Space,
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

const { Title, Text } = Typography

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
      title: '开赛时间（北京时间）',
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

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <Title level={3} style={{ marginBottom: 4 }}>
            当日赛程
          </Title>
          <Text type="secondary">同一竞彩日内按竞彩编号升序排列；没有编号的比赛排在最后，再按开赛时间排列。默认显示北京时间今天所属的竞彩日（北京时间 00:00 到 11:30 开赛的场次属于前一个竞彩日），默认只看竞彩场。</Text>
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

      <Card styles={{ body: { padding: 0 } }}>
        <Table
          rowKey="id"
          loading={loading}
          columns={columns}
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
