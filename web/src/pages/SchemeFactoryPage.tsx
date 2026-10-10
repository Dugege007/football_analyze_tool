import { HelpTip, LabelWithHelp } from '../components/HelpTip'
import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Card,
  Form,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  composeStrategy,
  listStrategies,
  listStrategyTemplates,
  validateStrategy,
  type StrategyDef,
  type StrategyTemplate,
  type ValidationRunResponse,
} from '../api/client'
import {
  labelReused,
  labelRunStatus,
  labelShadowScheme,
  labelStrategyStatus,
  labelTemplate,
} from '../labels'

const { Title, Text, Paragraph } = Typography

const statusColor: Record<string, string> = {
  active: 'success',
  shadow: 'processing',
  draft: 'default',
  archived: 'warning',
}

export default function SchemeFactoryPage() {
  const [loading, setLoading] = useState(false)
  const [items, setItems] = useState<StrategyDef[]>([])
  const [templates, setTemplates] = useState<StrategyTemplate[]>([])
  const [composing, setComposing] = useState(false)
  const [validatingId, setValidatingId] = useState<number | null>(null)
  const [lastValidate, setLastValidate] = useState<ValidationRunResponse | null>(null)
  const [form] = Form.useForm()

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [s, t] = await Promise.all([listStrategies(), listStrategyTemplates()])
      setItems(s.data.items)
      setTemplates(t.data.items)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const onCompose = async () => {
    try {
      const v = await form.validateFields()
      setComposing(true)
      const created = await composeStrategy({
        strategy_key: v.strategy_key,
        version: v.version,
        display_name: v.display_name || undefined,
        template: v.template,
        status: 'shadow',
      })
      message.success(`已生成方案 #${created.id} ${created.strategy_key}@${created.version}`)
      form.resetFields(['version', 'display_name'])
      await refresh()
    } catch (e) {
      if (e && typeof e === 'object' && 'errorFields' in e) return
      message.error(e instanceof Error ? e.message : '生成方案失败')
    } finally {
      setComposing(false)
    }
  }

  const onValidate = async (row: StrategyDef) => {
    setValidatingId(row.id)
    try {
      const run = await validateStrategy(row.id, {
        shadow: true,
        params: { window: 'ui-m3' },
      })
      setLastValidate(run)
      message.success(
        run.reused
          ? `复用缓存 · 验证记录 #${run.id}`
          : `新建验证 · 验证记录 #${run.id}`,
      )
    } catch (e) {
      message.error(e instanceof Error ? e.message : '验证失败')
    } finally {
      setValidatingId(null)
    }
  }

  const columns: ColumnsType<StrategyDef> = [
    {
      title: '编号',
      dataIndex: 'id',
      width: 64,
    },
    {
      title: '方案',
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text strong>
            {r.strategy_key}
            {r.is_default ? <Tag color="blue" style={{ marginLeft: 8 }}>默认</Tag> : null}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {labelShadowScheme(r.strategy_key, r.status) || r.display_name || '—'} · 版本 {r.version}
          </Text>
        </Space>
      ),
    },
    {
      title: '状态',
      width: 96,
      render: (_, r) => (
        <Tag color={statusColor[r.status] || 'default'}>
          {labelStrategyStatus(r.status)}
        </Tag>
      ),
    },
    {
      title: '状态机',
      width: 110,
      render: (_, r) => (
        <Text type="secondary">
          {r.config?.state_machine == null ? '无' : String(r.config.state_machine)}
        </Text>
      ),
    },
    {
      title: '指纹',
      ellipsis: true,
      render: (_, r) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {r.config_fingerprint ? `${r.config_fingerprint.slice(0, 12)}…` : '—'}
        </Text>
      ),
    },
    {
      title: '操作',
      width: 120,
      render: (_, r) => (
        <Button
          size="small"
          type="primary"
          loading={validatingId === r.id}
          onClick={() => void onValidate(r)}
        >
          验证
        </Button>
      ),
    },
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Title level={3} style={{ marginBottom: 4 }}>
          方案工场
          <HelpTip tip="可以列出方案、选模板生成新版本、做验证（复用缓存的结果或新建验证）。不影响现网的默认方案 CFFXDJ_5_V3。这里生成的方案默认是影子状态，只用于工场试跑。" />
        </Title>
      </div>

      <Card
        size="small"
        title={
          templates.length > 0 ? (
            <LabelWithHelp
              label="从模板生成方案"
              tip={templates.map((t) => `${labelTemplate(t.name)}：${t.description || '—'}`).join('\n')}
            />
          ) : (
            '从模板生成方案'
          )
        }
      >
        <Form
          form={form}
          layout="inline"
          initialValues={{ template: 'simple_gate', strategy_key: 'UI_COMPOSE' }}
          style={{ rowGap: 12 }}
        >
          <Form.Item
            name="strategy_key"
            label="方案标识"
            rules={[{ required: true, message: '必填' }]}
          >
            <Input style={{ width: 160 }} placeholder="方案标识" />
          </Form.Item>
          <Form.Item
            name="version"
            label="版本"
            rules={[{ required: true, message: '必填' }]}
          >
            <Input style={{ width: 140 }} placeholder="例如 2026.10.06b" />
          </Form.Item>
          <Form.Item name="template" label="模板" rules={[{ required: true }]}>
            <Select
              style={{ width: 200 }}
              options={templates.map((t) => ({
                value: t.name,
                label: labelTemplate(t.name),
              }))}
            />
          </Form.Item>
          <Form.Item name="display_name" label="显示名">
            <Input style={{ width: 140 }} placeholder="可选" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" loading={composing} onClick={() => void onCompose()}>
              生成方案
            </Button>
          </Form.Item>
          <Form.Item>
            <Button onClick={() => void refresh()}>刷新列表</Button>
          </Form.Item>
        </Form>
      </Card>

      {lastValidate && (
        <Card size="small" title="最近一次验证">
          <Space wrap>
            <Tag color={lastValidate.reused ? 'purple' : 'green'}>
              {labelReused(lastValidate.reused) ?? '—'}
            </Tag>
            <Text>
              验证记录 #{lastValidate.id} · 方案定义 #{lastValidate.strategy_def_id} · 状态{' '}
              {labelRunStatus(lastValidate.status)}
            </Text>
          </Space>
          <Paragraph
            style={{
              marginTop: 8,
              marginBottom: 0,
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              fontSize: 12,
              whiteSpace: 'pre-wrap',
            }}
          >
            {JSON.stringify(lastValidate.summary ?? {}, null, 2)}
          </Paragraph>
        </Card>
      )}

      <Card size="small" title="已登记方案" styles={{ body: { padding: 0 } }}>
        <Table
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={items}
          pagination={false}
          size="middle"
        />
      </Card>
    </Space>
  )
}
