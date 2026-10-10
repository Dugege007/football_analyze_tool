import { HelpTip } from '../components/HelpTip'
import { useEffect, useState } from 'react'
import { Card, Descriptions, Tag, Typography, message } from 'antd'
import { getBankrollConfig } from '../api/client'

const { Title } = Typography

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
        <HelpTip tip="这一页只读：只展示后端已经预留的配置项，不能修改，也不写入数据库。付费汇率源暂缓接入。" />
      </Title>
      <Descriptions bordered size="small" column={1}>
        <Descriptions.Item label="统计本金">
          {initialBankroll ?? '—'} {fxCurrency}
        </Descriptions.Item>
        <Descriptions.Item label="展示币种">
          <Tag>{fxCurrency}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="汇率表">
          {ratesEmpty ? '-' : '已有汇率数据'}
          <HelpTip tip="汇率换算还没有接入。" />
        </Descriptions.Item>
      </Descriptions>
    </Card>
  )
}
