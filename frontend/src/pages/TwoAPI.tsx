import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'
import {
  Activity,
  Copy,
  KeyRound,
  ListChecks,
  Plus,
  Power,
  RefreshCw,
  Server,
  TerminalSquare,
  Trash2,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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

type TwoAPITabKey = 'accounts' | 'keys' | 'logs'

type PluginSettingKey = 'enabled' | 'min_credit' | 'auto_refill'

type PluginSettingField =
  | { key: PluginSettingKey; type: 'toggle'; label: string; helper?: string }
  | { key: PluginSettingKey; type: 'number'; label: string; helper?: string; fallback: number }

// 只保留真正有用的开关：启用、自动补号、最低余额阈值。
// 删除 request_timeout / max_retries / wake_timeout / auto_wake 等死开关。
const SETTING_FIELDS: PluginSettingField[] = [
  { key: 'enabled', type: 'toggle', label: '启用 2API' },
  { key: 'auto_refill', type: 'toggle', label: '自动补号', helper: '账号池为空或全部不可用时，后台触发现有注册脚本。' },
  { key: 'min_credit', type: 'number', label: '最低余额阈值', fallback: 1 },
]

const TAB_ITEMS: Array<{ key: TwoAPITabKey; label: string; icon: any }> = [
  { key: 'accounts', label: '账号', icon: ListChecks },
  { key: 'keys', label: '密钥', icon: KeyRound },
  { key: 'logs', label: '日志', icon: TerminalSquare },
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
  return listen || `http://127.0.0.1:6543/${plugin}/v1`
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

function SectionCard({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-semibold text-[var(--color-text)]">{title}</h3>
        {action ? <div className="shrink-0">{action}</div> : null}
      </div>
      <div className="mt-4">{children}</div>
    </section>
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
  const canPushRemote = selectedPlugin === 'thesys'
  const serverListen = serverStatus?.listen || status?.server?.listen || status?.listen
  const baseUrl = pluginBaseUrl(selectedPlugin, serverListen)
  const availableCount = Number(selectedPluginStatus?.available_count || 0)
  const accountCount = Number(selectedPluginStatus?.account_count || 0)
  const pluginKeys = keys.filter((item) => String(item.plugin || '') === selectedPlugin)
  const accounts = selectedPluginStatus?.accounts || []
  const serverRunning = Boolean(serverStatus?.running)
  const toggleSettingFields = SETTING_FIELDS.filter((field) => field.type === 'toggle')
  const numberSettingFields = SETTING_FIELDS.filter((field) => field.type === 'number')

  const metricCards = useMemo(() => [
    { label: '可用账号', value: `${availableCount}/${accountCount}`, icon: Activity, tone: availableCount > 0 ? 'text-emerald-400' : 'text-red-400' },
    { label: 'API Key', value: pluginKeys.length, icon: KeyRound, tone: 'text-[var(--color-accent)]' },
    { label: '2API 服务', value: serverRunning ? '运行中' : '未启动', icon: Power, tone: serverRunning ? 'text-emerald-400' : 'text-red-400' },
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
    const allowedKeys = new Set(SETTING_FIELDS.map((field) => field.key))
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
    <div className="page-enter space-y-5">
      <section className="space-y-4">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="min-w-0 space-y-3">
            <h1 className="text-[1.65rem] font-semibold tracking-[-0.04em] text-[var(--color-text)]">2API 代理控制台</h1>
            <div className="toolbar-strip">
              <Badge variant="default">{currentLabel}</Badge>
              <Badge variant={availableCount > 0 ? 'success' : 'danger'}>可用 {availableCount}/{accountCount}</Badge>
              <Badge variant="secondary">Key {pluginKeys.length}</Badge>
              <Badge variant={serverRunning ? 'success' : 'danger'}>{serverRunning ? '服务运行中' : '服务未启动'}</Badge>
              {loading ? <Badge variant="warning">刷新中</Badge> : null}
            </div>
          </div>

          <div className="w-full rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4 xl:max-w-[520px]">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="break-all font-mono text-xs leading-5 text-[var(--color-text-secondary)]">{baseUrl}</div>
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
        {error ? <div className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div> : null}
      </section>

      <section className="grid gap-3 md:grid-cols-3">
        {metricCards.map(({ label, value, icon: Icon, tone }) => (
          <div key={label} className="workspace-metric-panel">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="workspace-kicker">{label}</div>
                <div className="workspace-metric-value tabular-nums">{value}</div>
              </div>
              <div className="workspace-metric-icon"><Icon className={`h-5 w-5 ${tone}`} /></div>
            </div>
          </div>
        ))}
      </section>

      <div className="flex items-center gap-1 border-b border-[var(--color-border)]">
        {TAB_ITEMS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            type="button"
            onClick={() => setActiveTab(key)}
            className={`flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm transition-colors ${activeTab === key ? 'border-[var(--color-accent)] text-[var(--color-text)]' : 'border-transparent text-[var(--color-text-secondary)] hover:text-[var(--color-text)]'}`}
          >
            <Icon className="h-4 w-4" />{label}
            {key === 'accounts' ? <span className="text-xs tabular-nums text-[var(--color-text-muted)]">{accountCount}</span> : null}
            {key === 'keys' ? <span className="text-xs tabular-nums text-[var(--color-text-muted)]">{pluginKeys.length}</span> : null}
            {key === 'logs' ? <span className="text-xs tabular-nums text-[var(--color-text-muted)]">{logs.length}</span> : null}
          </button>
        ))}
        <div className="ml-auto">
          <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
            <RefreshCw className={`mr-1.5 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />刷新
          </Button>
        </div>
      </div>

      {activeTab === 'accounts' && (
        <div className="space-y-5">
          <div className="overflow-hidden rounded-xl border border-[var(--color-border)]">
            <div className="flex items-center justify-between border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-2.5">
              <span className="text-xs font-medium text-[var(--color-text-secondary)]">账号池</span>
              <Button variant="ghost" size="sm" onClick={refreshCredits}>
                <RefreshCw className="mr-1.5 h-3.5 w-3.5" />刷新余额
              </Button>
            </div>
            <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm">
              <thead className="bg-[var(--color-surface)]">
                <tr className="border-b border-[var(--color-border)]">
                  <th className="px-4 py-3 text-left text-xs font-medium text-[var(--color-text-secondary)]">邮箱</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-[var(--color-text-secondary)]">余额</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-[var(--color-text-secondary)]">状态</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-[var(--color-text-secondary)]">Base URL</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account: any) => (
                  <tr key={`${account.email}-${account.base_url_preview}`} className="border-b border-[var(--color-border)]/40">
                    <td className="px-4 py-3 text-xs text-[var(--color-text)]">{account.email || '-'}</td>
                    <td className="px-4 py-3 tabular-nums text-[var(--color-text)]">{account.enabled ? (account.credit_amount ?? '-') : '-'}</td>
                    <td className="px-4 py-3"><Badge variant={account.enabled && account.credit_ok ? 'success' : 'danger'}>{formatStatusText(account)}</Badge></td>
                    <td className="px-4 py-3 font-mono text-xs text-[var(--color-text-secondary)]">{account.base_url_preview || '-'}</td>
                  </tr>
                ))}
                {accounts.length === 0 ? (
                  <tr><td colSpan={4} className="px-4 py-2"><EmptyPanel>未发现 {currentLabel} 代理账号</EmptyPanel></td></tr>
                ) : null}
              </tbody>
            </table>
            </div>
          </div>

          <SectionCard title="运行策略">
            <div className="space-y-3">
              {toggleSettingFields.map((field) => (
                <SettingToggle
                  key={field.key}
                  label={field.key === 'enabled' ? `启用 ${currentLabel} 2API` : field.label}
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
          </SectionCard>

          {canPushRemote ? (
            <SectionCard title="远端推送">
              <div className="space-y-3">
                <input value={remoteUrl} onChange={(event) => setRemoteUrl(event.target.value)} placeholder="http://1.2.3.4:8000 或 https://backend.example.com" className="control-surface" />
                <SettingToggle label="只推送最新账号" checked={pushLatestOnly} onChange={setPushLatestOnly} />
                <Button onClick={pushRemote} disabled={pushingRemote} className="w-full">
                  {pushingRemote ? '推送中...' : '推送到远端 Linux'}
                </Button>
                {remotePushResult ? <div className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">{remotePushResult}</div> : null}
              </div>
            </SectionCard>
          ) : null}
        </div>
      )}

      {activeTab === 'keys' && (
        <div className="space-y-4">
          <div className="flex flex-col gap-2 md:flex-row">
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="备注，例如 RikkaHub" className="control-surface" />
            <Button onClick={createKey}><Plus className="mr-1.5 h-4 w-4" />创建密钥</Button>
          </div>
          {pluginKeys.length === 0 ? <EmptyPanel>还没有 {currentLabel} 2API key</EmptyPanel> : null}
          <div className="space-y-2">
            {pluginKeys.map((item) => (
              <div key={item.id} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-mono text-xs text-[var(--color-text-secondary)]">{item.key_preview}</div>
                    <div className="mt-1 text-xs text-[var(--color-text-muted)]">{item.note || '无备注'} · {item.plugin}</div>
                  </div>
                  <div className="flex gap-1">
                    <Button variant="ghost" size="icon" onClick={() => copyText(item.key)} title="复制"><Copy className="h-4 w-4" /></Button>
                    <Button variant="ghost" size="icon" onClick={() => deleteKey(item.id)} title="删除"><Trash2 className="h-4 w-4" /></Button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {activeTab === 'logs' && (
        <div className="space-y-4">
          <pre className="max-h-[620px] overflow-auto rounded-xl border border-[var(--color-border)] bg-black/30 p-4 text-xs leading-5 text-[var(--color-text-secondary)]">
            {logs.length ? logs.join('\n') : '暂无日志'}
          </pre>
        </div>
      )}
    </div>
  )
}
