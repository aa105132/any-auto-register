import { useEffect, useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { getPlatforms } from '@/lib/app-data'
import { apiFetch } from '@/lib/utils'
import { useActiveTask } from '@/context/ActiveTaskContext'
import { Ban, Clock3, Database, Plus, RefreshCw, RotateCcw, ShieldCheck, Trash2, Upload, Users, WalletCards } from 'lucide-react'

type GooglePoolAccount = {
  email: string
  added_at?: string
  expires_at?: string
  source?: string
  source_order_id?: string
  registered_platforms?: string[]
  registered_count?: number
  notes?: string
  password?: string
  status?: string
}

type PlatformMeta = {
  name?: string
  display_name?: string
  supported_identity_modes?: string[]
  supported_oauth_providers?: string[]
}

type PlatformOption = {
  value: string
  label: string
  source: 'oauth' | 'history'
}

type PoolResponse = {
  stats?: {
    total?: number
    unused?: number
    by_platform?: Record<string, number>
  }
  items?: GooglePoolAccount[]
}

function formatTime(value?: string) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function Metric({ label, value, hint, icon: Icon }: { label: string; value: string | number; hint: string; icon: any }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-[var(--color-text-muted)]">{label}</div>
          <div className="mt-1 text-2xl font-semibold tabular-nums text-[var(--color-text)]">{value}</div>
        </div>
        <div className="flex h-9 w-9 items-center justify-center rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text)]">
          <Icon className="h-4 w-4" />
        </div>
      </div>
      <div className="mt-2 text-xs text-[var(--color-text-muted)]">{hint}</div>
    </div>
  )
}

export default function GoogleAccountPool() {
  const [data, setData] = useState<PoolResponse>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [importText, setImportText] = useState('')
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState<any>(null)
  const [platformFilter, setPlatformFilter] = useState('')
  const [platformFilterMode, setPlatformFilterMode] = useState<'registered' | 'missing'>('missing')
  const [statusUpdatingEmail, setStatusUpdatingEmail] = useState('')
  const [deletingInvalid, setDeletingInvalid] = useState(false)
  const [notice, setNotice] = useState('')
  const [platforms, setPlatforms] = useState<PlatformMeta[]>([])

  // ─── Workspace 批量建号 ───
  const { setActiveTask } = useActiveTask()
  const [wsCount, setWsCount] = useState(50)
  const [wsRecoveryDomain, setWsRecoveryDomain] = useState('bufan.de5.net')
  const [wsPassword, setWsPassword] = useState('Bufan123456')
  const [wsBatchSize, setWsBatchSize] = useState(10)
  const [wsGenLoading, setWsGenLoading] = useState(false)
  const [wsCreateLoading, setWsCreateLoading] = useState(false)
  const [wsUsers, setWsUsers] = useState<any[]>([])
  const [wsNotice, setWsNotice] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      setData(await apiFetch('/google-account-pool'))
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载 Google 账号池失败')
    } finally {
      setLoading(false)
    }
  }

  const importAccounts = async () => {
    const lines = importText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
    if (lines.length === 0) {
      setError('请先粘贴要导入的账号，每行一个')
      return
    }
    setImporting(true)
    setError('')
    setNotice('')
    setImportResult(null)
    try {
      const result = await apiFetch('/google-account-pool/import', {
        method: 'POST',
        body: JSON.stringify({ lines, source: 'manual' }),
      })
      setImportResult(result)
      setImportText('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入 Google 账号失败')
    } finally {
      setImporting(false)
    }
  }

  const fillExample = () => setImportText('demo1@gmail.com|password123\ndemo2@gmail.com----password456')

  // ─── Workspace 批量操作 ───
  const loadWsUsers = async () => {
    try {
      const d = await apiFetch('/google-workspace/users-json')
      if (d.ok) setWsUsers(d.users || [])
    } catch { /* ignore */ }
  }

  const genWsUsers = async () => {
    setWsGenLoading(true)
    setWsNotice('')
    setError('')
    try {
      const d = await apiFetch('/google-workspace/gen-users', {
        method: 'POST',
        body: JSON.stringify({
          count: wsCount,
          recovery_domain: wsRecoveryDomain,
          password: wsPassword,
          one_per_user: true,
        }),
      })
      if (d.ok) {
        setWsNotice(`已生成 ${d.total} 个用户，其中 ${d.has_recovery} 个有辅助邮箱`)
        await loadWsUsers()
      } else {
        setWsNotice(`生成失败: ${d.stderr || d.error || '未知错误'}`)
      }
    } catch (e) {
      setWsNotice(e instanceof Error ? e.message : '生成用户失败')
    } finally {
      setWsGenLoading(false)
    }
  }

  const startBulkCreate = async () => {
    setWsCreateLoading(true)
    setWsNotice('')
    setError('')
    try {
      const res = await apiFetch('/google-workspace/bulk-create', {
        method: 'POST',
        body: JSON.stringify({ limit: 0, offset: 0 }),
      })
      setActiveTask({
        id: res.task_id,
        platform: 'google_workspace',
        status: res.status || 'pending',
        count: res.progress_detail?.total ?? wsCount,
        succeeded: res.success,
        failed: res.error_count,
      })
      setWsNotice(`已启动批量创建任务 ${res.task_id}，进度请在右下角任务条查看`)
    } catch (e) {
      setWsNotice(e instanceof Error ? e.message : '启动批量创建失败')
    } finally {
      setWsCreateLoading(false)
    }
  }

  useEffect(() => { loadWsUsers() }, [])

  const markAccountStatus = async (item: GooglePoolAccount, status: 'valid' | 'invalid') => {
    if (!item.email) return
    setStatusUpdatingEmail(item.email)
    setError('')
    setNotice('')
    try {
      await apiFetch(`/google-account-pool/${encodeURIComponent(item.email)}/${status}`, {
        method: 'POST',
        body: status === 'invalid' ? JSON.stringify({ reason: '前端手动标注失效' }) : undefined,
      })
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新 Google 账号状态失败')
    } finally {
      setStatusUpdatingEmail('')
    }
  }


  const deleteInvalidAccounts = async () => {
    const count = items.filter((item) => (item.status || 'valid').toLowerCase() === 'invalid').length
    if (count <= 0) {
      setNotice('当前没有已失效账号可删除')
      return
    }
    if (!window.confirm(`确认删除 ${count} 个已失效 Google 账号？此操作不可撤销。`)) return
    setDeletingInvalid(true)
    setError('')
    setNotice('')
    try {
      const result = await apiFetch('/google-account-pool/invalid', { method: 'DELETE' })
      setNotice(`已删除 ${result?.deleted || 0} 个失效账号`)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失效 Google 账号失败')
    } finally {
      setDeletingInvalid(false)
    }
  }

  useEffect(() => { load() }, [])

  useEffect(() => {
    getPlatforms()
      .then((items) => setPlatforms(Array.isArray(items) ? items : []))
      .catch(() => setPlatforms([]))
  }, [])

  const items = data.items || []
  const platformEntries = Object.entries(data.stats?.by_platform || {}).sort((a, b) => b[1] - a[1])
  const platformOptions = useMemo<PlatformOption[]>(() => {
    const options = new Map<string, PlatformOption>()

    platforms
      .filter((platform) => (platform.supported_identity_modes || []).includes('oauth_browser'))
      .filter((platform) => (platform.supported_oauth_providers || []).includes('google'))
      .forEach((platform) => {
        const value = String(platform.name || '').trim()
        if (!value) return
        options.set(value.toLowerCase(), {
          value,
          label: platform.display_name ? `${platform.display_name} (${value})` : value,
          source: 'oauth',
        })
      })

    const addHistoryPlatform = (platform: string) => {
      const value = String(platform || '').trim()
      if (!value) return
      const key = value.toLowerCase()
      if (!options.has(key)) options.set(key, { value, label: value, source: 'history' })
    }

    platformEntries.forEach(([platform]) => addHistoryPlatform(platform))
    items.forEach((item) => (item.registered_platforms || []).forEach(addHistoryPlatform))

    return Array.from(options.values()).sort((a, b) => a.value.localeCompare(b.value))
  }, [items, platformEntries, platforms])
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const targetPlatform = platformFilter.trim().toLowerCase()
    return items.filter((item) => {
      const platforms = (item.registered_platforms || []).map((platform) => String(platform).trim().toLowerCase()).filter(Boolean)
      if (targetPlatform) {
        const hasPlatform = platforms.includes(targetPlatform)
        if (platformFilterMode === 'registered' && !hasPlatform) return false
        if (platformFilterMode === 'missing' && hasPlatform) return false
      }
      if (!q) return true
      const platformText = platforms.join(' ')
      return `${item.email} ${item.password || ''} ${item.source || ''} ${item.source_order_id || ''} ${item.status || ''} ${item.notes || ''} ${platformText}`.toLowerCase().includes(q)
    })
  }, [items, query, platformFilter, platformFilterMode])

  const invalidCount = items.filter((item) => (item.status || 'valid').toLowerCase() === 'invalid').length
  const reusable = items.filter((item) => (item.status || 'valid').toLowerCase() !== 'invalid' && (item.registered_platforms || []).length === 0).length

  return (
    <div className="page-enter space-y-4">
      <section className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.65fr)]">
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-5 py-5">
          <div className="workspace-kicker">账号资产 / Google 账号池</div>
          <h1 className="mt-2 text-[var(--text-2xl)] font-semibold tracking-tight text-[var(--color-text)]">Google 账号池</h1>
          <p className="mt-2 max-w-[72ch] text-sm leading-6 text-[var(--color-text-secondary)]">
            这里展示由 HStockPlus 购买并保存的 Google/Gmail 账号，以及每个账号已经注册过的平台。复用注册会跳过已在目标平台注册过的账号。
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={load} disabled={loading}>
              <RefreshCw className={`mr-1 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
              {loading ? '刷新中' : '刷新'}
            </Button>
            <a href="#google-pool-import" className="inline-flex h-8 items-center justify-center rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 text-xs font-medium text-[var(--color-text-secondary)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-text)]">
              <Plus className="mr-1 h-3.5 w-3.5" />手动导入
            </a>
            <Button size="sm" variant="destructive" onClick={deleteInvalidAccounts} disabled={deletingInvalid || invalidCount === 0}>
              <Trash2 className="mr-1 h-3.5 w-3.5" />{deletingInvalid ? '删除中...' : `删除失效账号 (${invalidCount})`}
            </Button>
            <Badge variant="secondary">来源 output/google_accounts_pool.json</Badge>
          </div>
        </div>
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-4">
          <div className="workspace-kicker">平台占用</div>
          <div className="mt-3 flex flex-wrap gap-2">
            {platformEntries.length > 0 ? platformEntries.map(([platform, count]) => (
              <span key={platform} className="inline-flex items-center gap-1 rounded-full border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)]">
                <span className="h-1.5 w-1.5 rounded-full bg-[var(--color-accent)]" />
                {platform} · {count}
              </span>
            )) : <span className="text-sm text-[var(--color-text-muted)]">暂无平台注册记录</span>}
          </div>
        </div>
      </section>

      <Card id="google-pool-import" className="p-0 overflow-hidden">
        <div className="grid gap-4 p-5 xl:grid-cols-[minmax(0,1fr)_320px]">
          <div>
            <div className="workspace-kicker">手动 / 批量导入</div>
            <h2 className="mt-1 text-base font-semibold text-[var(--color-text)]">导入 Google 账号到池</h2>
            <p className="mt-1 text-sm text-[var(--color-text-secondary)]">一行一个账号，支持 <span className="font-mono">邮箱|密码</span>、<span className="font-mono">邮箱----密码</span>、逗号、Tab 或空格分隔。重复邮箱会跳过，不覆盖原密码。</p>
            <textarea
              value={importText}
              onChange={(event) => setImportText(event.target.value)}
              rows={6}
              placeholder="demo@gmail.com|password123
demo2@gmail.com----password456"
              className="control-surface control-surface-mono mt-3 resize-y"
            />
            <div className="mt-3 flex flex-wrap gap-2">
              <Button size="sm" onClick={importAccounts} disabled={importing}>
                <Upload className="mr-1 h-3.5 w-3.5" />{importing ? '导入中...' : '批量导入'}
              </Button>
              <Button size="sm" variant="outline" onClick={fillExample}>填入示例</Button>
            </div>
          </div>
          <aside className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
            <div className="workspace-kicker">解析规则</div>
            <div className="mt-3 space-y-2 text-xs text-[var(--color-text-secondary)]">
              <div><span className="font-mono text-[var(--color-text)]">email|password</span></div>
              <div><span className="font-mono text-[var(--color-text)]">email----password</span></div>
              <div><span className="font-mono text-[var(--color-text)]">email,password</span></div>
              <div><span className="font-mono text-[var(--color-text)]">email password</span></div>
              <div className="pt-2 text-[var(--color-text-muted)]">只会保存邮箱和密码；平台注册记录会在复用成功后自动追加。</div>
            </div>
            {importResult ? (
              <div className="mt-4 rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">
                新增 {importResult.created || 0}，重复 {importResult.duplicates || 0}，无效 {importResult.invalid || 0}
              </div>
            ) : null}
          </aside>
        </div>
      </Card>

      {/* ─── Workspace 批量建号 ─── */}
      <Card className="p-0 overflow-hidden">
        <div className="border-b border-[var(--color-border)] px-5 py-4">
          <div className="flex items-center gap-2">
            <Users className="h-4 w-4 text-[var(--color-accent)]" />
            <div className="workspace-kicker">Google Workspace 批量建号</div>
          </div>
          <h2 className="mt-1 text-base font-semibold text-[var(--color-text)]">Workspace 子账号批量创建</h2>
          <p className="mt-1 text-sm text-[var(--color-text-secondary)]">生成用户清单 + pangxie/bufan 辅助邮箱 → 浏览器自动填表批量创建。创建时 Google 会自动发登录说明到辅助邮箱。</p>
        </div>
        <div className="grid gap-4 p-5 xl:grid-cols-[minmax(0,1fr)_minmax(300px,0.5fr)]">
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block space-y-1.5">
                <span className="text-xs font-medium text-[var(--color-text-secondary)]">用户数量</span>
                <input type="number" value={wsCount} min={1} max={100} onChange={(e) => setWsCount(Number(e.target.value))}
                  className="control-surface control-surface-compact" />
              </label>
              <label className="block space-y-1.5">
                <span className="text-xs font-medium text-[var(--color-text-secondary)]">辅助邮箱域</span>
                <select value={wsRecoveryDomain} onChange={(e) => setWsRecoveryDomain(e.target.value)}
                  className="control-surface control-surface-compact appearance-none">
                  <option value="bufan.de5.net">bufan.de5.net</option>
                  <option value="pangxie888.com">pangxie888.com</option>
                  <option value="chenbufan.cloud">chenbufan.cloud</option>
                </select>
              </label>
              <label className="block space-y-1.5">
                <span className="text-xs font-medium text-[var(--color-text-secondary)]">统一密码</span>
                <input type="text" value={wsPassword} onChange={(e) => setWsPassword(e.target.value)}
                  className="control-surface control-surface-compact" />
              </label>
              <label className="block space-y-1.5">
                <span className="text-xs font-medium text-[var(--color-text-secondary)]">每批数量</span>
                <input type="number" value={wsBatchSize} min={1} max={10} onChange={(e) => setWsBatchSize(Number(e.target.value))}
                  className="control-surface control-surface-compact" />
              </label>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={genWsUsers} disabled={wsGenLoading}>
                <Plus className="mr-1 h-3.5 w-3.5" />{wsGenLoading ? '生成中...' : '生成用户清单'}
              </Button>
              <Button size="sm" variant="default" onClick={startBulkCreate} disabled={wsCreateLoading || wsUsers.length === 0}>
                <Users className="mr-1 h-3.5 w-3.5" />{wsCreateLoading ? '启动中...' : '开始批量创建'}
              </Button>
              <Button size="sm" variant="outline" onClick={loadWsUsers}>
                <RefreshCw className="mr-1 h-3.5 w-3.5" />刷新清单
              </Button>
            </div>
            {wsNotice ? (
              <div className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">{wsNotice}</div>
            ) : null}
          </div>
          <aside className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
            <div className="workspace-kicker">用户清单</div>
            <div className="mt-2 text-sm text-[var(--color-text)]">
              {wsUsers.length > 0 ? `${wsUsers.length} 个用户` : '尚未生成'}
            </div>
            {wsUsers.length > 0 ? (
              <div className="mt-3 max-h-48 space-y-1 overflow-y-auto text-xs text-[var(--color-text-secondary)]">
                {wsUsers.slice(0, 20).map((u, i) => (
                  <div key={i} className="flex items-center justify-between gap-2 font-mono">
                    <span className="truncate">{u.email}</span>
                    <span className="text-[var(--color-text-muted)]">{u.recovery_email ? '✓' : '✗'}</span>
                  </div>
                ))}
                {wsUsers.length > 20 ? <div className="pt-1 text-[var(--color-text-muted)]">...还有 {wsUsers.length - 20} 个</div> : null}
              </div>
            ) : (
              <p className="mt-2 text-xs text-[var(--color-text-muted)]">先点「生成用户清单」创建辅助邮箱，再点「开始批量创建」。</p>
            )}
          </aside>
        </div>
      </Card>

      <section className="grid gap-3 md:grid-cols-4">
        <Metric label="总账号" value={data.stats?.total ?? items.length} hint="池内保存的 Google 账号数" icon={Database} />
        <Metric label="完全未用" value={reusable} hint="未失效且未绑定任何平台，适合优先复用" icon={WalletCards} />
        <Metric label="已使用" value={items.filter((item) => (item.registered_platforms || []).length > 0).length} hint="至少注册过一个平台" icon={ShieldCheck} />
        <Metric label="已失效" value={invalidCount} hint="已标注失效的账号不会被自动复用" icon={Ban} />
      </section>

      <Card className="p-0 overflow-hidden">
        <div className="border-b border-[var(--color-border)] px-5 py-4">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
            <div>
              <div className="workspace-kicker">池内账号</div>
              <div className="mt-1 text-sm text-[var(--color-text-secondary)]">按加入时间读取，测试账号密码直接明文展示，失效账号不会被自动复用。</div>
            </div>
            <div className="grid gap-2 md:grid-cols-[220px_180px_180px_auto]">
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索邮箱 / 密码 / 平台 / 订单号 / 状态"
                className="control-surface control-surface-compact"
              />
              <select value={platformFilter} onChange={(event) => setPlatformFilter(event.target.value)} className="control-surface control-surface-compact appearance-none">
                <option value="">选择平台筛选</option>
                {platformOptions.map((platform) => <option key={platform.value} value={platform.value}>{platform.label}</option>)}
              </select>
              <select value={platformFilterMode} onChange={(event) => setPlatformFilterMode(event.target.value as 'registered' | 'missing')} className="control-surface control-surface-compact appearance-none" disabled={!platformFilter}>
                <option value="missing">未注册该平台</option>
                <option value="registered">已注册该平台</option>
              </select>
              <Button size="sm" variant="outline" onClick={() => { setQuery(''); setPlatformFilter(''); setPlatformFilterMode('missing') }}>重置筛选</Button>
            </div>
          </div>
          {platformFilter ? (
            <div className="mt-3 flex flex-wrap gap-2 text-xs text-[var(--color-text-muted)]">
              <Badge variant={platformFilterMode === 'missing' ? 'warning' : 'default'}>{platformFilterMode === 'missing' ? `未注册 ${platformFilter}` : `已注册 ${platformFilter}`}</Badge>
              <Badge variant="secondary">匹配 {filtered.length} / {items.length}</Badge>
            </div>
          ) : null}
        </div>

        {notice ? (
          <div className="mx-5 mt-5 rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">{notice}</div>
        ) : null}
        {error ? (
          <div className="m-5 rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div>
        ) : null}

        {filtered.length === 0 ? (
          <div className="px-5 py-12 text-center">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text-muted)]">
              <Clock3 className="h-5 w-5" />
            </div>
            <div className="mt-3 text-sm font-medium text-[var(--color-text)]">暂无 Google 账号</div>
            <div className="mt-1 text-xs text-[var(--color-text-muted)]">没有匹配账号。可以调整搜索条件，或用平台筛选反选查看未注册某平台的账号。</div>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1080px] text-sm">
              <thead className="border-b border-[var(--color-border)] bg-[var(--color-surface-hover)] text-xs text-[var(--color-text-muted)]">
                <tr>
                  <th className="px-5 py-3 text-left">Google 邮箱</th>
                  <th className="px-5 py-3 text-left">密码</th>
                  <th className="px-5 py-3 text-left">已注册平台</th>
                  <th className="px-5 py-3 text-left">来源</th>
                  <th className="px-5 py-3 text-left">订单号</th>
                  <th className="px-5 py-3 text-left">加入时间</th>
                  <th className="px-5 py-3 text-left">状态</th>
                  <th className="px-5 py-3 text-left">操作</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((item) => {
                  const platforms = item.registered_platforms || []
                  const isInvalid = (item.status || 'valid').toLowerCase() === 'invalid'
                  return (
                    <tr key={`${item.email}:${item.source_order_id || ''}`} className={`border-b border-[var(--color-border)]/50 hover:bg-[var(--color-surface-hover)]/60 ${isInvalid ? 'bg-red-500/5 opacity-75' : ''}`}>
                      <td className="px-5 py-3 font-medium text-[var(--color-text)]">{item.email}</td>
                      <td className="px-5 py-3 font-mono text-xs text-[var(--color-text)]">{item.password || '-'}</td>
                      <td className="px-5 py-3">
                        {platforms.length > 0 ? (
                          <div className="flex flex-wrap gap-1.5">
                            {platforms.map((platform) => <Badge key={platform} variant="default">{platform}</Badge>)}
                          </div>
                        ) : <span className="text-[var(--color-text-muted)]">未注册任何平台</span>}
                      </td>
                      <td className="px-5 py-3 text-[var(--color-text-secondary)]">{item.source || '-'}</td>
                      <td className="px-5 py-3 font-mono text-xs text-[var(--color-text-secondary)]">{item.source_order_id || '-'}</td>
                      <td className="px-5 py-3 text-[var(--color-text-secondary)]">{formatTime(item.added_at)}</td>
                      <td className="px-5 py-3">
                        {isInvalid ? (
                          <Badge variant="danger">已失效</Badge>
                        ) : (
                          <Badge variant={platforms.length > 0 ? 'secondary' : 'success'}>{platforms.length > 0 ? `已用 ${platforms.length} 次` : '可优先使用'}</Badge>
                        )}
                      </td>
                      <td className="px-5 py-3">
                        {isInvalid ? (
                          <Button size="sm" variant="outline" onClick={() => markAccountStatus(item, 'valid')} disabled={statusUpdatingEmail === item.email}>
                            <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                            恢复有效
                          </Button>
                        ) : (
                          <Button size="sm" variant="outline" onClick={() => markAccountStatus(item, 'invalid')} disabled={statusUpdatingEmail === item.email}>
                            <Ban className="mr-1.5 h-3.5 w-3.5" />
                            标注失效
                          </Button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
