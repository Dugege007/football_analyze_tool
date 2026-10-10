import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Alert,
  Breadcrumb,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  List,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'
import {
  getMatch,
  getMatchOdds,
  getMatchPrediction,
  type DataSource,
} from '../api/client'
import { LEAK_SUSPECT_TAG, labelDataSource, labelSettleBook, readLedgerNote, readLeakSuspect } from '../labels'
import { DEFAULT_STRATEGY, STRATEGY_WHITELIST } from '../api/strategies'
import OddsWaterTimelineChart from '../components/OddsWaterTimelineChart'
import { isCollectorEmpty } from '../sheet/types'
import { apiBase, useDbSource } from '../api/dataSource'
import type {
  AsianLineFull,
  AsianPhases,
  Direction,
  MatchDetail,
  Odds,
  Prediction,
} from '../api/types'

const { Title, Text, Paragraph } = Typography


function metaLabel(
  stats: MatchDetail['stats'],
  kind: 'recent' | 'h2h' | 'rank' | 'injury' | 'weather' | 'popularity',
): string | null {
  const m = stats.meta?.[kind]
  if (!m) return null
  const bits: string[] = []
  if (m.source === 'manual_seed') bits.push('手工底座')
  else if (m.source) bits.push(String(m.source))
  if (m.as_of) bits.push(`as_of ${m.as_of}`)
  return bits.length ? bits.join(' · ') : null
}

/** 伤停：未采到 null；仅 obs.injury.known_empty===true 才「确认无伤停」 */
function formatInjury(stats: MatchDetail['stats']): string {
  const inj = stats.injury
  const obs = stats.obs?.injury
  if (obs?.known_empty === true) return '确认无伤停'
  if (inj == null) return '未采到伤停'
  if (Array.isArray(inj) && inj.length === 0) {
    // 空数组且无 known_empty → 仍按未确认空，不写「确认无伤停」
    return '未采到伤停'
  }
  try {
    return typeof inj === 'string' ? inj : JSON.stringify(inj)
  } catch {
    return '—'
  }
}

const directionColor: Record<Direction, string> = {
  主: 'blue',
  客: 'orange',
  不下注: 'default',
}

function formatAsian(phase: AsianPhases | undefined, key: 'open' | 'mid' | 'close') {
  if (!phase) return '—'
  const v = phase[key]
  if (v == null) return '—'
  if (typeof v === 'number') return String(v)
  const line = v as AsianLineFull
  const parts = [
    line.home_water != null ? `主水 ${line.home_water}` : null,
    line.handicap != null ? `盘 ${line.handicap}` : null,
    line.away_water != null ? `客水 ${line.away_water}` : null,
  ].filter(Boolean)
  return parts.length ? parts.join(' · ') : '—'
}

export default function MatchDetailPage() {
  const { id = '' } = useParams()
  const matchId = decodeURIComponent(id)
  const [strategy, setStrategy] = useState(DEFAULT_STRATEGY)

  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<MatchDetail | null>(null)
  const [odds, setOdds] = useState<Odds | null>(null)
  const [prediction, setPrediction] = useState<Prediction | null>(null)
  const [source, setSource] = useState<DataSource>('mock')
  const [predError, setPredError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [timelineOpen, setTimelineOpen] = useState(true)
  const [dbSource] = useDbSource()

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    const base = apiBase(dbSource)
    Promise.all([
      getMatch(matchId, base),
      getMatchOdds(matchId, base).catch(() => null),
    ])
      .then(([d, o]) => {
        if (cancelled) return
        setDetail(d.data)
        setSource(d.source)
        setOdds(o?.data ?? d.data.odds)
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
  }, [matchId, dbSource])

  useEffect(() => {
    let cancelled = false
    setPredError(null)
    setPrediction(null)
    getMatchPrediction(matchId, strategy, apiBase(dbSource))
      .then((p) => {
        if (!cancelled) setPrediction(p.data)
      })
      .catch((e: Error) => {
        if (!cancelled) setPredError(e.message)
      })
    return () => {
      cancelled = true
    }
  }, [matchId, strategy, dbSource])

  const asianRows = useMemo(() => {
    const asian = odds?.asian ?? {}
    return Object.entries(asian).map(([book, phases]) => ({
      key: book,
      book,
      open: formatAsian(phases, 'open'),
      mid: formatAsian(phases, 'mid'),
      close: formatAsian(phases, 'close'),
    }))
  }, [odds])

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 80 }}>
        <Spin size="large" />
      </div>
    )
  }

  if (error || !detail) {
    return <Empty description={error ?? '未找到比赛'} />
  }

  const { match, stats } = detail
  const h2h = (stats.h2h ?? {}) as Record<string, unknown>
  const h2hLast6 = (h2h.last6 ?? {}) as Record<string, number>
  const recent = (stats.recent ?? {}) as Record<string, unknown>
  const last6 = (recent.last6 ?? {}) as {
    home?: { gf?: number; ga?: number }
    away?: { gf?: number; ga?: number }
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <Breadcrumb
          items={[
            {
              title: (
                <Link to="/">
                  <ArrowLeftOutlined /> 赛程
                </Link>
              ),
            },
            { title: match.jc?.id ?? match.teams.home },
          ]}
        />
        <Tag color={source === 'api' ? 'success' : 'warning'}>
          {labelDataSource(source)}
        </Tag>
      </div>

      <div>
        <Space align="center" wrap>
          {match.jc && <Tag color="geekblue">{match.jc.id}</Tag>}
          <Tag>{match.competition?.name ?? '—'}</Tag>
          <Tag>{match.scope === 'jingcai' ? '竞彩' : '扩展'}</Tag>
        </Space>
        <Title level={3} style={{ marginTop: 8, marginBottom: 4 }}>
          {match.teams.home} 对 {match.teams.away}
        </Title>
        <Text type="secondary">
          竞彩日 {match.date}
          {match.kickoff_hour != null ? ` · 开赛约 ${match.kickoff_hour}:00` : ''}
        </Text>
      </div>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <Card title="盘口（初盘/中盘/临盘）" size="small">
            <Table
              size="small"
              pagination={false}
              dataSource={asianRows}
              locale={{ emptyText: '暂无亚盘' }}
              columns={[
                { title: '公司', dataIndex: 'book', width: 100 },
                { title: '初盘', dataIndex: 'open' },
                { title: '中盘', dataIndex: 'mid' },
                { title: '临盘', dataIndex: 'close' },
              ]}
            />
            {odds?.jc_home_win && (
              <Alert
                style={{ marginTop: 12 }}
                type="warning"
                showIcon
                title="竞彩胜平负不完整（仅主胜影子）"
                description="来自 odds_jc_home，缺平/负；不冒充完整竞彩胜平负。表格页见灰字「竞彩胜平负不完整」。"
              />
            )}
            {odds?.jc_hhad?.open && (
              <Alert
                style={{ marginTop: 12 }}
                type={isCollectorEmpty(odds.jc_hhad.open.missing_reason) ? 'warning' : 'info'}
                showIcon
                title={
                  isCollectorEmpty(odds.jc_hhad.open.missing_reason)
                    ? '暂无竞彩官方数据（让球）'
                    : `竞彩让球（详情为当前主表行） ${odds.jc_hhad.open.goal_line ?? '—'}`
                }
                description={
                  isCollectorEmpty(odds.jc_hhad.open.missing_reason)
                    ? '本地采集器未接通或尚未采集；决策时刻选线请以数据表 /table/matches 的 goal_line/decision_line 为准'
                    : `胜 ${odds.jc_hhad.open.home ?? '—'} / 平 ${odds.jc_hhad.open.draw ?? '—'} / 负 ${odds.jc_hhad.open.away ?? '—'}${
                        odds.jc_hhad.open.post_decision_line_change
                          ? '；决策后让球线已变（当前线仅对照）'
                          : ''
                      }`
                }
              />
            )}
          </Card>
        </Col>

        <Col xs={24} lg={10}>
          <Card title="交锋 / 近况 / 伤停" size="small">
            <Descriptions column={1} size="small">
              <Descriptions.Item label="排名">
                主 {stats.rank?.home ?? '—'} / 客 {stats.rank?.away ?? '—'}
                {metaLabel(stats, 'rank') ? (
                  <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                    （{metaLabel(stats, 'rank')}）
                  </Text>
                ) : null}
              </Descriptions.Item>
              <Descriptions.Item label="交锋场次">
                {String(h2h.matches ?? '—')}
                {metaLabel(stats, 'h2h') ? (
                  <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                    （{metaLabel(stats, 'h2h')}）
                  </Text>
                ) : null}
              </Descriptions.Item>
              <Descriptions.Item label="交锋近6（主视角）">
                {h2hLast6.home_gf != null
                  ? `${h2hLast6.home_gf} : ${h2hLast6.home_ga}`
                  : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="近6进失">
                主 {last6.home ? `${last6.home.gf}/${last6.home.ga}` : '—'} · 客{' '}
                {last6.away ? `${last6.away.gf}/${last6.away.ga}` : '—'}
                {metaLabel(stats, 'recent') ? (
                  <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                    （{metaLabel(stats, 'recent')}）
                  </Text>
                ) : null}
              </Descriptions.Item>
              <Descriptions.Item label="伤停">
                <Text type={stats.injury == null && stats.obs?.injury?.known_empty !== true ? 'secondary' : undefined}>
                  {formatInjury(stats)}
                </Text>
                {metaLabel(stats, 'injury') ? (
                  <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                    （{metaLabel(stats, 'injury')}）
                  </Text>
                ) : null}
              </Descriptions.Item>
              <Descriptions.Item label="支持率代理">
                {stats.support_proxy_odds ?? '—'}
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>

      <Card title="赛前盘口／水位折线" size="small">
        <Collapse
          size="small"
          activeKey={timelineOpen ? ['timeline'] : []}
          onChange={(keys) => {
            const open = (Array.isArray(keys) ? keys : [keys]).includes('timeline')
            setTimelineOpen(open)
          }}
          items={[
            {
              key: 'timeline',
              label: '查看亚盘盘口阶梯与水位走势（默认平博，可切换机构）',
              children: (
                <OddsWaterTimelineChart
                  matchId={matchId}
                  kickoffAt={match.kickoff_at}
                  defaultBook="pinnacle"
                  active={timelineOpen}
                />
              ),
            },
          ]}
        />
      </Card>

      <Card
        title="预测结论"
        size="small"
        extra={
          <Space wrap>
            <Select
              size="small"
              value={strategy}
              style={{ minWidth: 200 }}
              options={STRATEGY_WHITELIST.map((s) => ({
                value: s.value,
                label: s.ready ? s.label : `${s.label}（暂无数据）`,
                disabled: !s.ready,
              }))}
              onChange={setStrategy}
            />
            {prediction ? (
              <Tag color={directionColor[prediction.direction]}>{prediction.direction}</Tag>
            ) : null}
            {prediction && readLedgerNote(prediction) ? (
              <Tooltip title={readLedgerNote(prediction)}>
                <Tag>台账备注</Tag>
              </Tooltip>
            ) : null}
            {prediction && readLeakSuspect(prediction).suspect ? (
              <Tooltip title={readLeakSuspect(prediction).reason ?? '后端标记疑似泄漏'}>
                <Tag style={{ color: '#8c8c8c' }}>{LEAK_SUSPECT_TAG}</Tag>
              </Tooltip>
            ) : null}
          </Space>
        }
      >
        {prediction ? (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <Descriptions column={{ xs: 1, sm: 2 }} size="small">
              <Descriptions.Item label="方案">{prediction.strategy}</Descriptions.Item>
              <Descriptions.Item label="结算盘口">{labelSettleBook(prediction.settle_book)}</Descriptions.Item>
              <Descriptions.Item label="置信度">
                {prediction.confidence ?? '—'}
              </Descriptions.Item>
            </Descriptions>
            <div>
              <Text type="secondary">依据要点</Text>
              <List
                size="small"
                dataSource={prediction.rationale}
                renderItem={(item) => <List.Item>{item}</List.Item>}
                locale={{ emptyText: '暂无依据' }}
              />
            </div>
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              仅供参考，不保证输赢。默认按澳门收盘结算口径展示。
            </Paragraph>
          </Space>
        ) : (
          <Empty description={predError ? `${predError}（${strategy}）` : '暂无预测'} />
        )}
      </Card>
    </Space>
  )
}
