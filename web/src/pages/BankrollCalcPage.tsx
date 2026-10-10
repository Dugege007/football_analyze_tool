import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Form,
  InputNumber,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  calcBankroll,
  getBankrollConfig,
  type CalcResponse,
} from '../api/client'

const { Title, Text, Paragraph } = Typography

type RowForm = {
  key: string
  match_id?: string
  strategy: string
  stake_units: number
}

export default function BankrollCalcPage() {
  const [bankroll, setBankroll] = useState<number>(10000)
  const [remaining, setRemaining] = useState<number | null>(null)
  const [configNote, setConfigNote] = useState<string>('')
  const [fxCurrency, setFxCurrency] = useState<string>('CNY')
  const [fxRatesEmpty, setFxRatesEmpty] = useState(true)
  const [rows, setRows] = useState<RowForm[]>([
    {
      key: '1',
      match_id: '2026-06-06|六204',
      strategy: 'CFFXDJ_5_V3',
      stake_units: 2,
    },
  ])
  const [result, setResult] = useState<CalcResponse | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    getBankrollConfig()
      .then((res) => {
        const stats = res.data.items.stats_initial_bankroll?.value?.amount
        const calc = res.data.items.calculator_bankroll?.value?.amount
        const snap = res.data.latest_snapshot?.balance
        if (typeof calc === 'number') setBankroll(calc)
        else if (typeof stats === 'number') setBankroll(stats)
        if (typeof snap === 'number') setRemaining(snap)
        const todo = res.data.amount_todo?.length
          ? `待配置：${res.data.amount_todo.join('、')}`
          : ''
        setConfigNote(todo)
        const fxCur = res.data.items.fx_display_currency?.value?.currency
        if (typeof fxCur === 'string' && fxCur) setFxCurrency(fxCur)
        const rates = res.data.items.fx_rates?.value?.rates
        setFxRatesEmpty(!(rates && typeof rates === 'object' && Object.keys(rates as object).length > 0))
      })
      .catch((e: Error) => message.warning(e.message))
  }, [])

  const limitsText = useMemo(() => {
    if (!result) {
      return '单注≥50、≤剩余资金 50%；单场≤3 份、单日≤10 份；1 份 = 基数 × 0.5%'
    }
    const { amount_limits: a, caps, unit_amount, unit_fraction } = result
    return `1 份 = ${unit_amount}（基数 × ${(unit_fraction * 100).toFixed(1)}%）；单注≥${a.min_stake_amount}、≤剩余 ${(a.max_stake_pct_of_remaining * 100).toFixed(0)}%；单场≤${caps.per_match} 份、单日≤${caps.per_day} 份`
  }, [result])

  const onCalc = async () => {
    setLoading(true)
    try {
      const body = {
        bankroll,
        ...(remaining != null ? { remaining_bankroll: remaining } : {}),
        items: rows.map((r) => ({
          match_id: r.match_id || undefined,
          strategy: r.strategy,
          stake_units: r.stake_units,
        })),
      }
      const res = await calcBankroll(body)
      setResult(res.data)
      if (res.data.warnings?.length) {
        message.warning(res.data.warnings.join('；'))
      }
    } catch (e) {
      message.error(e instanceof Error ? e.message : '计算失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Title level={3} style={{ marginBottom: 4 }}>
          注额计算器
        </Title>
        <Text type="secondary">
          只算「份 → 金额」展示，不改预测入库。多方案对比请分开计算，避免抢同一天 10 份。
        </Text>
      </div>

      <Card size="small" title="汇率展示（占位）">
        <Text>
          展示币种：<Tag>{fxCurrency}</Tag>
          {fxRatesEmpty
            ? '汇率表为空，金额仍按人民币展示；换算逻辑待汇率写入后再接。'
            : '已读到汇率表（换算逻辑尚未接入）。'}
        </Text>
      </Card>

      {configNote ? <Alert type="info" showIcon message={configNote} /> : null}
      <Alert type="success" showIcon message={limitsText} />

      <Card title="本金" size="small">
        <Form layout="inline">
          <Form.Item label="计算本金">
            <InputNumber
              min={1}
              step={100}
              value={bankroll}
              onChange={(v) => v != null && setBankroll(v)}
              addonAfter="CNY"
              style={{ width: 180 }}
            />
          </Form.Item>
          <Form.Item label="剩余资金（可选）">
            <InputNumber
              min={0}
              step={100}
              value={remaining ?? undefined}
              onChange={(v) => setRemaining(v)}
              placeholder="默认用最新资金快照 / 基数"
              style={{ width: 220 }}
            />
          </Form.Item>
          <Form.Item>
            <Button type="primary" loading={loading} onClick={onCalc}>
              计算
            </Button>
          </Form.Item>
        </Form>
      </Card>

      <Card
        title="待算条目"
        size="small"
        extra={
          <Button
            size="small"
            onClick={() =>
              setRows((prev) => [
                ...prev,
                {
                  key: String(Date.now()),
                  strategy: 'CFFXDJ_5_V3',
                  stake_units: 1,
                },
              ])
            }
          >
            加一行
          </Button>
        }
      >
        <Table
          size="small"
          pagination={false}
          rowKey="key"
          dataSource={rows}
          columns={[
            {
              title: '比赛编号',
              render: (_, row, idx) => (
                <input
                  style={{ width: '100%', padding: 4 }}
                  value={row.match_id ?? ''}
                  placeholder="2026-06-06|六204"
                  onChange={(e) => {
                    const v = e.target.value
                    setRows((prev) =>
                      prev.map((r, i) => (i === idx ? { ...r, match_id: v } : r)),
                    )
                  }}
                />
              ),
            },
            {
              title: '方案',
              width: 160,
              render: (_, row, idx) => (
                <input
                  style={{ width: '100%', padding: 4 }}
                  value={row.strategy}
                  onChange={(e) => {
                    const v = e.target.value
                    setRows((prev) =>
                      prev.map((r, i) => (i === idx ? { ...r, strategy: v } : r)),
                    )
                  }}
                />
              ),
            },
            {
              title: '份',
              width: 100,
              render: (_, row, idx) => (
                <InputNumber
                  min={0}
                  max={3}
                  value={row.stake_units}
                  onChange={(v) =>
                    setRows((prev) =>
                      prev.map((r, i) =>
                        i === idx ? { ...r, stake_units: v ?? 0 } : r,
                      ),
                    )
                  }
                />
              ),
            },
            {
              title: '',
              width: 72,
              render: (_, __, idx) => (
                <Button
                  size="small"
                  danger
                  disabled={rows.length <= 1}
                  onClick={() => setRows((prev) => prev.filter((_, i) => i !== idx))}
                >
                  删
                </Button>
              ),
            },
          ]}
        />
      </Card>

      {result && (
        <Card title="计算结果" size="small">
          <Paragraph style={{ marginBottom: 12 }}>
            基数 {result.base_bankroll}（{result.base_source}）· 每份{' '}
            {result.unit_amount} · 合计 {result.totals.units} 份 /{' '}
            {result.totals.amount} {result.amount_limits.currency}
            {result.capped ? <Tag color="orange">有截断</Tag> : null}
            {result.raised ? <Tag color="blue">有抬升到下限</Tag> : null}
          </Paragraph>
          <Paragraph type="secondary">
            剩余：{result.remaining_bankroll.start} → {result.remaining_bankroll.after}（
            {result.remaining_bankroll.source}）
          </Paragraph>
          <Table
            size="small"
            pagination={false}
            rowKey={(r) => String(r.index)}
            dataSource={result.items}
            columns={[
              { title: '#', dataIndex: 'index', width: 48 },
              { title: '比赛', dataIndex: 'match_id' },
              { title: '方案', dataIndex: 'strategy', width: 140 },
              {
                title: '请求份',
                dataIndex: 'stake_units_requested',
                width: 80,
              },
              { title: '实算份', dataIndex: 'stake_units', width: 80 },
              { title: '金额', dataIndex: 'amount', width: 90 },
              {
                title: '标记',
                width: 160,
                render: (_, row) => (
                  <Space size={4} wrap>
                    {row.capped ? <Tag color="orange">已截断</Tag> : null}
                    {row.raised ? <Tag color="blue">已抬升</Tag> : null}
                    {row.capped_amount ? <Tag>金额已截断</Tag> : null}
                    {row.error ? <Tag color="red">错误</Tag> : null}
                    {!row.stake_units ? <Tag>已跳过/0</Tag> : null}
                  </Space>
                ),
              },
            ]}
          />
        </Card>
      )}
    </Space>
  )
}
