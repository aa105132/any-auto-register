import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { NavLink, useParams } from 'react-router-dom'
import {
  Activity,
  Copy,
  KeyRound,
  ListChecks,
  Plus,
  Power,
  RefreshCw,
  Server,
  SlidersHorizontal,
  TerminalSquare,
  Trash2,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { apiFetch } from '@/lib/utils'

type TwoAPIPluginStatus = {
  name: string
  display_name?: string
  enabled?: boolean
  account_count?: number
  available_count?: number
  accounts?: any[]
  settings?: Record<string, any>
  recent_logs?: string[]
}

type TwoAPITabKey = 'accounts' | 'settings' | 'keys' | 'logs'

type PluginSettingKey =
  | 'enabled'
  | 'min_credit'
  | 'auto_wake'
  | 'auto_refill'
  | 'request_timeout'
  | 'wake_timeout'
  | 'max_retries'
  | 'keepalive_space_fallback'
  | 'minimize_ask_context'

type PluginSettingField =
  | { key: PluginSettingKey; type: 'toggle'; label: string; helper?: string }
  | { key: PluginSettingKey; type: 'number'; label: string; helper?: string; fallback: number }

const COMMON_SETTING_FIELDS: PluginSettingField[] = [
  { key: 'enabled', type: 'toggle', label: '启用 2API' },
  { key: 'auto_refill', type: 'toggle', label: '自动补号', helper: '账号池为空或全部不可用时，后台触发现有注册脚本。' },
  { key: 'min_credit', type: 'number', label: '最低余额阈值', fallback: 1 },
  { key: 'request_timeout', type: 'number', label: '请求超时秒数', fallback: 90 },
  { key: 'max_retries', type: 'number', label: '最大重试次数', fallback: 2 },
]

const PLUGIN_SETTING_FIELDS: Record<string, PluginSettingField[]> = {
  zo: [
    { key: 'enabled', type: 'toggle', label: '启用 Zo 2API' },
    { key: 'auto_wake', type: 'toggle', label: '自动唤醒', helper: 'Space 睡眠时，先尝试平台恢复接口，再轮询 models。' },
    { key: 'keepalive_space_fallback', type: 'toggle', label: '保活 Space 兼容代理', helper: '默认关闭；仅在需要旧 *.zo.space OpenAI 代理 fallback 时启用。' },
    { key: 'minimize_ask_context', type: 'toggle', label: '降低 /ask 上下文', helper: '自动启用空 scopes 的极简 persona，可减少 Zo 固定工具上下文 token；失败时自动回退。' },
    { key: 'auto_refill', type: 'toggle', label: '自动补号', helper: '账号池为空或全部不可用时，后台触发现有 Zo 注册脚本。' },
    { key: 'min_credit', type: 'number', label: '最低余额阈值', fallback: 1 },
    { key: 'request_timeout', type: 'number', label: '请求超时秒数', fallback: 90 },
    { key: 'wake_timeout', type: 'number', label: '唤醒超时秒数', fallback: 60 },
    { key: 'max_retries', type: 'number', label: '最大重试次数', fallback: 2 },
  ],
  swarms: [
    { key: 'enabled', type: 'toggle', label: '启用 Swarms 2API' },
    { key: 'auto_refill', type: 'toggle', label: '自动补号', helper: '账号池为空或全部不可用时，后台触发 Swarms 注册任务。' },
    { key: 'min_credit', type: 'number', label: '最低余额阈值', fallback: 1 },
    { key: 'request_timeout', type: 'number', label: '请求超时秒数', fallback: 90 },
    { key: 'max_retries', type: 'number', label: '最大重试次数', fallback: 2 },
  ],
}

const TAB_ITEMS: Array<{ key: TwoAPITabKey; label: string; description: string; icon: any }> = [
  { key: 'accounts', label: '账号', description: '轮询候选、余额与可用状态', icon: ListChecks },
  { key: 'settings', label: '设置', description: '运行策略、自动补号与远端推送', icon: SlidersHorizontal },
  { key: 'keys', label: '密钥', description: '给外部客户端使用的 API Key', icon: KeyRound },
  { key: 'logs', label: '日志', description: '插件运行与调用记录', icon: TerminalSquare },
]

function copyText(value: string) {
  if (!value) return
  navigator.clipboard?.writeText(value).catch(() => undefined)
}

function pluginLabel(plugin: TwoAPIPluginStatus | null | undefined, fallback: string) {
  const name = String(plugin?.name || fallback || '').trim()
  if (name.toLowerCase() === 'zo') return 'Zo'
  return String(plugin?.display_name || name || '未知插件').trim()
}

function pluginBaseUrl(plugin: string, listen?: string) {
  if (plugin === 'zo') return listen || 'http://127.0.0.1:6543/zo/v1'
  return `http://127.0.0.1:6543/${plugin}/v1`
}

function formatStatusText(account: any) {
  const raw = String(account?.last_status || '').trim()
  if (raw === 'proxy_missing') return '未部署代理'
  if (raw) return raw
  if (account?.enabled && account?.credit_ok) return '可用'
  return '不可用'
}

function SettingToggle({ label, checked, onChange, helper }: { label: string; checked: boolean; onChange: (v: boolean) => void; helper?: string }) {
  return (
    <label className="group flex items-start justify-between gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-3.5 py-3 transition-colors hover:border-[var(--color-accent)]/45 hover:bg-[var(--color-surface-hover)]">
      <span>
        <span className="block text-sm font-medium text-[var(--color-text)]">{label}</span>
        {helper ? <span className="mt-1 block text-xs leading-5 text-[var(--color-text-secondary)]">{helper}</span> : null}
      </span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} className="mt-1 h-4 w-4 accent-[var(--color-accent)]" />
    </label>
  )
}

function EmptyPanel({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-[var(--color-border)] bg-[var(--color-surface)]/60 px-4 py-8 text-center text-sm text-[var(--color-text-secondary)]">
      {children}
    </div>
  )
}

export default function TwoAPI() {
  const params = useParams()
  const selectedPlugin = String(params.plugin || 'zo').trim() || 'zo'
  const [activeTab, setActiveTab] = useState<TwoAPITabKey>('accounts')
  const [status, setStatus] = useState<any>(null)
  const [keys, setKeys] = useState<any[]>([])
  const [logs, setLogs] = useState<string[]>([])
  const [serverStatus, setServerStatus] = useState<any>(null)
  const [serverStarting, setServerStarting] = useState(false)
  const [note, setNote] = useState('')
  const [remoteUrl, setRemoteUrl] = useState('')
  const [pushLatestOnly, setPushLatestOnly] = useState(true)
  const [pushingRemote, setPushingRemote] = useState(false)
  const [remotePushResult, setRemotePushResult] = useState('')
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [settings, setSettings] = useState<any>({})

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const [statusData, keyData, logData, settingData] = await Promise.all([
        apiFetch('/2api/status'),
        apiFetch('/2api/keys'),
        apiFetch(`/2api/logs?plugin=${encodeURIComponent(selectedPlugin)}&limit=240`),
        apiFetch(`/2api/plugins/${encodeURIComponent(selectedPlugin)}/settings`),
      ])
      setStatus(statusData)
      setServerStatus(statusData?.server || null)
      setKeys(keyData.items || [])
      setLogs(logData.items || [])
      setSettings(settingData || {})
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载 2API 插件失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load().catch(() => undefined) }, [selectedPlugin])

  const plugins: TwoAPIPluginStatus[] = status?.plugins || []
  const selectedPluginStatus = plugins.find((item: any) => item.name === selectedPlugin) || null
  const currentLabel = pluginLabel(selectedPluginStatus, selectedPlugin)
  const canPushRemote = selectedPlugin === 'zo' || selectedPlugin === 'swarms'
  const canRecoverPlugin = selectedPlugin === 'zo'
  const serverListen = serverStatus?.listen || status?.server?.listen || status?.listen
  const baseUrl = pluginBaseUrl(selectedPlugin, serverListen)
  const availableCount = Number(selectedPluginStatus?.available_count || 0)
  const accountCount = Number(selectedPluginStatus?.account_count || 0)
  const pluginKeys = keys.filter((item) => String(item.plugin || '') === selectedPlugin)
  const accounts = selectedPluginStatus?.accounts || []
  const serverRunning = Boolean(serverStatus?.running)
  const settingFields = PLUGIN_SETTING_FIELDS[selectedPlugin] || COMMON_SETTING_FIELDS
  const toggleSettingFields = settingFields.filter((field) => field.type === 'toggle')
  const numberSettingFields = settingFields.filter((field) => field.type === 'number')
  const activeTabMeta = TAB_ITEMS.find((item) => item.key === activeTab) || TAB_ITEMS[0]
  const ActiveTabIcon = activeTabMeta.icon

  const metricCards = useMemo(() => [
    { label: '可用账号', value: `${availableCount}/${accountCount}`, note: '跳过空额度或不可用账号', icon: Activity, tone: availableCount > 0 ? 'text-emerald-400' : 'text-red-400' },
    { label: 'API Key', value: pluginKeys.length, note: '外部访问密钥数量', icon: KeyRound, tone: 'text-[var(--color-accent)]' },
    { label: '2API 服务', value: serverRunning ? '运行中' : '未启动', note: '本地监听 127.0.0.1:6543', icon: Power, tone: serverRunning ? 'text-emerald-400' : 'text-red-400' },
  ], [accountCount, availableCount, pluginKeys.length, serverRunning])

  const createKey = async () => {
    const row = await apiFetch('/2api/keys', { method: 'POST', body: JSON.stringify({ plugin: selectedPlugin, note }) })
    setNote('')
    await load()
    if (row.key) copyText(row.key)
  }

  const deleteKey = async (id: string) => {
    await apiFetch(`/2api/keys/${id}`, { method: 'DELETE' })
    await load()
  }

  const buildSettingsPayload = () => {
    const allowedKeys = new Set(settingFields.map((field) => field.key))
    return Object.fromEntries(Object.entries(settings).filter(([key]) => allowedKeys.has(key as PluginSettingKey)))
  }

  const saveSettings = async () => {
    setSaving(true)
    try {
      await apiFetch(`/2api/plugins/${encodeURIComponent(selectedPlugin)}/settings`, { method: 'POST', body: JSON.stringify(buildSettingsPayload()) })
      await load()
    } finally {
      setSaving(false)
    }
  }

  const refreshCredits = async () => {
    await apiFetch(`/2api/plugins/${encodeURIComponent(selectedPlugin)}/refresh-credits`, { method: 'POST' })
    await load()
  }

  const recoverPlugin = async () => {
    await apiFetch(`/2api/plugins/${encodeURIComponent(selectedPlugin)}/recover`, { method: 'POST' })
    await load()
  }

  const startServer = async () => {
    setServerStarting(true)
    setError('')
    try {
      const result = await apiFetch('/2api/server/start', { method: 'POST' })
      setServerStatus(result)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '启动 2API 服务失败')
    } finally {
      setServerStarting(false)
    }
  }

  const pushRemote = async () => {
    if (!remoteUrl.trim()) {
      setError('请先填写远端后端地址')
      return
    }
    setPushingRemote(true)
    setError('')
    setRemotePushResult('')
    try {
      const result = await apiFetch(`/2api/plugins/${encodeURIComponent(selectedPlugin)}/push`, {
        method: 'POST',
        body: JSON.stringify({ target_url: remoteUrl.trim(), latest_only: pushLatestOnly, source: 'frontend-remote-push' }),
      })
      setRemotePushResult(`已推送 ${result?.pushed ?? 0} 个 ${currentLabel} 账号到远端`)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '推送到远端 Linux 失败')
    } finally {
      setPushingRemote(false)
    }
  }

  return (
    <div className="page-enter space-y-4">
      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-5 shadow-sm">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="min-w-0 space-y-3">
            <div className="workspace-kicker">系统 / 2API / {currentLabel}</div>
            <div>
              <h1 className="text-[1.65rem] font-semibold tracking-[-0.04em] text-[var(--color-text)]">2API 代理控制台</h1>
              <p className="mt-2 max-w-[72ch] text-sm leading-6 text-[var(--color-text-secondary)]">
                左侧选择插件，右侧按账号、设置、密钥、日志拆分操作。避免所有配置堆在同一屏里，排查和接入都更快。
              </p>
            </div>
            <div className="toolbar-strip">
              <Badge variant="default">{currentLabel}</Badge>
              <Badge variant={availableCount > 0 ? 'success' : 'danger'}>可用 {availableCount}/{accountCount}</Badge>
              <Badge variant="secondary">Key {pluginKeys.length}</Badge>
              <Badge variant={serverRunning ? 'success' : 'danger'}>{serverRunning ? '服务运行中' : '服务未启动'}</Badge>
              {loading ? <Badge variant="warning">刷新中</Badge> : null}
            </div>
            {error ? <div className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div> : null}
          </div>

          <div className="w-full rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4 xl:max-w-[520px]">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="workspace-kicker">客户端填写</div>
                <div className="mt-2 break-all font-mono text-xs leading-5 text-[var(--color-text-secondary)]">{baseUrl}</div>
              </div>
              <Server className="h-5 w-5 shrink-0 text-[var(--color-text-muted)]" />
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              <Button variant="outline" size="sm" onClick={() => copyText(baseUrl)}>
                <Copy className="mr-1.5 h-4 w-4" />复制 Base URL
              </Button>
              <Button variant={serverRunning ? 'outline' : 'default'} size="sm" onClick={startServer} disabled={serverStarting}>
                <Power className="mr-1.5 h-4 w-4" />{serverStarting ? '启动中...' : '启动 2API'}
              </Button>
            </div>
          </div>
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-[300px_minmax(0,1fr)]">
        <aside className="space-y-4">
          <Card className="sticky top-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-4">
            <div className="mb-3 flex items-center justify-between gap-2">
              <div>
                <div className="workspace-kicker">插件菜单</div>
                <div className="mt-1 text-sm font-semibold text-[var(--color-text)]">选择代理来源</div>
              </div>
              <Button variant="ghost" size="icon" onClick={load}>
                <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              </Button>
            </div>

            <div className="space-y-1.5">
              {(plugins.length ? plugins : [{ name: selectedPlugin, display_name: currentLabel }]).map((plugin: any) => {
                const key = String(plugin.name || '').trim()
                const label = pluginLabel(plugin, key)
                const isActive = key === selectedPlugin
                const pluginAvailable = Number(plugin.available_count || 0)
                const pluginTotal = Number(plugin.account_count || 0)
                return (
                  <NavLink
                    key={key}
                    to={`/twoapi/${key}`}
                    className={`block rounded-lg border px-3 py-3 transition-colors ${isActive ? 'border-[var(--color-accent)]/55 bg-[var(--color-accent-muted)]' : 'border-transparent bg-transparent hover:border-[var(--color-border)] hover:bg-[var(--color-surface-hover)]'}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-[var(--color-text)]">{label}</div>
                        <div className="mt-1 text-[11px] text-[var(--color-text-muted)]">{key}</div>
                      </div>
                      <Badge variant={pluginAvailable > 0 ? 'success' : 'secondary'}>{pluginAvailable}/{pluginTotal}</Badge>
                    </div>
                  </NavLink>
                )
              })}
            </div>

            <div className="mt-5 border-t border-[var(--color-border)] pt-4">
              <div className="workspace-kicker mb-2">{currentLabel} 子菜单</div>
              <div className="space-y-1">
                {TAB_ITEMS.map(({ key, label, icon: Icon }) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setActiveTab(key)}
                    className={`flex w-full items-center justify-between rounded-lg px-3 py-2.5 text-left text-sm transition-colors ${activeTab === key ? 'bg-[var(--color-surface-hover)] text-[var(--color-text)]' : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-text)]'}`}
                  >
                    <span className="flex items-center gap-2.5"><Icon className="h-4 w-4" />{label}</span>
                    {key === 'accounts' ? <span className="text-xs tabular-nums">{accountCount}</span> : null}
                    {key === 'keys' ? <span className="text-xs tabular-nums">{pluginKeys.length}</span> : null}
                    {key === 'logs' ? <span className="text-xs tabular-nums">{logs.length}</span> : null}
                  </button>
                ))}
              </div>
            </div>
          </Card>
        </aside>

        <main className="space-y-4">
          <section className="grid gap-3 md:grid-cols-3">
            {metricCards.map(({ label, value, note, icon: Icon, tone }) => (
              <div key={label} className="workspace-metric-panel">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="workspace-kicker">{label}</div>
                    <div className="workspace-metric-value tabular-nums">{value}</div>
                    <div className="mt-2 text-xs leading-5 text-[var(--color-text-secondary)]">{note}</div>
                  </div>
                  <div className="workspace-metric-icon"><Icon className={`h-5 w-5 ${tone}`} /></div>
                </div>
              </div>
            ))}
          </section>

          <Card className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-0">
            <div className="border-b border-[var(--color-border)] px-5 py-4">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
                <div>
                  <div className="workspace-kicker">{currentLabel}</div>
                  <div className="mt-1 flex items-center gap-2 text-lg font-semibold text-[var(--color-text)]">
                    <ActiveTabIcon className="h-5 w-5 text-[var(--color-accent)]" />
                    {activeTabMeta.label}
                  </div>
                  <p className="mt-1 text-xs text-[var(--color-text-secondary)]">{activeTabMeta.description}</p>
                </div>
                <div className="flex flex-wrap gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-1">
                  {TAB_ITEMS.map(({ key, label }) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => setActiveTab(key)}
                      className={`rounded-md px-3 py-1.5 text-sm transition-colors ${activeTab === key ? 'bg-[var(--color-accent)] text-white' : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-text)]'}`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <div className="p-5">
              {activeTab === 'accounts' && (
                <div className="space-y-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="text-sm text-[var(--color-text-secondary)]">账号池决定 OpenAI 兼容代理实际轮询到哪个上游账号。</div>
                    <div className="flex gap-2">
                      <Button variant="outline" size="sm" onClick={refreshCredits}>刷新余额</Button>
                      {canRecoverPlugin ? <Button variant="outline" size="sm" onClick={recoverPlugin}>唤醒/恢复</Button> : null}
                    </div>
                  </div>
                  <div className="glass-table-wrap workspace-table-scroll rounded-xl border border-[var(--color-border)]">
                    <table className="workspace-table min-w-[760px] w-full text-sm">
                      <thead><tr><th className="px-4 py-3 text-left">邮箱</th><th className="px-4 py-3 text-left">余额</th><th className="px-4 py-3 text-left">状态</th><th className="px-4 py-3 text-left">Base URL</th></tr></thead>
                      <tbody>
                        {accounts.map((account: any) => (
                          <tr key={`${account.email}-${account.base_url_preview}`} className="border-b border-[var(--color-border)]/40">
                            <td className="px-4 py-3 text-xs">{account.email || '-'}</td>
                            <td className="px-4 py-3 tabular-nums">{account.enabled ? (account.credit_amount ?? '-') : '-'}</td>
                            <td className="px-4 py-3"><Badge variant={account.enabled && account.credit_ok ? 'success' : 'danger'}>{formatStatusText(account)}</Badge></td>
                            <td className="px-4 py-3 font-mono text-xs text-[var(--color-text-secondary)]">{account.base_url_preview || '-'}</td>
                          </tr>
                        ))}
                        {accounts.length === 0 ? <tr><td colSpan={4} className="px-4 py-8"><EmptyPanel>未发现 {currentLabel} 代理账号。注册完成后会自动写入对应插件账号池。</EmptyPanel></td></tr> : null}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {activeTab === 'settings' && (
                <div className="grid gap-4 2xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
                  <div className="space-y-3">
                    <div>
                      <div className="workspace-kicker">运行策略</div>
                      <div className="mt-1 text-base font-semibold text-[var(--color-text)]">插件行为</div>
                    </div>
                    {toggleSettingFields.map((field) => (
                      <SettingToggle
                        key={field.key}
                        label={field.key === 'enabled' && field.label === '启用 2API' ? `启用 ${currentLabel} 2API` : field.label}
                        helper={field.helper}
                        checked={Boolean(settings[field.key])}
                        onChange={(value) => setSettings((previous: any) => ({ ...previous, [field.key]: value }))}
                      />
                    ))}
                    <div className="grid gap-3 sm:grid-cols-2">
                      {numberSettingFields.map((field) => (
                        <label key={field.key} className="block space-y-2">
                          <span className="workspace-kicker">{field.label}</span>
                          <input
                            type="number"
                            value={settings[field.key] ?? field.fallback}
                            onChange={(event) => setSettings((previous: any) => ({ ...previous, [field.key]: Number(event.target.value || field.fallback) }))}
                            className="control-surface"
                          />
                          {field.helper ? <span className="block text-xs text-[var(--color-text-secondary)]">{field.helper}</span> : null}
                        </label>
                      ))}
                    </div>
                    <Button onClick={saveSettings} disabled={saving} className="w-full">
                      {saving ? '保存中...' : '保存设置'}
                    </Button>
                  </div>

                  <div className="space-y-3">
                    <div>
                      <div className="workspace-kicker">远端后端</div>
                      <div className="mt-1 text-base font-semibold text-[var(--color-text)]">推送到远端 Linux</div>
                      <p className="mt-2 text-xs leading-5 text-[var(--color-text-secondary)]">
                        将本机 {currentLabel} 注册结果推送到另一台 Linux 后端的 2API 导入接口，适合本地浏览器登录、远端服务器运行代理。
                      </p>
                    </div>
                    {canPushRemote ? (
                      <div className="space-y-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
                        <input value={remoteUrl} onChange={(event) => setRemoteUrl(event.target.value)} placeholder="http://1.2.3.4:8000 或 https://backend.example.com" className="control-surface" />
                        <SettingToggle label="只推送最新账号" checked={pushLatestOnly} onChange={setPushLatestOnly} />
                        <Button onClick={pushRemote} disabled={pushingRemote} className="w-full">
                          {pushingRemote ? '推送中...' : '推送到远端 Linux'}
                        </Button>
                        {remotePushResult ? <div className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">{remotePushResult}</div> : null}
                      </div>
                    ) : (
                      <EmptyPanel>{currentLabel} 暂未声明远端推送入口。</EmptyPanel>
                    )}
                  </div>
                </div>
              )}

              {activeTab === 'keys' && (
                <div className="space-y-4">
                  <div className="flex flex-col gap-2 md:flex-row">
                    <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="备注，例如 RikkaHub" className="control-surface" />
                    <Button onClick={createKey}><Plus className="mr-1.5 h-4 w-4" />创建密钥</Button>
                  </div>
                  {pluginKeys.length === 0 ? <EmptyPanel>还没有 {currentLabel} 2API key，创建后给 RikkaHub / 酒馆使用。</EmptyPanel> : null}
                  <div className="space-y-2">
                    {pluginKeys.map((item) => (
                      <div key={item.id} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div>
                            <div className="font-mono text-xs text-[var(--color-text-secondary)]">{item.key_preview}</div>
                            <div className="mt-1 text-xs text-[var(--color-text-muted)]">{item.note || '无备注'} · {item.plugin}</div>
                          </div>
                          <div className="flex gap-2">
                            <button className="table-action-btn" onClick={() => copyText(item.key)}><Copy className="mr-1 h-4 w-4" />复制</button>
                            <button className="table-action-btn table-action-btn-danger" onClick={() => deleteKey(item.id)}><Trash2 className="mr-1 h-4 w-4" />删除</button>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {activeTab === 'logs' && (
                <div className="space-y-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="text-sm text-[var(--color-text-secondary)]">最多显示最近 240 条插件日志。</div>
                    <Button variant="outline" size="sm" onClick={load}><RefreshCw className="mr-1.5 h-4 w-4" />刷新日志</Button>
                  </div>
                  <pre className="max-h-[620px] overflow-auto rounded-xl border border-[var(--color-border)] bg-black/30 p-4 text-xs leading-5 text-[var(--color-text-secondary)]">
                    {logs.length ? logs.join('\n') : '暂无日志'}
                  </pre>
                </div>
              )}
            </div>
          </Card>
        </main>
      </div>
    </div>
  )
}
