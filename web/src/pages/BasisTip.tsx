import { Tooltip, Typography } from 'antd'
import { formatRoi, isSampleInsufficient } from '../labels'

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** 按初盘 / 临盘口径分账的悬停（仅对照，不单独成行）；api_closing 组带 ledger_note 时附在该行后 */
export function BasisTip({
  title,
  split,
  keys,
  labels,
}: {
  title: string
  split: Record<string, Record<string, unknown>>
  keys: readonly string[]
  labels: Record<string, string>
}) {
  const all = [...keys, ...Object.keys(split).filter((k) => !keys.includes(k))]
  return (
    <Tooltip
      title={
        <div>
          <div>按{title}分（仅对照，不单独成行）：</div>
          {all.map((k) => {
            const v = split[k]
            if (!v) return null
            const roi = num(v.roi)
            const note = typeof v.ledger_note === 'string' && v.ledger_note ? v.ledger_note : null
            return (
              <div key={k}>
                {labels[k] ?? k}：场次 {num(v.n) ?? 0}
                {num(v.n_eligible) != null ? ` · 可评 ${num(v.n_eligible)}` : ''} · 命中 {num(v.hits) ?? 0} · 回报率{' '}
                {formatRoi(roi)}
                {isSampleInsufficient(v.hits) && roi != null ? '（样本不足）' : ''}
                {note ? `；备注：${note}` : ''}
              </div>
            )
          })}
        </div>
      }
    >
      <Typography.Text type="secondary" style={{ fontSize: 12, borderBottom: '1px dashed #bfbfbf', cursor: 'help' }}>
        {title}
      </Typography.Text>
    </Tooltip>
  )
}
