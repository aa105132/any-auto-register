import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Activity, Copy, KeyRound, PlugZap, Plus, Power, RefreshCw, Settings2, Trash2 } from 'lucide-react'

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

function SettingToggle({ label, checked, onChange, helper }: { label: string; checked: boolean; onChange: (v: boolean) => void; helper?: string }) {

  return (
    <label className="flex items-start justify-between gap-3 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2">
      <span>
        <span className="block text-sm font-medium text-[var(--color-text)]">{label}</span>
        {helper ? <span className="mt-1 block text-xs leading-5 text-[var(--color-text-secondary)]">{helper}</span> : null}
      </span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} className="mt-1 h-4 w-4" />
    </label>
  )
}

export default function TwoAPI() {
  const params = useParams()
  const selectedPlugin = String(params.plugin || 'zo').trim() || 'zo'
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
  const [settings, setSettings] = useState<any>({ enabled: true, min_credit: 1, auto_wake: true, auto_refill: false, request_timeout: 90, wake_timeout: 60, max_retries: 2, keepalive_space_fallback: false, minimize_ask_context: true })

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const [statusData, keyData, logData, settingData] = await Promise.all([
        apiFetch('/2api/status'),
        apiFetch('/2api/keys'),
        apiFetch(`/2api/logs?plugin=${encodeURIComponent(selectedPlugin)}&limit=240`),
        apiFetch('/2api/settings'),
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
  const serverListen = serverStatus?.listen || status?.server?.listen || status?.listen
  const baseUrl = pluginBaseUrl(selectedPlugin, serverListen)
  const availableCount = Number(selectedPluginStatus?.available_count || 0)
  const accountCount = Number(selectedPluginStatus?.account_count || 0)
  const pluginKeys = keys.filter((item) => String(item.plugin || '') === selectedPlugin)
  const accounts = selectedPluginStatus?.accounts || []
  const serverRunning = Boolean(serverStatus?.running)

  const metricCards = useMemo(() => [
    { label: '当前插件', value: currentLabel, note: '侧边栏选择后进入对应插件详情', icon: PlugZap, tone: 'text-[var(--color-accent)]' },
    { label: '可用账号', value: `${availableCount}/${accountCount}`, note: '已跳过空额度或不可用账号', icon: Activity, tone: availableCount > 0 ? 'text-emerald-400' : 'text-red-400' },
    { label: 'API Key', value: pluginKeys.length, note: `${currentLabel} 外部访问密钥数量`, icon: KeyRound, tone: 'text-[var(--color-accent)]' },
    { label: '2API 服务', value: serverRunning ? '运行中' : '未启动', note: '本地监听 127.0.0.1:6543', icon: Power, tone: serverRunning ? 'text-emerald-400' : 'text-red-400' },
  ], [accountCount, availableCount, currentLabel, pluginKeys.length, serverRunning])

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

  const saveSettings = async () => {
    setSaving(true)
    try {
      await apiFetch('/2api/settings', { method: 'POST', body: JSON.stringify(settings) })
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
    <div className="page-enter space-y-5">
      <section className="rounded-xl border border-[var(--color-border)] bg-[linear-gradient(180deg,rgba(255,255,255,0.05),rgba(255,255,255,0.018))] p-5 shadow-[var(--shadow-sm)]">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(280px,0.65fr)]">
          <div className="space-y-3">
            <div className="workspace-kicker">系统 / 2API / {currentLabel}</div>
            <div>
              <h1 className="text-[1.7rem] font-semibold tracking-[-0.045em] text-[var(--color-text)]">{currentLabel} 2API 插件</h1>
              <p className="mt-2 max-w-[70ch] text-sm leading-6 text-[var(--color-text-secondary)]">
                每个平台插件独立展示代理设置、访问密钥、账号池和运行日志。左侧 2API 菜单可像账号资产一样展开并切换不同插件。
              </p>
            </div>
            <div className="toolbar-strip">
              <Badge variant="default">Base URL {baseUrl}</Badge>
              <Badge variant={availableCount > 0 ? 'success' : 'danger'}>{currentLabel} 可用 {availableCount}</Badge>
              <Badge variant="secondary">Key {pluginKeys.length}</Badge>
              <Badge variant={serverRunning ? 'success' : 'danger'}>127.0.0.1:6543 {serverRunning ? '运行中' : '未启动'}</Badge>
              {loading ? <Badge variant="warning">刷新中</Badge> : null}
            </div>
            {error ? <div className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div> : null}
          </div>

          <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
            <div className="workspace-kicker">客户端填写</div>
            <div className="mt-2 break-all font-mono text-xs leading-5 text-[var(--color-text-secondary)]">{baseUrl}</div>
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

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
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
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        <Card className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="workspace-kicker">{currentLabel} 插件设置</div>
              <div className="mt-1 text-base font-semibold text-[var(--color-text)]">运行策略</div>
            </div>
            <Settings2 className="h-5 w-5 text-[var(--color-text-secondary)]" />
          </div>
          <div className="mt-4 space-y-3">
            <SettingToggle label={`启用 ${currentLabel} 2API`} checked={Boolean(settings.enabled)} onChange={(v) => setSettings((s: any) => ({ ...s, enabled: v }))} />
            <SettingToggle label="自动唤醒" helper="空间睡眠时，先尝试平台恢复接口，再轮询 models。" checked={Boolean(settings.auto_wake)} onChange={(v) => setSettings((s: any) => ({ ...s, auto_wake: v }))} />
            <SettingToggle label="保活 Space 兼容代理" helper="默认关闭；仅在需要旧 *.zo.space OpenAI 代理 fallback 时启用。" checked={Boolean(settings.keepalive_space_fallback)} onChange={(v) => setSettings((s: any) => ({ ...s, keepalive_space_fallback: v }))} />
            <SettingToggle label="降低 /ask 上下文" helper="自动启用空 scopes 的极简 persona，可减少 Zo 固定工具上下文 token；失败时自动回退。" checked={Boolean(settings.minimize_ask_context)} onChange={(v) => setSettings((s: any) => ({ ...s, minimize_ask_context: v }))} />
            <SettingToggle label="自动补号" helper="账号池为空或全部不可用时，后台触发现有注册脚本。" checked={Boolean(settings.auto_refill)} onChange={(v) => setSettings((s: any) => ({ ...s, auto_refill: v }))} />
            <label className="block space-y-2">
              <span className="workspace-kicker">最低余额阈值</span>
              <input type="number" value={settings.min_credit ?? 1} onChange={(e) => setSettings((s: any) => ({ ...s, min_credit: Number(e.target.value || 0) }))} className="control-surface" />
            </label>
            <label className="block space-y-2">
              <span className="workspace-kicker">请求超时秒数</span>
              <input type="number" value={settings.request_timeout ?? 90} onChange={(e) => setSettings((s: any) => ({ ...s, request_timeout: Number(e.target.value || 90) }))} className="control-surface" />
            </label>
            <div className="grid grid-cols-2 gap-2">
              <Button variant="outline" onClick={refreshCredits}>刷新余额</Button>
              <Button variant="outline" onClick={recoverPlugin}>唤醒/恢复</Button>
            </div>
            <Button onClick={saveSettings} disabled={saving} className="w-full">
              {saving ? '保存中...' : '保存设置'}
            </Button>
          </div>
        </Card>

        {canPushRemote ? (
          <Card className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
            <div className="workspace-kicker">远端后端地址</div>
            <div className="mt-1 text-base font-semibold text-[var(--color-text)]">推送到远端 Linux</div>
            <p className="mt-2 text-xs leading-5 text-[var(--color-text-secondary)]">
              将本机 {currentLabel} 注册结果推送到另一台 Linux 后端的 2API 导入接口，适合远端部署代理池。
            </p>
            <div className="mt-4 space-y-3">
              <input value={remoteUrl} onChange={(event) => setRemoteUrl(event.target.value)} placeholder="http://1.2.3.4:8000 或 https://backend.example.com" className="control-surface" />
              <SettingToggle label="只推送最新账号" checked={pushLatestOnly} onChange={setPushLatestOnly} />
              <Button onClick={pushRemote} disabled={pushingRemote} className="w-full">
                {pushingRemote ? '推送中...' : '推送到远端 Linux'}
              </Button>
              {remotePushResult ? <div className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">{remotePushResult}</div> : null}
            </div>
          </Card>
        ) : null}

        <Card className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="workspace-kicker">{currentLabel} API Key</div>
              <div className="mt-1 text-base font-semibold text-[var(--color-text)]">外部访问密钥</div>
            </div>
            <Button variant="outline" size="sm" onClick={load}><RefreshCw className="mr-1.5 h-4 w-4" />刷新</Button>
          </div>
          <div className="mt-4 flex gap-2">
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="备注，例如 RikkaHub" className="control-surface" />
            <Button onClick={createKey}><Plus className="mr-1.5 h-4 w-4" />创建</Button>
          </div>
          <div className="mt-4 space-y-2">
            {pluginKeys.length === 0 ? <div className="empty-state-panel">还没有 {currentLabel} 2API key，创建后给 RikkaHub / 酒馆使用。</div> : null}
            {pluginKeys.map((item) => (
              <div key={item.id} className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
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
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card className="overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-0">
          <div className="border-b border-[var(--color-border)] px-5 py-4">
            <div className="workspace-kicker">{currentLabel} 账号池</div>
            <div className="mt-1 text-base font-semibold text-[var(--color-text)]">轮询候选账号</div>
          </div>
          <div className="glass-table-wrap workspace-table-scroll">
            <table className="workspace-table min-w-[760px] w-full text-sm">
              <thead><tr><th className="px-4 py-3 text-left">邮箱</th><th className="px-4 py-3 text-left">余额</th><th className="px-4 py-3 text-left">状态</th><th className="px-4 py-3 text-left">Base URL</th></tr></thead>
              <tbody>
                {accounts.map((account: any) => (
                  <tr key={`${account.email}-${account.base_url_preview}`} className="border-b border-[var(--color-border)]/40">
                    <td className="px-4 py-3 text-xs">{account.email || '-'}</td>
                    <td className="px-4 py-3 tabular-nums">{account.enabled ? (account.credit_amount ?? '-') : '-'}</td>
                    <td className="px-4 py-3"><Badge variant={account.enabled && account.credit_ok ? 'success' : 'danger'}>{account.last_status === 'proxy_missing' ? '未部署代理' : account.last_status || (account.enabled && account.credit_ok ? '可用' : '不可用')}</Badge></td>
                    <td className="px-4 py-3 font-mono text-xs text-[var(--color-text-secondary)]">{account.base_url_preview || '-'}</td>
                  </tr>
                ))}
                {accounts.length === 0 ? <tr><td colSpan={4} className="px-4 py-8"><div className="empty-state-panel">未发现 {currentLabel} 代理账号。注册完成后会自动写入对应插件账号池。</div></td></tr> : null}
              </tbody>
            </table>
          </div>
        </Card>

        <Card className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="workspace-kicker">运行日志</div>
              <div className="mt-1 text-base font-semibold text-[var(--color-text)]">{currentLabel} 插件日志</div>
            </div>
            <Button variant="outline" size="sm" onClick={load}><RefreshCw className="mr-1.5 h-4 w-4" />刷新</Button>
          </div>
          <pre className="mt-4 max-h-[520px] overflow-auto rounded-md border border-[var(--color-border)] bg-black/30 p-3 text-xs leading-5 text-[var(--color-text-secondary)]">
            {logs.length ? logs.join('\n') : '暂无日志'}
          </pre>
        </Card>
      </div>
    </div>
  )
}
