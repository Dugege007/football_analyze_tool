import type { ReactNode } from 'react'
import { Tooltip } from 'antd'
import { QuestionCircleOutlined } from '@ant-design/icons'

/**
 * 界面规范（见 web/UI-GUIDELINES.md）：注释性说明不直接写在页面上，
 * 需要解释的栏目或标签旁放一个小问号，鼠标悬停时用 Tooltip 显示说明。
 */
export function HelpTip({ tip }: { tip: ReactNode }) {
  return (
    <Tooltip title={<div style={{ whiteSpace: 'pre-line' }}>{tip}</div>}>
      <QuestionCircleOutlined
        aria-label="说明"
        style={{ marginLeft: 4, color: 'rgba(0,0,0,0.45)', cursor: 'help', fontSize: 13 }}
        onClick={(e) => e.stopPropagation()}
      />
    </Tooltip>
  )
}

/** 标签文字加问号说明。 */
export function LabelWithHelp({ label, tip }: { label: ReactNode; tip: ReactNode }) {
  return (
    <span style={{ whiteSpace: 'nowrap' }}>
      {label}
      <HelpTip tip={tip} />
    </span>
  )
}

export default HelpTip
