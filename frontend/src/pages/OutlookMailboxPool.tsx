import { Fragment, useEffect, useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { apiDownload, apiFetch, triggerBrowserDownload } from '@/lib/utils'
import { Ban, Clock3, Download, Inbox, Mail, RefreshCw, RotateCcw, Upload, WalletCards } from 'lucide-react'

type MailboxInventoryItem = {
  id: number
  provider_key: string
  email: string
  token_preview: string
  status: string
  note: string
  last_error: string
  last_task_id?: string
  last_platform?: string
  created_at?: string | null
  updated_at?: string | null
  metadata?: Record<string, unknown>
}

type MailboxInventoryRow =
  | { kind: 'plain'; item: MailboxInventoryItem }
  | { kind: 'outlook_group'; item: MailboxInventoryItem; children: MailboxInventoryItem[] }
  | { kind: 'outlook_alias_orphan'; item: MailboxInventoryItem }

type MailboxInventoryResponse = {
  items?: MailboxInventoryItem[]
  counts?: Record<string, number>
}

const PROVIDER_KEY = 'outlook_token'

const STATUS_VARIANT: Record<string, any> = {
  unused: 'secondary',
  running: 'warning',
  registered: 'success',
  blacklisted: 'danger',
  oauth_pending: 'warning',
  register_failed: 'danger',
  existing_account: 'warning',
  existing_suspected: 'warning',
}

const STATUS_LABEL: Record<string, string> = {
  unused: '未使用',
  running: '运行中',
  registered: '已注册',
  blacklisted: '已拉黑',
  oauth_pending: '待 OAuth',
  register_failed: '注册失败',
  existing_account: '已存在账号',
  existing_suspected: '疑似已注册',
}

function formatTime(value?: string | null) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function getInventoryUsedPlatforms(metadata?: Record<string, unknown>) {
  const raw = metadata?.used_platforms
  if (!Array.isArray(raw)) return []
  return raw.filter(Boolean).map((item) => String(item))
}

function isOutlookAliasInventoryItem(item: MailboxInventoryItem) {
  const metadata = item.metadata || {}
  const source = String(metadata.source || '').trim().toLowerCase()
  const local = String(item.email || '').split('@')[0] || ''
  return item.provider_key === PROVIDER_KEY
    && (source === 'outlook_alias_auto'
      || Boolean(metadata.alias_parent_email || metadata.outlook_login_email)
      || local.includes('+'))
}

function normalizeEmailKey(value: string) {
  return String(value || '').trim().toLowerCase()
}

function getOutlookAliasParentEmail(item: MailboxInventoryItem) {
  const metadata = item.metadata || {}
  const directParent = String(metadata.alias_parent_email || metadata.outlook_login_email || '').trim()
  if (directParent) return directParent
  const email = String(item.email || '').trim()
  if (!email.includes('@')) return ''
  const [local, domain] = email.split('@', 2)
  if (!local.includes('+')) return ''
  return `${local.split('+', 1)[0]}@${domain}`
}

function getOutlookAliasParentKey(item: MailboxInventoryItem) {
  const parentEmail = getOutlookAliasParentEmail(item)
  return parentEmail ? normalizeEmailKey(parentEmail) : ''
}

function groupOutlookAliasItems(items: MailboxInventoryItem[]): MailboxInventoryRow[] {
  const parentKeySet = new Set<string>()
  for (const item of items) {
    if (!isOutlookAliasInventoryItem(item)) {
      parentKeySet.add(normalizeEmailKey(item.email))
    }
  }

  const groupedChildren = new Map<string, MailboxInventoryItem[]>()
  for (const item of items) {
    if (!isOutlookAliasInventoryItem(item)) continue
    const parentKey = getOutlookAliasParentKey(item)
    if (!parentKey || !parentKeySet.has(parentKey)) continue
    const current = groupedChildren.get(parentKey) || []
    current.push(item)
    groupedChildren.set(parentKey, current)
  }

  const emittedParents = new Set<string>()
  const rows: MailboxInventoryRow[] = []
  for (const item of items) {
    if (isOutlookAliasInventoryItem(item)) {
      const parentKey = getOutlookAliasParentKey(item)
      if (parentKey && parentKeySet.has(parentKey)) continue
      rows.push({ kind: 'outlook_alias_orphan', item })
      continue
    }

    const parentKey = normalizeEmailKey(item.email)
    if (emittedParents.has(parentKey)) continue
    emittedParents.add(parentKey)
    const children = groupedChildren.get(parentKey) || []
    rows.push(children.length > 0 ? { kind: 'outlook_group', item, children } : { kind: 'plain', item })
  }
  return rows
}

function flattenRow(row: MailboxInventoryRow) {
  return row.kind === 'outlook_group' ? [row.item, ...row.children] : [row.item]
}

function Metric({ label, value, hint, icon: Icon }: { label: string; value: string | number; hint: string; icon: any }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-[var(--color-text-muted)]">{label}</div>
          <div className="mt-1 text-2xl font-semibold tabular-nums text-[var(--color-text)]">{value}</div>
        </div>
        <div className="flex h-9 w-9 items-center justify-center rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-accent-text)]">
          <Icon className="h-4 w-4" />
        </div>
      </div>
      <div className="mt-2 text-xs text-[var(--color-text-muted)]">{hint}</div>
    </div>
  )
}

export default function OutlookMailboxPool() {
  const [data, setData] = useState<MailboxInventoryResponse>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [importText, setImportText] = useState('')
  const [importing, setImporting] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [resetting, setResetting] = useState<Record<number, boolean>>({})

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      setData(await apiFetch(`/mailbox-inventory?provider_key=${encodeURIComponent(PROVIDER_KEY)}`))
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载 Outlook 邮箱池失败')
    } finally {
      setLoading(false)
    }
  }

  const importLines = async () => {
    const lines = importText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
    if (lines.length === 0) {
      setError('请先粘贴 Outlook 令牌邮箱，每行一个')
      return
    }
    setImporting(true)
    setError('')
    setNotice('')
    try {
      const result = await apiFetch('/mailbox-inventory/import', {
        method: 'POST',
        body: JSON.stringify({ provider_key: PROVIDER_KEY, lines }),
      })
      setNotice(`导入完成：新增 ${result?.created || 0}，更新 ${result?.updated || 0}，跳过 ${result?.skipped || 0}`)
      setImportText('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入 Outlook 邮箱失败')
    } finally {
      setImporting(false)
    }
  }

  const exportLines = async () => {
    setExporting(true)
    setError('')
    try {
      const { blob, filename } = await apiDownload(`/mailbox-inventory/export?provider_key=${encodeURIComponent(PROVIDER_KEY)}`)
      triggerBrowserDownload(blob, filename)
      setNotice('已导出 Outlook 邮箱池')
    } catch (err) {
      setError(err instanceof Error ? err.message : '导出 Outlook 邮箱池失败')
    } finally {
      setExporting(false)
    }
  }

  const resetItem = async (itemId: number) => {
    setResetting((current) => ({ ...current, [itemId]: true }))
    setError('')
    try {
      await apiFetch(`/mailbox-inventory/${itemId}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'unused', last_error: '' }),
      })
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '重置邮箱状态失败')
    } finally {
      setResetting((current) => ({ ...current, [itemId]: false }))
    }
  }

  const fillExample = () => setImportText('demo@outlook.com----mail-pass----000000004C12AE6F----0.ABC')

  useEffect(() => { load() }, [])

  const items = data.items || []
  const counts = data.counts || {}
  const aliasCount = items.filter(isOutlookAliasInventoryItem).length
  const parentCount = items.length - aliasCount
  const unusedCount = counts.unused || 0
  const blacklistedCount = counts.blacklisted || 0

  const rows = useMemo(() => groupOutlookAliasItems(items), [items])
  const filteredRows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const targetStatus = statusFilter.trim().toLowerCase()
    return rows.filter((row) => {
      const entries = flattenRow(row)
      if (targetStatus && !entries.some((item) => String(item.status || '').trim().toLowerCase() === targetStatus)) return false
      if (!q) return true
      return entries.some((item) => {
        const platforms = getInventoryUsedPlatforms(item.metadata).join(' ')
        return `${item.email} ${item.token_preview || ''} ${item.status || ''} ${item.note || ''} ${item.last_error || ''} ${item.last_platform || ''} ${platforms}`.toLowerCase().includes(q)
      })
    })
  }, [rows, query, statusFilter])

  const statusOptions = Object.keys(STATUS_LABEL)

  return (
    <div className="page-enter space-y-4">
      <section className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.65fr)]">
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-5 py-5">
          <div className="workspace-kicker">账号资产 / Outlook 邮箱池</div>
          <h1 className="mt-2 text-[var(--text-2xl)] font-semibold tracking-tight text-[var(--color-text)]">Outlook 邮箱池</h1>
          <p className="mt-2 max-w-[72ch] text-sm leading-6 text-[var(--color-text-secondary)]">
            独立管理 Outlook refresh token 邮箱。父邮箱保持令牌和收信能力，自动生成的子邮箱会收纳在父邮箱二级列表里，注册任务可直接复用未使用项。
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={load} disabled={loading}>
              <RefreshCw className={`mr-1 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
              {loading ? '刷新中' : '刷新'}
            </Button>
            <a href="#outlook-pool-import" className="inline-flex h-8 items-center justify-center rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 text-xs font-medium text-[var(--color-text-secondary)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-text)]">
              <Upload className="mr-1 h-3.5 w-3.5" />导入令牌邮箱
            </a>
            <Button size="sm" variant="outline" onClick={exportLines} disabled={exporting || loading || items.length === 0}>
              <Download className="mr-1 h-3.5 w-3.5" />{exporting ? '导出中...' : '导出'}
            </Button>
            <Badge variant="secondary">来源 mailbox_inventory / outlook_token</Badge>
          </div>
        </div>
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-4">
          <div className="workspace-kicker">状态分布</div>
          <div className="mt-3 flex flex-wrap gap-2">
            {statusOptions.map((status) => (
              <Badge key={status} variant={STATUS_VARIANT[status] || 'secondary'}>
                {STATUS_LABEL[status] || status} {counts[status] || 0}
              </Badge>
            ))}
          </div>
        </div>
      </section>

      <Card id="outlook-pool-import" className="p-0 overflow-hidden">
        <div className="grid gap-4 p-5 xl:grid-cols-[minmax(0,1fr)_320px]">
          <div>
            <div className="workspace-kicker">手动 / 批量导入</div>
            <h2 className="mt-1 text-base font-semibold text-[var(--color-text)]">导入 Outlook 令牌邮箱</h2>
            <p className="mt-1 text-sm text-[var(--color-text-secondary)]">一行一个邮箱，格式为 <span className="font-mono">email----password----client_id----refresh_token</span>。重复邮箱会更新令牌信息。</p>
            <textarea
              value={importText}
              onChange={(event) => setImportText(event.target.value)}
              rows={6}
              placeholder="email----password----client_id----refresh_token\ndemo@outlook.com----mail-pass----000000004C12AE6F----0.ABC"
              className="control-surface control-surface-mono mt-3 resize-y"
            />
            <div className="mt-3 flex flex-wrap gap-2">
              <Button size="sm" onClick={importLines} disabled={importing}>
                <Upload className="mr-1 h-3.5 w-3.5" />{importing ? '导入中...' : '批量导入'}
              </Button>
              <Button size="sm" variant="outline" onClick={fillExample}>填入示例</Button>
            </div>
          </div>
          <aside className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
            <div className="workspace-kicker">复用规则</div>
            <div className="mt-3 space-y-2 text-xs text-[var(--color-text-secondary)]">
              <div>只从 <span className="font-mono text-[var(--color-text)]">unused</span> 状态取号。</div>
              <div>验证码超时会进入黑名单，可手动拉回。</div>
              <div>父邮箱可生成子邮箱，子邮箱显示在父邮箱下方。</div>
              <div>注册成功后记录已用平台，避免同平台重复使用。</div>
            </div>
            {notice ? (
              <div className="mt-4 rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">{notice}</div>
            ) : null}
          </aside>
        </div>
      </Card>

      <section className="grid gap-3 md:grid-cols-4">
        <Metric label="总邮箱" value={items.length} hint="Outlook 父邮箱和自动子邮箱总量" icon={Inbox} />
        <Metric label="父邮箱" value={parentCount} hint="持有 refresh token 的基础邮箱" icon={Mail} />
        <Metric label="可用" value={unusedCount} hint="当前可被注册任务领取" icon={WalletCards} />
        <Metric label="已拉黑" value={blacklistedCount} hint="验证码超时或异常后暂停复用" icon={Ban} />
      </section>

      <Card className="p-0 overflow-hidden">
        <div className="border-b border-[var(--color-border)] px-5 py-4">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
            <div>
              <div className="workspace-kicker">池内邮箱</div>
              <div className="mt-1 text-sm text-[var(--color-text-secondary)]">父邮箱与子邮箱合并展示；子邮箱保持二级列表，方便检查复用链路。</div>
            </div>
            <div className="grid gap-2 md:grid-cols-[260px_180px_auto]">
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索邮箱 / 平台 / 错误 / 状态"
                className="control-surface control-surface-compact"
              />
              <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className="control-surface control-surface-compact appearance-none">
                <option value="">全部状态</option>
                {statusOptions.map((status) => <option key={status} value={status}>{STATUS_LABEL[status] || status}</option>)}
              </select>
              <Button size="sm" variant="outline" onClick={() => { setQuery(''); setStatusFilter('') }}>重置筛选</Button>
            </div>
          </div>
          <div className="mt-3 flex flex-wrap gap-2 text-xs text-[var(--color-text-muted)]">
            <Badge variant="secondary">匹配 {filteredRows.length} / {rows.length} 组</Badge>
            {statusFilter ? <Badge variant={STATUS_VARIANT[statusFilter] || 'secondary'}>{STATUS_LABEL[statusFilter] || statusFilter}</Badge> : null}
          </div>
        </div>

        {error ? (
          <div className="m-5 rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div>
        ) : null}

        {filteredRows.length === 0 ? (
          <div className="px-5 py-12 text-center">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text-muted)]">
              <Clock3 className="h-5 w-5" />
            </div>
            <div className="mt-3 text-sm font-medium text-[var(--color-text)]">暂无 Outlook 邮箱</div>
            <div className="mt-1 text-xs text-[var(--color-text-muted)]">没有匹配邮箱。可以调整筛选，或导入 Outlook refresh token 邮箱。</div>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1120px] text-sm">
              <thead className="border-b border-[var(--color-border)] bg-[var(--color-surface-hover)] text-xs text-[var(--color-text-muted)]">
                <tr>
                  <th className="px-5 py-3 text-left">邮箱</th>
                  <th className="px-5 py-3 text-left">Token</th>
                  <th className="px-5 py-3 text-left">状态</th>
                  <th className="px-5 py-3 text-left">已用平台</th>
                  <th className="px-5 py-3 text-left">备注 / 错误</th>
                  <th className="px-5 py-3 text-left">更新时间</th>
                  <th className="px-5 py-3 text-left">操作</th>
                </tr>
              </thead>
              <tbody>
                {filteredRows.map((row) => {
                  const item = row.item
                  const usedPlatforms = getInventoryUsedPlatforms(item.metadata)
                  const isAliasGroup = row.kind === 'outlook_group'
                  const isAliasOrphan = row.kind === 'outlook_alias_orphan'
                  return (
                    <Fragment key={item.id}>
                      <tr className="border-b border-[var(--color-border)]/50 hover:bg-[var(--color-surface-hover)]/60">
                        <td className="px-5 py-3">
                          <div className="max-w-[240px] break-all font-medium text-[var(--color-text)]">{item.email}</div>
                          {isAliasGroup ? (
                            <div className="mt-1 flex flex-wrap items-center gap-1.5">
                              <Badge variant="warning">子邮箱组</Badge>
                              <span className="text-[11px] text-[var(--color-text-muted)]">子邮箱 {row.children.length} 个</span>
                            </div>
                          ) : isAliasOrphan ? (
                            <div className="mt-1 flex flex-wrap items-center gap-1.5">
                              <Badge variant="warning">子邮箱</Badge>
                              <span className="text-[11px] text-[var(--color-text-muted)]">父邮箱 {getOutlookAliasParentEmail(item) || '-'}</span>
                            </div>
                          ) : null}
                        </td>
                        <td className="px-5 py-3 font-mono text-xs text-[var(--color-text-secondary)]">{item.token_preview || '-'}</td>
                        <td className="px-5 py-3"><Badge variant={STATUS_VARIANT[item.status] || 'secondary'}>{STATUS_LABEL[item.status] || item.status}</Badge></td>
                        <td className="px-5 py-3 text-[var(--color-text-secondary)]">{usedPlatforms.length > 0 ? usedPlatforms.join(', ') : '-'}</td>
                        <td className="px-5 py-3 text-[var(--color-text-secondary)]">
                          <div className="max-w-[280px] truncate">{item.note || '-'}</div>
                          {item.last_error ? <div className="mt-1 max-w-[280px] truncate text-red-300">{item.last_error}</div> : null}
                        </td>
                        <td className="px-5 py-3 text-[var(--color-text-secondary)] tabular-nums">{formatTime(item.updated_at)}</td>
                        <td className="px-5 py-3">
                          <Button size="sm" variant="outline" onClick={() => resetItem(item.id)} disabled={!!resetting[item.id]}>
                            <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                            {resetting[item.id] ? '重置中...' : item.status === 'blacklisted' ? '从黑名单拉回' : '重置为未使用'}
                          </Button>
                        </td>
                      </tr>
                      {isAliasGroup ? (
                        <tr className="border-b border-[var(--color-border)]/50 bg-[var(--color-surface-hover)]/40">
                          <td colSpan={7} className="px-5 py-3">
                            <div className="rounded-lg border border-dashed border-[var(--color-border)]/80 bg-[var(--color-surface)] px-3 py-2">
                              <div className="text-[11px] font-medium uppercase tracking-[0.16em] text-[var(--color-text-muted)]">子邮箱列表</div>
                              <div className="mt-2 space-y-1.5">
                                {row.children.map((child) => {
                                  const childPlatforms = getInventoryUsedPlatforms(child.metadata)
                                  return (
                                    <div key={child.id} className="flex flex-wrap items-center justify-between gap-3 rounded-md bg-[var(--color-surface-hover)]/60 px-3 py-2">
                                      <div className="min-w-0">
                                        <div className="break-all text-sm text-[var(--color-text)]">{child.email}</div>
                                        <div className="mt-1 flex flex-wrap items-center gap-1.5">
                                          <Badge variant={STATUS_VARIANT[child.status] || 'secondary'}>{STATUS_LABEL[child.status] || child.status}</Badge>
                                          <span className="text-[11px] text-[var(--color-text-muted)]">{childPlatforms.length > 0 ? childPlatforms.join(', ') : '-'}</span>
                                        </div>
                                      </div>
                                      <Button size="sm" variant="outline" onClick={() => resetItem(child.id)} disabled={!!resetting[child.id]}>
                                        <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                                        {resetting[child.id] ? '重置中...' : child.status === 'blacklisted' ? '从黑名单拉回' : '重置为未使用'}
                                      </Button>
                                    </div>
                                  )
                                })}
                              </div>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
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
