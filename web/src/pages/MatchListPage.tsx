import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
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
import type { Direction, MatchListItem } from '../api/types'

const { Title, Text } = Typography

const directionColor: Record<Direction, string> = {
  主: 'blue',
  客: 'orange',
  不下注: 'default',
}

export default function MatchListPage() {
  const navigate = useNavigate()
  const [date, setDate] = useState<Dayjs>(dayjs('2026-06-06'))
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
      title: '对阵',
      render: (_, row) => (
        <Text strong>
          {row.match.teams.home}{' '}
          <Text type="secondary">对</Text> {row.match.teams.away}
        </Text>
      ),
    },
    {
      title: '开赛',
      width: 72,
      render: (_, row) =>
        row.match.kickoff_hour != null ? `${row.match.kickoff_hour}:00` : '—',
    },
    {
      title: '赛果',
      width: 72,
      render: (_, row) => formatScore(row.result),
    },
    {
      title: '方向',
      width: 96,
      render: (_, row) =>
        row.direction ? (
          <Tag color={directionColor[row.direction]}>{row.direction}</Tag>
        ) : (
          <Text type="secondary">—</Text>
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
          <Text type="secondary">按竞彩日 → 开赛小时 → 竞彩编号排序 · 默认只看竞彩场</Text>
        </div>
        <Tag color={source === 'api' ? 'success' : 'warning'}>
          {labelDataSource(source)}
        </Tag>
      </div>

      <Space wrap>
        <DatePicker
          value={date}
          onChange={(d) => d && setDate(d)}
          allowClear={false}
        />
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
              <Empty description="该竞彩日暂无比赛" />
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
