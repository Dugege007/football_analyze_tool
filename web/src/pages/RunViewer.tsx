import { LabelWithHelp } from '../components/HelpTip'
import { useState } from 'react'
import { Button, Card, Empty, InputNumber, Space, Table, Tag, Tooltip, Typography, message } from 'antd'
import { getValidationRun, type ValidationRunDetail } from '../api/client'
import { apiBase, DB_SOURCE_LABEL, type DbSource } from '../api/dataSource'
import {
  LEAK_SUSPECT_TAG,
  formatRoi,
  isSampleInsufficient,
  labelCompareStatus,
  labelShadowScheme,
  readLedgerNote,
  readLeakSuspect,
} from '../labels'
import { BasisTip } from './BasisTip'
import {
  CLOSE_BASIS_KEYS,
  CLOSE_BASIS_LABEL,
  OPEN_BASIS_KEYS,
  OPEN_BASIS_LABEL,
  closeBasisSplit,
  notEvaluableLines,
  openBasisSplit,
} from './compareSplit'

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** 只读查看已有验证记录（GET /strategies/runs/{编号}）；不新建任何记录，副本模式下也可用 */
export default function RunViewer({ source }: { source: DbSource }) {
  const [runId, setRunId] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [runs, setRuns] = useState<ValidationRunDetail[]>([])

  const onLoad = async () => {
    if (runId == null) return
    setLoading(true)
    try {
      const r = await getValidationRun(runId, apiBase(source))
      setRuns((prev) => [r, ...prev.filter((x) => x.id !== r.id)].slice(0, 8))
    } catch (e) {
      message.error(e instanceof Error ? e.message : '读取失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Card
      size="small"
      title={
        <LabelWithHelp
          label="查看已有验证记录"
          tip={`只读取已有的验证记录，不新建记录。当前数据源：${DB_SOURCE_LABEL[source]}。`}
        />
      }
    >
      <Space style={{ marginBottom: 8 }}>
        <Typography.Text>验证记录编号</Typography.Text>
        <InputNumber min={1} value={runId} onChange={(v) => setRunId(typeof v === 'number' ? v : null)} style={{ width: 120 }} />
        <Button onClick={() => void onLoad()} loading={loading} disabled={runId == null}>
          查看
        </Button>
        {runs.length ? (
          <Button type="link" onClick={() => setRuns([])}>
            清空
          </Button>
        ) : null}
      </Space>
      {runs.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="输入编号查看" />
      ) : (
        <Table<ValidationRunDetail>
          size="small"
          rowKey="id"
          pagination={false}
          dataSource={runs}
          scroll={{ x: true }}
          columns={[
            { title: '编号', dataIndex: 'id', width: 64 },
            {
              title: '方案',
              width: 200,
              render: (_, r) => {
                const s = r.summary ?? {}
                const ob = openBasisSplit(s)
                const cb = closeBasisSplit(s)
                const key = typeof s.strategy_key === 'string' ? s.strategy_key : null
                const zh = labelShadowScheme(key)
                const leak = readLeakSuspect(r, s)
                const note = readLedgerNote(r, s)
                return (
                  <Space size={4} wrap>
                    <span>{key ?? `方案 ${r.strategy_def_id ?? '—'}`}{zh ? ` · ${zh}` : ''}</span>
                    {leak.suspect ? (
                      <Tooltip title={leak.reason ?? '后端标记疑似泄漏'}>
                        <Tag style={{ marginInlineEnd: 0, color: '#8c8c8c' }}>{LEAK_SUSPECT_TAG}</Tag>
                      </Tooltip>
                    ) : null}
                    {note ? (
                      <Tooltip title={note}>
                        <Tag style={{ marginInlineEnd: 0 }}>台账备注</Tag>
                      </Tooltip>
                    ) : null}
                    {ob ? <BasisTip title="初盘口径" split={ob} keys={OPEN_BASIS_KEYS} labels={OPEN_BASIS_LABEL} /> : null}
                    {cb ? <BasisTip title="临盘口径" split={cb} keys={CLOSE_BASIS_KEYS} labels={CLOSE_BASIS_LABEL} /> : null}
                  </Space>
                )
              },
            },
            { title: '标签', dataIndex: 'run_label', width: 180, render: (v) => v ?? '—' },
            { title: '完成时间', dataIndex: 'finished_at', width: 150, render: (v) => v ?? '—' },
            { title: '状态', dataIndex: 'status', width: 80, render: (v: string) => <Tag>{labelCompareStatus(v)}</Tag> },
            { title: '场次', width: 64, render: (_, r) => num(r.summary?.n) ?? '—' },
            { title: '可评场次', width: 80, render: (_, r) => num(r.summary?.n_eligible) ?? '—' },
            {
              title: '不可评估',
              width: 80,
              render: (_, r) => {
                const n = num(r.summary?.n_not_evaluable)
                if (n == null) return '—'
                const lines = notEvaluableLines(r.summary)
                if (!lines) return n
                return (
                  <Tooltip title={<div>{lines.map((l) => <div key={l}>{l}</div>)}</div>}>
                    <span style={{ borderBottom: '1px dashed #bfbfbf', cursor: 'help' }}>{n}</span>
                  </Tooltip>
                )
              },
            },
            {
              title: '命中数',
              width: 64,
              render: (_, r) => {
                const leak = readLeakSuspect(r, r.summary)
                const v = num(r.summary?.hits) ?? '—'
                return leak.suspect ? (
                  <Tooltip title={`${LEAK_SUSPECT_TAG}${leak.reason ? `：${leak.reason}` : ''}`}>
                    <Typography.Text type="secondary">{v}</Typography.Text>
                  </Tooltip>
                ) : (
                  v
                )
              },
            },
            {
              title: '回报率',
              width: 140,
              render: (_, r) => {
                const v = num(r.summary?.roi)
                if (v == null) return '—'
                const leak = readLeakSuspect(r, r.summary)
                if (leak.suspect)
                  return (
                    <Tooltip title={`${LEAK_SUSPECT_TAG}${leak.reason ? `：${leak.reason}` : ''}`}>
                      <Typography.Text type="secondary">{formatRoi(v)}</Typography.Text>
                    </Tooltip>
                  )
                // L1：命中数低于 80 灰显「样本不足」
                if (isSampleInsufficient(r.summary?.hits))
                  return (
                    <Space size={4} wrap>
                      <Typography.Text type="secondary">{formatRoi(v)}</Typography.Text>
                      <Tag>样本不足</Tag>
                    </Space>
                  )
                return formatRoi(v)
              },
            },
          ]}
        />
      )}
    </Card>
  )
}
