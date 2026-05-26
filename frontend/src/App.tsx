import { BrowserRouter, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { Suspense, lazy, useEffect, useState } from 'react'
import { getPlatforms } from '@/lib/app-data'
import { apiFetch } from '@/lib/utils'
import { ActiveTaskProvider } from '@/context/ActiveTaskContext'
import { FloatingTaskButton } from '@/components/tasks/FloatingTaskButton'
import {
  ChevronDown,
  ChevronRight,
  CreditCard,
  Database,
  Globe,
  History,
  Inbox,
  LayoutDashboard,
  Moon,
  PlugZap,
  PlusCircle,
  Settings as SettingsIcon,
  Sun,
  Users,
} from 'lucide-react'

const Dashboard = lazy(() => import('@/pages/Dashboard'))
const Accounts = lazy(() => import('@/pages/Accounts'))
const GoogleAccountPool = lazy(() => import('@/pages/GoogleAccountPool'))
const CreditCardPool = lazy(() => import('@/pages/CreditCardPool'))
const OutlookMailboxPool = lazy(() => import('@/pages/OutlookMailboxPool'))
const Register = lazy(() => import('@/pages/Register'))
const Proxies = lazy(() => import('@/pages/Proxies'))
const Settings = lazy(() => import('@/pages/Settings'))
const TaskHistory = lazy(() => import('@/pages/TaskHistory'))
const TwoAPI = lazy(() => import('@/pages/TwoAPI'))

function navClass(isActive: boolean) {
  return [
    'sidebar-nav-item',
    isActive ? 'active' : '',
  ].join(' ')
}

function AccountsSubNav() {
  const location = useLocation()
  const isAccounts = location.pathname.startsWith('/accounts')
  const [open, setOpen] = useState(isAccounts)
  const [platforms, setPlatforms] = useState<{ key: string; label: string }[]>([])

  useEffect(() => {
    if (isAccounts) setOpen(true)
  }, [isAccounts])

  useEffect(() => {
    getPlatforms()
      .then((data) => setPlatforms((data || []).map((p: any) => ({ key: p.name, label: p.display_name }))))
      .catch(() => setPlatforms([]))
  }, [])

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`sidebar-nav-item w-full justify-between ${isAccounts ? 'active' : ''}`}
      >
        <span className="flex items-center gap-2.5">
          <Users className="h-4 w-4" />
          <span>账号资产</span>
        </span>
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
      </button>
      {open && (
        <div className="ml-2 mt-1 space-y-0.5">
          {platforms.map((p) => (
            <NavLink
              key={p.key}
              to={`/accounts/${p.key}`}
              className={({ isActive }) =>
                `sidebar-nav-item text-[13px] ${isActive ? 'active' : ''}`
              }
            >
              <span className="h-1.5 w-1.5 rounded-full bg-[var(--color-accent)]/70 flex-shrink-0" />
              <span>{p.label}</span>
            </NavLink>
          ))}
        </div>
      )}
    </div>
  )
}


type TwoAPIPluginNavItem = {
  key: string
  label: string
}

function formatTwoAPIPluginLabel(plugin: any) {
  const name = String(plugin?.name || '').trim()
  const display = String(plugin?.display_name || '').trim()
  if (name.toLowerCase() === 'zo') return 'Zo'
  return display || name || 'unknown'
}

function TwoAPISubNav() {
  const location = useLocation()
  const isTwoAPI = location.pathname.startsWith('/twoapi')
  const [open, setOpen] = useState(isTwoAPI)
  const [plugins, setPlugins] = useState<TwoAPIPluginNavItem[]>([{ key: 'zo', label: 'Zo' }])

  useEffect(() => {
    if (isTwoAPI) setOpen(true)
  }, [isTwoAPI])

  useEffect(() => {
    apiFetch('/2api/plugins')
      .then((data) => {
        const rows = Array.isArray(data?.items) ? data.items : []
        const mapped = rows
          .map((plugin: any) => ({ key: String(plugin?.name || '').trim(), label: formatTwoAPIPluginLabel(plugin) }))
          .filter((plugin: TwoAPIPluginNavItem) => plugin.key)
        setPlugins(mapped.length > 0 ? mapped : [{ key: 'zo', label: 'Zo' }])
      })
      .catch(() => setPlugins([{ key: 'zo', label: 'Zo' }]))
  }, [])

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`sidebar-nav-item w-full justify-between ${isTwoAPI ? 'active' : ''}`}
      >
        <span className="flex items-center gap-2.5">
          <PlugZap className="h-4 w-4" />
          <span>2API</span>
        </span>
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
      </button>
      {open && (
        <div className="ml-2 mt-1 space-y-0.5">
          {plugins.map((plugin) => (
            <NavLink
              key={plugin.key}
              to={`/twoapi/${plugin.key}`}
              className={({ isActive }) =>
                `sidebar-nav-item text-[13px] ${isActive ? 'active' : ''}`
              }
            >
              <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full bg-[var(--color-accent)]/70" />
              <span>{plugin.label}</span>
            </NavLink>
          ))}
        </div>
      )}
    </div>
  )
}

function Sidebar({ theme, toggleTheme }: { theme: string; toggleTheme: () => void }) {
  const isLight = theme === 'light'

  return (
    <aside className="app-sidebar">
      <div className="sidebar-inner">
        <div className="mb-3 flex items-center justify-between gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2">
          <span className="text-sm font-semibold text-[var(--color-text)]">控制台</span>
          <button
            type="button"
            onClick={toggleTheme}
            className="flex h-8 w-8 items-center justify-center rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text-secondary)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-text)]"
          >
            {isLight ? <Moon className="h-3.5 w-3.5" /> : <Sun className="h-3.5 w-3.5" />}
          </button>
        </div>

        <nav className="flex-1 space-y-5 overflow-y-auto">
          <section>
            <div className="sidebar-section-title mb-2">入口</div>
            <div className="space-y-1">
              <NavLink to="/" end className={({ isActive }) => navClass(isActive)}>
                <LayoutDashboard className="h-4 w-4" />
                <span>总览</span>
              </NavLink>
              <NavLink to="/register" className={({ isActive }) => navClass(isActive)}>
                <PlusCircle className="h-4 w-4" />
                <span>注册</span>
              </NavLink>
            </div>
          </section>

          <section>
            <div className="sidebar-section-title mb-2">资产</div>
            <div className="space-y-1">
              <AccountsSubNav />
              <NavLink to="/google-account-pool" className={({ isActive }) => navClass(isActive)}>
                <Database className="h-4 w-4" />
                <span>Google 账号池</span>
              </NavLink>
              <NavLink to="/outlook-mailbox-pool" className={({ isActive }) => navClass(isActive)}>
                <Inbox className="h-4 w-4" />
                <span>Outlook 邮箱池</span>
              </NavLink>
              <NavLink to="/credit-card-pool" className={({ isActive }) => navClass(isActive)}>
                <CreditCard className="h-4 w-4" />
                <span>信用卡池</span>
              </NavLink>
            </div>
          </section>

          <section>
            <div className="sidebar-section-title mb-2">系统</div>
            <div className="space-y-1">
              <NavLink to="/history" className={({ isActive }) => navClass(isActive)}>
                <History className="h-4 w-4" />
                <span>任务记录</span>
              </NavLink>
              <NavLink to="/proxies" className={({ isActive }) => navClass(isActive)}>
                <Globe className="h-4 w-4" />
                <span>代理资源</span>
              </NavLink>
              <TwoAPISubNav />
              <NavLink to="/settings" className={({ isActive }) => navClass(isActive)}>
                <SettingsIcon className="h-4 w-4" />
                <span>配置中心</span>
              </NavLink>
            </div>
          </section>
        </nav>
      </div>
    </aside>
  )
}

function Shell({ theme, toggleTheme }: { theme: string; toggleTheme: () => void }) {
  return (
    <div className="app-shell">
      <div className="app-window">
        <Sidebar theme={theme} toggleTheme={toggleTheme} />
        <main className="app-main">
          <Suspense fallback={<RouteFallback />}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/accounts" element={<Accounts />} />
              <Route path="/accounts/:platform" element={<Accounts />} />
              <Route path="/google-account-pool" element={<GoogleAccountPool />} />
              <Route path="/outlook-mailbox-pool" element={<OutlookMailboxPool />} />
              <Route path="/credit-card-pool" element={<CreditCardPool />} />
              <Route path="/register" element={<Register />} />
              <Route path="/history" element={<TaskHistory />} />
              <Route path="/proxies" element={<Proxies />} />
              <Route path="/twoapi" element={<TwoAPI />} />
              <Route path="/twoapi/:plugin" element={<TwoAPI />} />
              <Route path="/settings" element={<Settings />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </div>
  )
}

function RouteFallback() {
  return (
    <div className="flex min-h-[240px] items-center justify-center rounded-md border border-[var(--color-border)] bg-[var(--color-surface)]">
      <div className="text-sm text-[var(--color-text-secondary)]">加载中...</div>
    </div>
  )
}

export default function App() {
  const [theme, setTheme] = useState(() => localStorage.getItem('theme') || 'dark')

  useEffect(() => {
    document.documentElement.classList.toggle('light', theme === 'light')
    localStorage.setItem('theme', theme)
  }, [theme])

  const toggleTheme = () => setTheme((v) => (v === 'dark' ? 'light' : 'dark'))

  return (
    <BrowserRouter>
      <ActiveTaskProvider>
        <Shell theme={theme} toggleTheme={toggleTheme} />
        <FloatingTaskButton />
      </ActiveTaskProvider>
    </BrowserRouter>
  )
}
