import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Card,
  DatePicker,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs, { type Dayjs } from 'dayjs'
import {
  getMatchOdds,
  getMatchPrediction,
  listMatches,
  type DataSource,
} from '../api/client'
import { labelDataSource } from '../labels'
import { DEFAULT_STRATEGY } from '../api/strategies'
import { formatScore } from '../api/formatScore'
import {
  extractMacauCloseHandicap,
  formatAhMessageLine,
  formatMatchHeaderLine,
  simplifiedStakeFromRationale,
} from '../api/messagePreview'
import type { Direction, MatchListItem, Result } from '../api/types'

const { Title, Text, Paragraph } = Typography

const directionColor: Record<Direction, string> = {
  主: 'blue',
  客: 'orange',
  不下注: 'default',
}

type Row = MatchListItem & {
  stake: number | null
  handicap: number | null
  preview: string | null
  header: string
  result: Result | null
}

export default function PredictionsPage() {
  const [date, setDate] = useState<Dayjs>(dayjs('2026-06-06'))
  const [loading, setLoading] = useState(false)
  const [source, setSource] = useState<DataSource>('mock')
  const [rows, setRows] = useState<Row[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const d = date.format('YYYY-MM-DD')
    setLoading(true)
    setError(null)
    listMatches(d, 'jingcai')
      .then(async (res) => {
        if (cancelled) return
        setSource(res.source)
        const enriched = await Promise.all(
          res.data.items.map(async (it) => {
            const header = formatMatchHeaderLine({
              jcNo: it.match.jc?.id || it.match.jc?.no,
              kickoff: it.match.kickoff_at,
              league: it.match.competition?.name,
              home: it.match.teams.home,
              away: it.match.teams.away,
            })
            const result = it.result ?? null
            if (!it.has_prediction) {
              return {
                ...it,
                result,
                stake: null,
                handicap: null,
                preview: null,
                header,
              }
            }
            const [predRes, oddsRes] = await Promise.all([
              getMatchPrediction(it.id, DEFAULT_STRATEGY).catch(() => null),
              getMatchOdds(it.id).catch(() => null),
            ])
            const pred = predRes?.data
            const direction = pred?.direction ?? it.direction ?? null
            const handicap = extractMacauCloseHandicap(oddsRes?.data)
            const stake =
              pred?.stake != null && pred.stake > 0
                ? pred.stake
                : simplifiedStakeFromRationale(pred?.rationale)
            const preview = formatAhMessageLine(direction, handicap, stake)
            return {
              ...it,
              result,
              direction: direction ?? it.direction,
              stake,
              handicap,
              preview,
              header,
            }
          }),
        )
        if (!cancelled) setRows(enriched)
      })
      .catch((e: Error) => {
        if (!cancelled) {
          setError(e.message)
          message.warning(e.message)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [date])

  const digest = useMemo(() => {
    const lines: string[] = []
    for (const r of rows) {
      if (!r.preview || r.direction === '不下注') continue
      lines.push(r.header)
      lines.push(`  ${r.preview}；`)
    }
    return lines.length ? lines.join('\n') : '（当日无可发方向，或份/盘口未齐）'
  }, [rows])

  const columns: ColumnsType<Row> = [
    {
      title: '编号',
      width: 88,
      render: (_, r) => r.match.jc?.id || '—',
    },
    {
      title: '对阵',
      render: (_, r) => (
        <Link to={`/matches/${encodeURIComponent(r.id)}`}>
          {r.match.teams.home} 对 {r.match.teams.away}
        </Link>
      ),
    },
    {
      title: '联赛',
      width: 110,
      render: (_, r) => r.match.competition?.name || '—',
    },
    {
      title: '赛果',
      width: 72,
      render: (_, r) => formatScore(r.result),
    },
    {
      title: '方向',
      width: 88,
      render: (_, r) =>
        r.direction ? (
          <Tag color={directionColor[r.direction]}>{r.direction}</Tag>
        ) : (
          '—'
        ),
    },
    {
      title: '份',
      width: 56,
      render: (_, r) => (r.stake != null ? r.stake : '—'),
    },
    {
      title: '消息预览',
      render: (_, r) =>
        r.preview ? (
          <Text code style={{ whiteSpace: 'pre' }}>
            {r.preview}
          </Text>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Title level={3} style={{ marginBottom: 4 }}>
          预测
        </Title>
        <Text type="secondary">
          按竞彩日列场次 · 默认方案 {DEFAULT_STRATEGY} · 消息格式{' '}
          <Text code>主+0.5  x2</Text>（不写玩法名）
        </Text>
      </div>

      <Space wrap>
        <span>竞彩日</span>
        <DatePicker value={date} onChange={(v) => v && setDate(v)} allowClear={false} />
        <Tag color={source === 'api' ? 'success' : 'warning'}>
          {labelDataSource(source)}
        </Tag>
      </Space>

      {error && <Alert type="error" showIcon message={error} />}

      <Card size="small" title="当日消息预览（可复制）">
        <Paragraph
          copyable={{ text: digest }}
          style={{
            marginBottom: 0,
            whiteSpace: 'pre-wrap',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
            fontSize: 13,
          }}
        >
          {digest}
        </Paragraph>
      </Card>

      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={false}
        size="middle"
      />
    </Space>
  )
}
