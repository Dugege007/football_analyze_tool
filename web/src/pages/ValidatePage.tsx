import { Alert, Card, Typography } from 'antd'

const { Title, Paragraph } = Typography

/** M2+ 占位：验证缓存按指纹复用 */
export default function ValidatePage() {
  return (
    <Card>
      <Title level={3} style={{ marginTop: 0 }}>
        验证
      </Title>
      <Alert
        type="info"
        showIcon
        message="布局占位"
        description="等待后端方案定义 / 验证缓存表草案后接真页；同方案+同验证形式+同数据指纹命中不重算。"
        style={{ marginBottom: 16 }}
      />
      <Paragraph type="secondary" style={{ marginBottom: 0 }}>
        现网亚盘与预测列表不受影响。
      </Paragraph>
    </Card>
  )
}
