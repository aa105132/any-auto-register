import { useEffect, useState, useCallback } from 'react'
import { useParams } from 'react-router-dom'
import { getPlatforms } from '@/lib/app-data'
import { apiFetch } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Plus, Upload, RefreshCw, Trash2, WalletCards, Inbox, ShieldCheck, ScanSearch } from 'lucide-react'
import type { Account } from '@/lib/account-utils'
import { getLifecycleStatus, getPlanState, getValidityStatus, getCashierUrl } from '@/lib/account-utils'
import { AccountsTable } from '@/pages/accounts/AccountsTable'
import { AccountDetailModal } from '@/pages/accounts/AccountDetailModal'
import { ImportModal } from '@/pages/accounts/ImportModal'
import { AddModal } from '@/pages/accounts/AddModal'
import { RegisterModal } from '@/pages/accounts/RegisterModal'
import { ExportMenu } from '@/pages/accounts/ExportMenu'
import { BatchActionMenu } from '@/pages/accounts/BatchActionMenu'
import { ActionResultModal } from '@/pages/accounts/ActionResultModal'
import { ActionTaskModal } from '@/pages/accounts/ActionTaskModal'

const STATUS_FILTER_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'trial', label: '试用中' },
  { value: 'subscribed', label: '已订阅' },
  { value: 'expired', label: '已过期' },
  { value: 'invalid', label: '已失效' },
]

const STATUS_FILTER_LABELS: Record<string, string> = Object.fromEntries(
  STATUS_FILTER_OPTIONS.map((o) => [o.value, o.label]),
)

function WorkspaceMetric({ label, value, icon: Icon, hint }: { label: string; value: number; icon: any; hint: string }) {
  return (
    <div className="workspace-metric-panel">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="workspace-kicker">{label}</div>
          <div className="workspace-metric-value tabular-nums">{value}</div>
          <div className="mt-1 text-xs text-[var(--color-text-secondary)]">{hint}</div>
        </div>
        <div className="workspace-metric-icon">
          <Icon className="h-4 w-4 text-[var(--color-accent)]" />
        </div>
      </div>
    </div>
  )
}


export default function Accounts() {
  const { platform } = useParams<{ platform: string }>()
  const [tab, setTab] = useState(platform || 'trae')
  useEffect(() => { if (platform) setTab(platform) }, [platform])

  const [accounts, setAccounts] = useState<Account[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [detail, setDetail] = useState<Account | null>(null)
  const [showImport, setShowImport] = useState(false)
  const [showAdd, setShowAdd] = useState(false)
  const [showRegister, setShowRegister] = useState(false)
  const [platformsMap, setPlatformsMap] = useState<Record<string, any>>({})
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [actionResult, setActionResult] = useState<{ title: string; payload: any } | null>(null)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [batchTask, setBatchTask] = useState<{ taskId: string; title: string; platform: string; actionId: string } | null>(null)
  const [batchTaskStatus, setBatchTaskStatus] = useState<string | null>(null)

  useEffect(() => {
    getPlatforms().then((list: any[]) => {
      const map: Record<string, any> = {}
      list.forEach((p) => { map[p.name] = p })
      setPlatformsMap(map)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 400)
    return () => clearTimeout(timer)
  }, [search])

  useEffect(() => { setSelectedIds(new Set()) }, [tab, filterStatus, debouncedSearch])

  const load = useCallback(async (p = tab, s = debouncedSearch, fs = filterStatus) => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ platform: p, page: '1', page_size: '100' })
      if (s) params.set('email', s)
      if (fs) params.set('status', fs)
      const data = await apiFetch(`/accounts?${params}`)
      setAccounts(data.items); setTotal(data.total)
    } finally { setLoading(false) }
  }, [tab, debouncedSearch, filterStatus])

  useEffect(() => { load(tab, debouncedSearch, filterStatus) }, [tab, debouncedSearch, filterStatus])

  useEffect(() => {
    setSelectedIds((prev) => {
      const visible = new Set(accounts.map((a) => a.id))
      return new Set([...prev].filter((id) => visible.has(id)))
    })
  }, [accounts])



  const pageIds = accounts.map((a) => a.id)
  const allSelectedOnPage = pageIds.length > 0 && pageIds.every((id) => selectedIds.has(id))
  const selectedCount = selectedIds.size
  const toggleOne = (id: number) => setSelectedIds((prev) => { const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next })
  const togglePage = () => setSelectedIds((prev) => { const next = new Set(prev); if (allSelectedOnPage) pageIds.forEach((id) => next.delete(id)); else pageIds.forEach((id) => next.add(id)); return next })

  const currentPlatformMeta = platformsMap[tab]
  const platformLabel = currentPlatformMeta?.display_name || tab
  const platformTone = currentPlatformMeta?.description || currentPlatformMeta?.summary || '集中查看账号、验证资源与状态。'
  const visibleTrial = accounts.filter((a) => getPlanState(a) === 'trial').length
  const visibleSubscribed = accounts.filter((a) => getPlanState(a) === 'subscribed').length
  const visibleInvalid = accounts.filter((a) => getValidityStatus(a) === 'invalid' || getLifecycleStatus(a) === 'invalid').length
  const linkedCashier = accounts.filter((a) => Boolean(getCashierUrl(a))).length
  const verificationBacked = accounts.filter((a) => Boolean(a.provider_resources?.find?.((r: any) => r?.resource_type === 'mailbox')?.handle)).length
  const filterStatusLabel = STATUS_FILTER_LABELS[filterStatus] || '全部状态'
  const hasFilters = Boolean(debouncedSearch || filterStatus)
  const currentScopeText = selectedCount > 0
    ? `已选 ${selectedCount} 个账号`
    : hasFilters ? `${debouncedSearch ? `关键词"${debouncedSearch}"` : '全部关键词'} · ${filterStatusLabel}` : '全部账号记录'

  return (
    <div className="flex flex-col gap-4 page-enter">
      {detail && <AccountDetailModal acc={detail} platform={tab} onClose={() => setDetail(null)} onSave={() => { setDetail(null); load() }} />}
      {showImport && <ImportModal platform={tab} onClose={() => setShowImport(false)} onDone={() => { setShowImport(false); load() }} />}
      {showAdd && <AddModal platform={tab} onClose={() => setShowAdd(false)} onDone={() => { setShowAdd(false); load() }} />}
      {showRegister && <RegisterModal platform={tab} platformMeta={platformsMap[tab]} onClose={() => setShowRegister(false)} onDone={() => load()} />}
      {actionResult && <ActionResultModal title={actionResult.title} payload={actionResult.payload} onClose={() => setActionResult(null)} />}
      {batchTask && (
        <ActionTaskModal title={batchTask.title} taskId={batchTask.taskId} taskStatus={batchTaskStatus}
          onClose={() => { setBatchTask(null); setBatchTaskStatus(null); load() }}
          onDone={(status: string) => setBatchTaskStatus(status)}
          onRetryFailed={(newTaskId: string, failedCount: number) => {
            setBatchTask({ taskId: newTaskId, title: `重跑失败 (${failedCount} 个)`, platform: batchTask.platform, actionId: batchTask.actionId })
            setBatchTaskStatus(null)
          }}
        />
      )}

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(340px,0.9fr)]">
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-5 py-5">
          <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
            <div className="min-w-0 space-y-3">
              <div className="workspace-kicker">账号资产 / {platformLabel}</div>
              <h1 className="text-[var(--text-2xl)] font-semibold tracking-tight text-[var(--color-text)]">{platformLabel} 账号工作台</h1>
              <p className="max-w-[66ch] text-sm text-[var(--color-text-secondary)]">{platformTone}</p>
              <div className="flex flex-wrap gap-2">
                <Badge variant="secondary">{selectedCount > 0 ? `已选 ${selectedCount}` : hasFilters ? '筛选结果' : '全部账号'}</Badge>
                <Badge variant="default">总量 {total}</Badge>
                <Badge variant="success">试用 {visibleTrial}</Badge>
                <Badge variant="default">订阅 {visibleSubscribed}</Badge>
                <Badge variant={visibleInvalid > 0 ? 'danger' : 'secondary'}>失效 {visibleInvalid}</Badge>
              </div>
            </div>
            <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 xl:max-w-[280px]">
              <div className="text-[11px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]">当前视图</div>
              <div className="mt-2 space-y-2">
                <div><div className="text-[11px] text-[var(--color-text-muted)]">检索范围</div><div className="text-sm text-[var(--color-text)]">{currentScopeText}</div></div>
                <div className="grid grid-cols-2 gap-2">
                  <div><div className="text-[11px] text-[var(--color-text-muted)]">状态</div><div className="text-sm font-medium text-[var(--color-text)]">{filterStatusLabel}</div></div>
                  <div><div className="text-[11px] text-[var(--color-text-muted)]">试用链接</div><div className="text-sm font-medium text-[var(--color-text)] tabular-nums">{linkedCashier}</div></div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <WorkspaceMetric label="当前总量" value={total} icon={WalletCards} hint="当前平台视图内记录数" />
          <WorkspaceMetric label="验证码邮箱" value={verificationBacked} icon={Inbox} hint="已挂接验证邮箱的账号" />
          <WorkspaceMetric label="已订阅" value={visibleSubscribed} icon={ShieldCheck} hint="当前列表里处于订阅状态" />
          <WorkspaceMetric label="待处理" value={visibleInvalid} icon={ScanSearch} hint="有效性或生命周期异常" />
        </div>
      </section>

      <Card className="overflow-visible p-0">
        <div className="divide-y divide-[var(--color-border)]">
          <section className="px-5 py-4">
            <div className="workspace-kicker">主操作</div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={() => setShowRegister(true)}><Plus className="mr-1 h-3.5 w-3.5" />自动注册</Button>
              <Button size="sm" variant="outline" onClick={() => setShowImport(true)}><Upload className="mr-1 h-3.5 w-3.5" />导入</Button>
              <ExportMenu platform={tab} total={total} statusFilter={filterStatus} searchFilter={debouncedSearch} selectedIds={[...selectedIds]} />
              <BatchActionMenu platform={tab} total={total} selectedIds={[...selectedIds]} statusFilter={filterStatus} searchFilter={debouncedSearch}
                onTaskCreated={(taskId: string, title: string, actionId: string) => { setBatchTask({ taskId, title, platform: tab, actionId }); setBatchTaskStatus(null) }} />
              <Button size="sm" variant="outline" onClick={() => setShowAdd(true)}>手动新增</Button>
              <Button variant="outline" size="sm" onClick={() => load()} disabled={loading}>
                <RefreshCw className={`mr-1 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />{loading ? '刷新中' : '刷新'}
              </Button>
              {selectedCount > 0 && (
                <Button size="sm" variant="destructive" disabled={bulkDeleting}
                  onClick={async () => {
                    if (!confirm(`确认删除选中的 ${selectedCount} 个账号？此操作不可撤销。`)) return
                    setBulkDeleting(true)
                    try { await Promise.allSettled([...selectedIds].map((id) => apiFetch(`/accounts/${id}`, { method: 'DELETE' }))); setSelectedIds(new Set()); load() }
                    finally { setBulkDeleting(false) }
                  }}><Trash2 className="mr-1 h-3.5 w-3.5" />{bulkDeleting ? '删除中...' : `删除已选 (${selectedCount})`}</Button>
              )}
            </div>
          </section>

          <section className="px-5 py-4">
            <div className="workspace-kicker">筛选与范围</div>
            <div className="mt-3 grid gap-3 xl:grid-cols-[minmax(260px,1.35fr)_minmax(180px,0.85fr)_minmax(200px,1fr)]">
              <div>
                <label className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]">邮箱搜索</label>
                <input type="text" placeholder="按邮箱搜索当前平台账号" value={search} onChange={(e) => setSearch(e.target.value)} className="input-field" />
              </div>
              <div>
                <label className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]">状态筛选</label>
                <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)} className="input-field select-field">
                  {STATUS_FILTER_OPTIONS.map((o) => <option key={o.value || 'all'} value={o.value}>{o.label}</option>)}
                </select>
              </div>
              <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2.5">
                <div className="text-[11px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]">当前结果</div>
                <div className="mt-1 text-sm text-[var(--color-text)]">{currentScopeText}</div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <Badge variant="secondary">{total} 账号</Badge>
                  {debouncedSearch && <Badge variant="default">关键词：{debouncedSearch}</Badge>}
                  {filterStatus ? <Badge variant="warning">{filterStatusLabel}</Badge> : <Badge variant="secondary">全部状态</Badge>}
                  {selectedCount > 0 && <Badge variant="success">已选 {selectedCount}</Badge>}
                </div>
              </div>
            </div>
          </section>
        </div>

        <AccountsTable accounts={accounts} loading={loading} selectedIds={selectedIds}
          toggleOne={toggleOne} togglePage={togglePage} allSelectedOnPage={allSelectedOnPage}
          onDetail={setDetail} tab={tab} search={debouncedSearch} filterStatus={filterStatus} />
      </Card>
    </div>
  )
}
