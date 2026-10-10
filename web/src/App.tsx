import { ConfigProvider, Layout, Menu, Spin, Typography, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom'
import MatchListPage from './pages/MatchListPage'
import MatchDetailPage from './pages/MatchDetailPage'
import BankrollCalcPage from './pages/BankrollCalcPage'
import PredictionsPage from './pages/PredictionsPage'
import SettingsPage from './pages/SettingsPage'
import SchemeFactoryPage from './pages/SchemeFactoryPage'
import ValidatePage from './pages/ValidatePage'
import ComparePage from './pages/ComparePage'
import dayjs from 'dayjs'
import { Suspense, lazy } from 'react'
import 'dayjs/locale/zh-cn'

dayjs.locale('zh-cn')

// 数据表页依赖 AG Grid，按需加载，避免拖慢其它页面
const SheetPage = lazy(() => import('./pages/SheetPage'))

const { Header, Content } = Layout
const { Text } = Typography

function navKey(pathname: string): string {
  if (pathname.startsWith('/bankroll')) return 'bankroll'
  if (pathname.startsWith('/predictions')) return 'predictions'
  if (pathname.startsWith('/settings')) return 'settings'
  if (pathname.startsWith('/schemes')) return 'schemes'
  if (pathname.startsWith('/validate')) return 'validate'
  if (pathname.startsWith('/compare')) return 'compare'
  if (pathname.startsWith('/sheet')) return 'sheet'
  if (pathname.startsWith('/matches')) return 'matches'
  return 'matches'
}

function AppShell() {
  const location = useLocation()
  const selected = navKey(location.pathname)

  return (
    <Layout style={{ minHeight: '100vh', background: '#f5f7fb' }}>
      <Header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          background: '#001529',
          padding: '0 16px',
        }}
      >
        <Text style={{ color: '#fff', fontSize: 16, fontWeight: 600, whiteSpace: 'nowrap' }}>
          比赛分析工具
        </Text>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[selected]}
          style={{ flex: 1, minWidth: 0, background: 'transparent' }}
          items={[
            { key: 'matches', label: <Link to="/">赛程</Link> },
            { key: 'predictions', label: <Link to="/predictions">预测</Link> },
            { key: 'sheet', label: <Link to="/sheet">数据表</Link> },
            { key: 'bankroll', label: <Link to="/bankroll">注额</Link> },
            { key: 'schemes', label: <Link to="/schemes">方案</Link> },
            { key: 'validate', label: <Link to="/validate">验证</Link> },
            { key: 'compare', label: <Link to="/compare">对比</Link> },
            { key: 'settings', label: <Link to="/settings">设置</Link> },
          ]}
        />
      </Header>
      <Content
        style={{
          padding: '24px 24px 48px',
          // 赛程页展开五个预测方向列后需要更宽的版面，其他页面保持 1100 像素。
          maxWidth: location.pathname === '/' ? 1560 : 1100,
          width: '100%',
          margin: '0 auto',
        }}
      >
        <Routes>
          <Route path="/" element={<MatchListPage />} />
          <Route path="/matches/:id" element={<MatchDetailPage />} />
          <Route path="/predictions" element={<PredictionsPage />} />
          <Route path="/bankroll" element={<BankrollCalcPage />} />
          <Route path="/schemes" element={<SchemeFactoryPage />} />
          <Route path="/validate" element={<ValidatePage />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route
            path="/sheet"
            element={
              <Suspense fallback={<Spin style={{ display: 'block', margin: 80 }} />}>
                <SheetPage />
              </Suspense>
            }
          />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Content>
    </Layout>
  )
}

export default function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: '#1677ff',
          borderRadius: 8,
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
        },
      }}
    >
      <BrowserRouter>
        <AppShell />
      </BrowserRouter>
    </ConfigProvider>
  )
}
