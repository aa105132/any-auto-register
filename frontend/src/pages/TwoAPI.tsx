import { useEffect, useMemo, useState } from 'react'
import { Activity, Copy, KeyRound, PlugZap, Plus, RefreshCw, Settings2, Trash2 } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { apiFetch } from '@/lib/utils'

function copyText(value: string) {
  if (!value) return
  navigator.clipboard?.writeText(value).catch(() => undefined)
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
  const [status, setStatus] = useState<any>(null)
  const [keys, setKeys] = useState<any[]>([])
  const [logs, setLogs] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [settings, setSettings] = useState<any>({ enabled: true, min_credit: 1, auto_wake: true, auto_refill: false, request_timeout: 90, wake_timeout: 60, max_retries: 2 })

  const load = async () => {
    const [statusData, keyData, logData, settingData] = await Promise.all([
      apiFetch('/2api/status'),
      apiFetch('/2api/keys'),
      apiFetch('/2api/logs?plugin=zo&limit=240'),
      apiFetch('/2api/settings'),
    ])
    setStatus(statusData)
    setKeys(keyData.items || [])
    setLogs(logData.items || [])
    setSettings(settingData || {})
  }

  useEffect(() => { load().catch(() => undefined) }, [])

  const zo = status?.plugins?.find((item: any) => item.name === 'zo') || null
  const baseUrl = status?.listen || 'http://127.0.0.1:6543/zo/v1'
  const availableCount = Number(zo?.available_count || 0)
  const accountCount = Number(zo?.account_count || 0)

  const metricCards = useMemo(() => [
    { label: '插件数', value: status?.plugins?.length || 0, note: '当前 2API 插件数量', icon: PlugZap, tone: 'text-[var(--color-accent)]' },
    { label: 'Zo 可用账号', value: `${availableCount}/${accountCount}`, note: '已跳过空额度或不可用账号', icon: Activity, tone: availableCount > 0 ? 'text-emerald-400' : 'text-red-400' },
    { label: 'API Key', value: keys.length, note: '外部客户端可用访问密钥', icon: KeyRound, tone: 'text-[var(--color-accent)]' },
    { label: '自动唤醒', value: settings.auto_wake ? '开启' : '关闭', note: 'Zo Space 睡眠时尝试 host restart', icon: RefreshCw, tone: settings.auto_wake ? 'text-emerald-400' : 'text-amber-400' },
  ], [accountCount, availableCount, keys.length, settings.auto_wake, status?.plugins?.length])

  const createKey = async () => {
    const row = await apiFetch('/2api/keys', { method: 'POST', body: JSON.stringify({ plugin: 'zo', note }) })
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

  return (
    <div className="page-enter space-y-5">
      <section className="rounded-xl border border-[var(--color-border)] bg-[linear-gradient(180deg,rgba(255,255,255,0.05),rgba(255,255,255,0.018))] p-5 shadow-[var(--shadow-sm)]">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(280px,0.65fr)]">
          <div className="space-y-3">
            <div className="workspace-kicker">系统 / 2API</div>
            <div>
              <h1 className="text-[1.7rem] font-semibold tracking-[-0.045em] text-[var(--color-text)]">2API 聚合代理</h1>
              <p className="mt-2 max-w-[70ch] text-sm leading-6 text-[var(--color-text-secondary)]">
                统一管理各平台的 OpenAI 兼容代理。当前先接入 Zo，多号轮询、自动唤醒、跳过空额度账号，后续插件复用同一框架。
              </p>
            </div>
            <div className="toolbar-strip">
              <Badge variant="default">Base URL {baseUrl}</Badge>
              <Badge variant={availableCount > 0 ? 'success' : 'danger'}>Zo 可用 {availableCount}</Badge>
              <Badge variant="secondary">Key {keys.length}</Badge>
            </div>
          </div>

          <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
            <div className="workspace-kicker">客户端填写</div>
            <div className="mt-2 break-all font-mono text-xs leading-5 text-[var(--color-text-secondary)]">{baseUrl}</div>
            <Button variant="outline" size="sm" onClick={() => copyText(baseUrl)} className="mt-4">
              <Copy className="mr-1.5 h-4 w-4" />复制 Base URL
            </Button>
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
              <div className="workspace-kicker">Zo 插件设置</div>
              <div className="mt-1 text-base font-semibold text-[var(--color-text)]">运行策略</div>
            </div>
            <Settings2 className="h-5 w-5 text-[var(--color-text-secondary)]" />
          </div>
          <div className="mt-4 space-y-3">
            <SettingToggle label="启用 Zo 2API" checked={Boolean(settings.enabled)} onChange={(v) => setSettings((s: any) => ({ ...s, enabled: v }))} />
            <SettingToggle label="自动唤醒" helper="Zo Free 计划睡眠时，先尝试 host restart，再轮询 models。" checked={Boolean(settings.auto_wake)} onChange={(v) => setSettings((s: any) => ({ ...s, auto_wake: v }))} />
            <SettingToggle label="自动补号" helper="账号池为空或全部不可用时，后台触发现有 Zo 注册脚本。" checked={Boolean(settings.auto_refill)} onChange={(v) => setSettings((s: any) => ({ ...s, auto_refill: v }))} />
            <label className="block space-y-2">
              <span className="workspace-kicker">最低余额阈值</span>
              <input type="number" value={settings.min_credit ?? 1} onChange={(e) => setSettings((s: any) => ({ ...s, min_credit: Number(e.target.value || 0) }))} className="control-surface" />
            </label>
            <label className="block space-y-2">
              <span className="workspace-kicker">请求超时秒数</span>
              <input type="number" value={settings.request_timeout ?? 90} onChange={(e) => setSettings((s: any) => ({ ...s, request_timeout: Number(e.target.value || 90) }))} className="control-surface" />
            </label>
            <Button onClick={saveSettings} disabled={saving} className="w-full">
              {saving ? '保存中...' : '保存设置'}
            </Button>
          </div>
        </Card>

        <Card className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="workspace-kicker">API Key 管理</div>
              <div className="mt-1 text-base font-semibold text-[var(--color-text)]">外部访问密钥</div>
            </div>
            <Button variant="outline" size="sm" onClick={load}><RefreshCw className="mr-1.5 h-4 w-4" />刷新</Button>
          </div>
          <div className="mt-4 flex gap-2">
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="备注，例如 RikkaHub" className="control-surface" />
            <Button onClick={createKey}><Plus className="mr-1.5 h-4 w-4" />创建</Button>
          </div>
          <div className="mt-4 space-y-2">
            {keys.length === 0 ? <div className="empty-state-panel">还没有 2API key，创建一个后给 RikkaHub / 酒馆使用。</div> : null}
            {keys.map((item) => (
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
            <div className="workspace-kicker">Zo 账号池</div>
            <div className="mt-1 text-base font-semibold text-[var(--color-text)]">轮询候选账号</div>
          </div>
          <div className="glass-table-wrap workspace-table-scroll">
            <table className="workspace-table min-w-[760px] w-full text-sm">
              <thead><tr><th className="px-4 py-3 text-left">邮箱</th><th className="px-4 py-3 text-left">余额</th><th className="px-4 py-3 text-left">状态</th><th className="px-4 py-3 text-left">Base URL</th></tr></thead>
              <tbody>
                {(zo?.accounts || []).map((account: any) => (
                  <tr key={`${account.email}-${account.base_url_preview}`} className="border-b border-[var(--color-border)]/40">
                    <td className="px-4 py-3 text-xs">{account.email}</td>
                    <td className="px-4 py-3 tabular-nums">{account.credit_amount}</td>
                    <td className="px-4 py-3"><Badge variant={account.credit_ok ? 'success' : 'danger'}>{account.last_status || (account.credit_ok ? '可用' : '空额度')}</Badge></td>
                    <td className="px-4 py-3 font-mono text-xs text-[var(--color-text-secondary)]">{account.base_url_preview}</td>
                  </tr>
                ))}
                {(!zo?.accounts || zo.accounts.length === 0) ? <tr><td colSpan={4} className="px-4 py-8"><div className="empty-state-panel">未发现 Zo 代理账号。注册 Zo 后会自动写入 output/zo_proxy_urls.txt。</div></td></tr> : null}
              </tbody>
            </table>
          </div>
        </Card>

        <Card className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="workspace-kicker">运行日志</div>
              <div className="mt-1 text-base font-semibold text-[var(--color-text)]">Zo 插件日志</div>
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
