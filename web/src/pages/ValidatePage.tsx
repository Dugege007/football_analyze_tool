import { HelpTip } from '../components/HelpTip'
import { Card, Empty, Typography } from 'antd'

const { Title } = Typography

/** M2+ 占位：验证缓存按指纹复用 */
export default function ValidatePage() {
  return (
    <Card>
      <Title level={3} style={{ marginTop: 0 }}>
        验证
        <HelpTip tip="这一页还在建设中，等后端的方案定义和验证缓存表定下来以后接入。同一方案、同一验证形式、同一数据指纹的验证会直接复用结果，不重新计算。" />
      </Title>
      <Empty description="这一页还在建设中。" />
    </Card>
  )
}
