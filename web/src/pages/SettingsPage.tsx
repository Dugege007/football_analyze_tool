import { useEffect, useState } from 'react'
import { Alert, Card, Descriptions, Tag, Typography, message } from 'antd'
import { getBankrollConfig } from '../api/client'

const { Title, Paragraph } = Typography

/** 布局占位：汇率/本金等配置展示，不写回 */
export default function SettingsPage() {
  const [fxCurrency, setFxCurrency] = useState('CNY')
  const [ratesEmpty, setRatesEmpty] = useState(true)
  const [initialBankroll, setInitialBankroll] = useState<number | null>(10000)

  useEffect(() => {
    getBankrollConfig()
      .then((res) => {
        const cur = res.data.items.fx_display_currency?.value?.currency
        if (typeof cur === 'string' && cur) setFxCurrency(cur)
        const rates = res.data.items.fx_rates?.value?.rates
        setRatesEmpty(!(rates && typeof rates === 'object' && Object.keys(rates as object).length > 0))
        const amt = res.data.items.stats_initial_bankroll?.value?.amount
        if (typeof amt === 'number') setInitialBankroll(amt)
      })
      .catch((e: Error) => message.warning(e.message))
  }, [])

  return (
    <Card>
      <Title level={3} style={{ marginTop: 0 }}>
        设置
      </Title>
      <Alert
        type="info"
        showIcon
        message="配置占位（只读）"
        description="付费汇率源暂缓；此处只展示后端已预留的键，不改入库。"
        style={{ marginBottom: 16 }}
      />
      <Descriptions bordered size="small" column={1}>
        <Descriptions.Item label="统计本金">
          {initialBankroll ?? '—'} {fxCurrency}
        </Descriptions.Item>
        <Descriptions.Item label="展示币种">
          <Tag>{fxCurrency}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="汇率表">
          {ratesEmpty ? '空表占位（换算未接）' : '已有汇率数据（换算未接）'}
        </Descriptions.Item>
      </Descriptions>
      <Paragraph type="secondary" style={{ marginTop: 16, marginBottom: 0 }}>
        非竞彩可查不默认分析 · 旧整点开赛兼容由后端保证。
      </Paragraph>
    </Card>
  )
}
