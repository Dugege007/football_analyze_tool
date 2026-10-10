import { Alert, Segmented, Space, Tooltip, Typography } from 'antd'
import { DB_SOURCE_LABEL, REPLICA_BANNER, isUnpromotedReplica, type DbMeta, type DbSource } from './dataSource'

/** 数据源切换（现网 / 副本） */
export function DataSourceSwitch({ value, onChange }: { value: DbSource; onChange: (s: DbSource) => void }) {
  return (
    <Tooltip title="现网 = 8787；副本 = 8788 只读实例（v2d3，未晋升）。是否为副本以后端返回的标识为准">
      <Space size={4}>
        <Typography.Text>数据源</Typography.Text>
        <Segmented
          value={value}
          onChange={(v) => onChange(v as DbSource)}
          options={(['live', 'replica'] as DbSource[]).map((s) => ({ value: s, label: DB_SOURCE_LABEL[s] }))}
        />
      </Space>
    </Tooltip>
  )
}

/** 副本横条：只按 meta 判（db=v2d3 且 promoted=false） */
export function ReplicaBanner({ meta }: { meta: DbMeta | null | undefined }) {
  if (!isUnpromotedReplica(meta)) return null
  return (
    <Alert
      type="warning"
      showIcon
      banner
      style={{ borderRadius: 8, border: '1px solid #ffe58f' }}
      message={REPLICA_BANNER}
      description={meta?.readonly ? '只读实例：不能新建验证或方案' : undefined}
    />
  )
}
