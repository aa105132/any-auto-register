import { useEffect, useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { apiFetch } from '@/lib/utils'
import { Ban, Clock3, CreditCard, Database, Plus, RefreshCw, RotateCcw, ShieldCheck, Trash2, Upload } from 'lucide-react'

type CreditCardPoolItem = {
  id: string
  number: string
  exp_month: string
  exp_year: string
  cvv: string
  country: string
  address: string
  city: string
  postal_code: string
  state: string
  name: string
  last4: string
  brand_hint: string
  source: string
  status: string
  note: string
  usage_count: number
  used_platforms: string[]
  last_used_at: string
  last_used_platform: string
  last_used_email: string
  added_at: string
  updated_at: string
}

type PoolResponse = {
  source?: string
  stats?: {
    total?: number
    valid?: number
    invalid?: number
    used?: number
    by_brand?: Record<string, number>
  }
  items?: CreditCardPoolItem[]
}

function formatTime(value?: string) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function formatExpiry(item: CreditCardPoolItem) {
  const month = String(item.exp_month || '').padStart(2, '0')
  const year = String(item.exp_year || '')
  return month && year ? `${month}/${year}` : '-'
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

const EXAMPLE = `5200000000000000|12|2029|123|US|Example Billing Address|Aloha|97003|Oregon|Zo User
5200000000000001|12/29|123|US|Example Billing Address|Aloha|97003|Oregon|Zo User`

export default function CreditCardPool() {
  const [data, setData] = useState<PoolResponse>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [importText, setImportText] = useState('')
  const [importing, setImporting] = useState(false)
  const [updatingId, setUpdatingId] = useState('')
  const [deletingInvalid, setDeletingInvalid] = useState(false)

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const suffix = statusFilter ? `?status=${encodeURIComponent(statusFilter)}` : ''
      setData(await apiFetch(`/credit-card-pool${suffix}`))
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载信用卡池失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [statusFilter])

  const importCards = async () => {
    const lines = importText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
    if (lines.length === 0) {
      setError('请先粘贴要导入的信用卡，每行一个，或使用“卡号/有效期/CVV/账单地址”块格式')
      return
    }
    setImporting(true)
    setError('')
    setNotice('')
    try {
      const result = await apiFetch('/credit-card-pool/import', {
        method: 'POST',
        body: JSON.stringify({ lines, source: 'manual' }),
      })
      setNotice(`导入完成：新增 ${result?.created || 0}，更新 ${result?.updated || 0}，无效 ${result?.invalid || 0}`)
      setImportText('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入信用卡失败')
    } finally {
      setImporting(false)
    }
  }

  const markStatus = async (item: CreditCardPoolItem, status: 'valid' | 'invalid') => {
    setUpdatingId(item.id)
    setError('')
    setNotice('')
    try {
      await apiFetch(`/credit-card-pool/${encodeURIComponent(item.id)}/${status}`, {
        method: 'POST',
        body: status === 'invalid' ? JSON.stringify({ reason: '前端手动标注失效' }) : undefined,
      })
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新信用卡状态失败')
    } finally {
      setUpdatingId('')
    }
  }

  const deleteInvalid = async () => {
    const count = (data.items || []).filter((item) => (item.status || 'valid').toLowerCase() === 'invalid').length
    if (count <= 0) {
      setNotice('当前没有已失效信用卡可删除')
      return
    }
    if (!window.confirm(`确认删除 ${count} 张已失效信用卡？此操作不可撤销。`)) return
    setDeletingInvalid(true)
    setError('')
    setNotice('')
    try {
      const result = await apiFetch('/credit-card-pool/invalid', { method: 'DELETE' })
      setNotice(`已删除 ${result?.deleted || 0} 张失效信用卡`)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失效信用卡失败')
    } finally {
      setDeletingInvalid(false)
    }
  }

  const items = data.items || []
  const stats = data.stats || {}
  const byBrand = Object.entries(stats.by_brand || {}).sort((a, b) => b[1] - a[1])
  const invalidCount = items.filter((item) => (item.status || 'valid').toLowerCase() === 'invalid').length
  const reusableCount = items.filter((item) => (item.status || 'valid').toLowerCase() !== 'invalid').length

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return items
    return items.filter((item) => {
      const text = [
        item.number,
        item.cvv,
        item.brand_hint,
        item.country,
        item.address,
        item.city,
        item.postal_code,
        item.state,
        item.name,
        item.status,
        item.note,
        item.last_used_platform,
        item.last_used_email,
        ...(item.used_platforms || []),
      ].join(' ').toLowerCase()
      return text.includes(q)
    })
  }, [items, query])

  return (
    <div className="page-enter space-y-4">
      <section className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.65fr)]">
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-5 py-5">
          <div className="workspace-kicker">账号资产 / 信用卡池</div>
          <h1 className="mt-2 text-[var(--text-2xl)] font-semibold tracking-tight text-[var(--color-text)]">信用卡池</h1>
          <p className="mt-2 max-w-[72ch] text-sm leading-6 text-[var(--color-text-secondary)]">
            管理靶场注册链路的绑卡资料。Zo 注册会优先使用任务 extra，其次环境变量，最后自动从这里取第一张有效卡，并记录使用平台和账号。
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={load} disabled={loading}>
              <RefreshCw className={`mr-1 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
              {loading ? '刷新中' : '刷新'}
            </Button>
            <a href="#credit-card-pool-import" className="inline-flex h-8 items-center justify-center rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 text-xs font-medium text-[var(--color-text-secondary)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-text)]">
              <Plus className="mr-1 h-3.5 w-3.5" />手动导入
            </a>
            <Button size="sm" variant="destructive" onClick={deleteInvalid} disabled={deletingInvalid || invalidCount === 0}>
              <Trash2 className="mr-1 h-3.5 w-3.5" />{deletingInvalid ? '删除中...' : `删除失效卡 (${invalidCount})`}
            </Button>
            <Badge variant="secondary">来源 {data.source || 'output/credit_cards_pool.json'}</Badge>
          </div>
        </div>
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-4">
          <div className="workspace-kicker">卡组织分布</div>
          <div className="mt-3 flex flex-wrap gap-2">
            {byBrand.length > 0 ? byBrand.map(([brand, count]) => (
              <Badge key={brand} variant="secondary">{brand} · {count}</Badge>
            )) : <span className="text-sm text-[var(--color-text-muted)]">暂无信用卡</span>}
          </div>
        </div>
      </section>

      <Card id="credit-card-pool-import" className="p-0 overflow-hidden">
        <div className="grid gap-4 p-5 xl:grid-cols-[minmax(0,1fr)_340px]">
          <div>
            <div className="workspace-kicker">手动 / 批量导入</div>
            <h2 className="mt-1 text-base font-semibold text-[var(--color-text)]">导入信用卡</h2>
            <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
              一行一张卡：<span className="font-mono">卡号|月|年|CVV|国家|账单地址|城市|邮编|州|持卡人</span>。也支持粘贴“卡号: ... / 有效期: ... / CVV: ...”块格式。
            </p>
            <textarea
              value={importText}
              onChange={(event) => setImportText(event.target.value)}
              rows={7}
              placeholder="卡号|月|年|CVV|国家|账单地址|城市|邮编|州|持卡人"
              className="control-surface control-surface-mono mt-3 resize-y"
            />
            <div className="mt-3 flex flex-wrap gap-2">
              <Button size="sm" onClick={importCards} disabled={importing}>
                <Upload className="mr-1 h-3.5 w-3.5" />{importing ? '导入中...' : '批量导入'}
              </Button>
              <Button size="sm" variant="outline" onClick={() => setImportText(EXAMPLE)}>填入示例</Button>
            </div>
          </div>
          <aside className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
            <div className="workspace-kicker">Zo 绑定顺序</div>
            <div className="mt-3 space-y-2 text-xs text-[var(--color-text-secondary)]">
              <div>1. 任务参数 <span className="font-mono text-[var(--color-text)]">extra.zo_card</span></div>
              <div>2. 环境变量 <span className="font-mono text-[var(--color-text)]">ZO_CARD_*</span></div>
              <div>3. Web 信用卡池第一张有效卡</div>
              <div className="pt-2 text-[var(--color-text-muted)]">这里是本地自用页面，按你的要求完整显示卡号与 CVV，方便快速复制和核对。</div>
            </div>
            {notice ? <div className="mt-4 rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">{notice}</div> : null}
          </aside>
        </div>
      </Card>

      <section className="grid gap-3 md:grid-cols-4">
        <Metric label="总卡数" value={stats.total ?? items.length} hint="池内保存的信用卡总量" icon={Database} />
        <Metric label="有效卡" value={stats.valid ?? reusableCount} hint="可被注册任务自动领取" icon={CreditCard} />
        <Metric label="已使用" value={stats.used ?? items.filter((item) => item.usage_count > 0).length} hint="至少被一个平台注册链路使用过" icon={ShieldCheck} />
        <Metric label="失效卡" value={stats.invalid ?? invalidCount} hint="失效卡不会被自动复用" icon={Ban} />
      </section>

      <Card className="p-0 overflow-hidden">
        <div className="border-b border-[var(--color-border)] px-5 py-4">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
            <div>
              <div className="workspace-kicker">池内信用卡</div>
              <div className="mt-1 text-sm text-[var(--color-text-secondary)]">完整显示卡号、CVV 和账单地址；失效卡不会被 Zo 自动取用。</div>
            </div>
            <div className="grid gap-2 md:grid-cols-[260px_160px_auto]">
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索卡号 / CVV / 地址 / 平台 / 邮箱"
                className="control-surface control-surface-compact"
              />
              <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className="control-surface control-surface-compact appearance-none">
                <option value="">全部状态</option>
                <option value="valid">有效</option>
                <option value="invalid">失效</option>
              </select>
              <Button size="sm" variant="outline" onClick={() => { setQuery(''); setStatusFilter('') }}>重置筛选</Button>
            </div>
          </div>
          <div className="mt-3 flex flex-wrap gap-2 text-xs text-[var(--color-text-muted)]">
            <Badge variant="secondary">匹配 {filtered.length} / {items.length}</Badge>
          </div>
        </div>

        {error ? <div className="m-5 rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div> : null}

        {filtered.length === 0 ? (
          <div className="px-5 py-12 text-center">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text-muted)]">
              <Clock3 className="h-5 w-5" />
            </div>
            <div className="mt-3 text-sm font-medium text-[var(--color-text)]">暂无信用卡</div>
            <div className="mt-1 text-xs text-[var(--color-text-muted)]">导入后，Zo 注册链路可以自动取用有效卡完成绑卡。</div>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1280px] text-sm">
              <thead className="border-b border-[var(--color-border)] bg-[var(--color-surface-hover)] text-xs text-[var(--color-text-muted)]">
                <tr>
                  <th className="px-5 py-3 text-left">卡号</th>
                  <th className="px-5 py-3 text-left">有效期</th>
                  <th className="px-5 py-3 text-left">CVV</th>
                  <th className="px-5 py-3 text-left">账单地址</th>
                  <th className="px-5 py-3 text-left">使用记录</th>
                  <th className="px-5 py-3 text-left">状态</th>
                  <th className="px-5 py-3 text-left">更新时间</th>
                  <th className="px-5 py-3 text-left">操作</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((item) => {
                  const isInvalid = (item.status || 'valid').toLowerCase() === 'invalid'
                  return (
                    <tr key={item.id} className={`border-b border-[var(--color-border)]/50 hover:bg-[var(--color-surface-hover)]/60 ${isInvalid ? 'bg-red-500/5 opacity-75' : ''}`}>
                      <td className="px-5 py-3">
                        <div className="font-mono text-sm font-semibold text-[var(--color-text)]">{item.number}</div>
                        <div className="mt-1 flex flex-wrap gap-1.5">
                          <Badge variant="secondary">{item.brand_hint || 'unknown'}</Badge>
                          <span className="text-[11px] text-[var(--color-text-muted)]">尾号 {item.last4 || '-'}</span>
                        </div>
                      </td>
                      <td className="px-5 py-3 font-mono text-xs text-[var(--color-text)]">{formatExpiry(item)}</td>
                      <td className="px-5 py-3 font-mono text-xs text-[var(--color-text)]">{item.cvv}</td>
                      <td className="px-5 py-3 text-[var(--color-text-secondary)]">
                        <div className="max-w-[340px] break-words">{item.address}</div>
                        <div className="mt-1 text-xs text-[var(--color-text-muted)]">{[item.city, item.state, item.postal_code, item.country].filter(Boolean).join(', ') || '-'}</div>
                        {item.name ? <div className="mt-1 text-xs text-[var(--color-text-muted)]">持卡人：{item.name}</div> : null}
                      </td>
                      <td className="px-5 py-3 text-[var(--color-text-secondary)]">
                        <div>次数：{item.usage_count || 0}</div>
                        <div className="mt-1 text-xs text-[var(--color-text-muted)]">平台：{(item.used_platforms || []).join(', ') || '-'}</div>
                        <div className="mt-1 max-w-[220px] truncate text-xs text-[var(--color-text-muted)]">账号：{item.last_used_email || '-'}</div>
                      </td>
                      <td className="px-5 py-3">
                        <Badge variant={isInvalid ? 'danger' : 'success'}>{isInvalid ? '已失效' : '有效'}</Badge>
                      </td>
                      <td className="px-5 py-3 text-[var(--color-text-secondary)] tabular-nums">{formatTime(item.updated_at || item.added_at)}</td>
                      <td className="px-5 py-3">
                        {isInvalid ? (
                          <Button size="sm" variant="outline" onClick={() => markStatus(item, 'valid')} disabled={updatingId === item.id}>
                            <RotateCcw className="mr-1.5 h-3.5 w-3.5" />恢复有效
                          </Button>
                        ) : (
                          <Button size="sm" variant="outline" onClick={() => markStatus(item, 'invalid')} disabled={updatingId === item.id}>
                            <Ban className="mr-1.5 h-3.5 w-3.5" />标注失效
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