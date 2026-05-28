import { useEffect, useMemo, useState } from 'react'
import { apiFetch } from '@/lib/utils'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Activity,
  CircleOff,
  Globe2,
  Plus,
  RefreshCw,
  ShieldCheck,
  ToggleLeft,
  ToggleRight,
  Trash2,
} from 'lucide-react'

function formatSuccessRate(success: number, fail: number) {
  const total = success + fail
  if (total <= 0) return '暂无'
  return `${Math.round((success / total) * 100)}%`
}

export default function Proxies() {
  const [proxies, setProxies] = useState<any[]>([])
  const [newProxy, setNewProxy] = useState('')
  const [region, setRegion] = useState('')
  const [checking, setChecking] = useState(false)

  const load = () => apiFetch('/proxies').then(setProxies)

  useEffect(() => {
    load()
  }, [])

  const add = async () => {
    if (!newProxy.trim()) return
    const lines = newProxy.trim().split('\n').map((item) => item.trim()).filter(Boolean)
    if (lines.length > 1) {
      await apiFetch('/proxies/bulk', {
        method: 'POST',
        body: JSON.stringify({ proxies: lines, region }),
      })
    } else {
      await apiFetch('/proxies', {
        method: 'POST',
        body: JSON.stringify({ url: lines[0], region }),
      })
    }
    setNewProxy('')
    load()
  }

  const del = async (id: number) => {
    await apiFetch(`/proxies/${id}`, { method: 'DELETE' })
    load()
  }

  const toggle = async (id: number) => {
    await apiFetch(`/proxies/${id}/toggle`, { method: 'PATCH' })
    load()
  }

  const check = async () => {
    setChecking(true)
    await apiFetch('/proxies/check', { method: 'POST' })
    setTimeout(() => {
      load()
      setChecking(false)
    }, 3000)
  }

  const activeCount = proxies.filter((item) => item.is_active).length
  const disabledCount = Math.max(proxies.length - activeCount, 0)
  const totalSuccess = proxies.reduce((sum, item) => sum + Number(item.success_count || 0), 0)
  const totalFail = proxies.reduce((sum, item) => sum + Number(item.fail_count || 0), 0)
  const noisyCount = proxies.filter((item) => Number(item.fail_count || 0) > Number(item.success_count || 0)).length
  const successRate = formatSuccessRate(totalSuccess, totalFail)

  const metricCards = useMemo(
    () => [
      {
        label: '代理总量',
        value: proxies.length,
        note: '当前代理池内全部节点',
        icon: Globe2,
        tone: 'text-[var(--color-accent)]',
      },
      {
        label: '已启用',
        value: activeCount,
        note: '正在参与调度的可用代理',
        icon: ShieldCheck,
        tone: 'text-emerald-400',
      },
      {
        label: '成功次数',
        value: totalSuccess,
        note: '累计成功请求记录',
        icon: Activity,
        tone: 'text-[var(--color-accent)]',
      },
      {
        label: '高风险节点',
        value: noisyCount,
        note: '失败次数高于成功次数',
        icon: CircleOff,
        tone: 'text-red-400',
      },
    ],
    [activeCount, noisyCount, proxies.length, totalSuccess],
  )

  return (
    <div className="page-enter space-y-5">
      <section className="rounded-xl border border-[var(--color-border)] bg-[linear-gradient(180deg,rgba(255,255,255,0.05),rgba(255,255,255,0.018))] p-5 shadow-[var(--shadow-sm)]">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(280px,0.65fr)]">
          <div className="space-y-3">
            <div className="workspace-kicker">系统 / 代理资源</div>
            <div>
              <h1 className="text-[1.7rem] font-semibold tracking-[-0.045em] text-[var(--color-text)]">
                代理资源工作台
              </h1>
              <p className="mt-2 max-w-[64ch] text-sm leading-6 text-[var(--color-text-secondary)]">
                集中管理代理池、批量导入、启停和连通性巡检。
              </p>
            </div>
            <div className="toolbar-strip">
              <Badge variant="default">总量 {proxies.length}</Badge>
              <Badge variant="secondary">启用 {activeCount}</Badge>
              <Badge variant="warning">停用 {disabledCount}</Badge>
              <Badge variant="secondary">成功率 {successRate}</Badge>
            </div>
          </div>

          <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
            <div className="workspace-kicker">运行提示</div>
            <div className="mt-2 text-sm font-medium text-[var(--color-text)]">录入后可直接巡检或批量启停。</div>
            <p className="mt-2 text-sm leading-6 text-[var(--color-text-secondary)]">
              支持单条录入，也支持多行批量导入。
            </p>
            <Button variant="outline" size="sm" onClick={check} disabled={checking} className="mt-4">
              <RefreshCw className={`mr-1.5 h-4 w-4 ${checking ? 'animate-spin' : ''}`} />
              {checking ? '巡检中...' : '巡检全部代理'}
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
              <div className="workspace-metric-icon">
                <Icon className={`h-5 w-5 ${tone}`} />
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
        <Card className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
          <div className="space-y-4">
            <div>
              <div className="workspace-kicker">录入面板</div>
              <div className="mt-2 text-base font-semibold text-[var(--color-text)]">新增代理或批量导入</div>
              <p className="mt-2 text-sm leading-6 text-[var(--color-text-secondary)]">
                地区标签会一起写入，用于后续筛选和平台分流。
              </p>
            </div>

            <textarea
              value={newProxy}
              onChange={(event) => setNewProxy(event.target.value)}
              placeholder="http://user:pass@host:port"
              rows={10}
              className="control-surface control-surface-mono resize-none"
            />

            <div className="space-y-2">
              <label className="workspace-kicker">地区标签</label>
              <input
                value={region}
                onChange={(event) => setRegion(event.target.value)}
                placeholder="例如 US、SG、HK"
                className="control-surface"
              />
            </div>

            <Button onClick={add} className="w-full">
              <Plus className="mr-1.5 h-4 w-4" />
              添加到代理池
            </Button>
          </div>
        </Card>

        <Card className="overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-0">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] px-5 py-4">
            <div>
              <div className="workspace-kicker">数据区</div>
              <div className="mt-1 text-base font-semibold text-[var(--color-text)]">代理清单</div>
              <p className="mt-1 text-xs leading-5 text-[var(--color-text-secondary)]">
                查看 URL、地区、状态和成功率。
              </p>
            </div>
            <div className="toolbar-strip">
              <Badge variant="secondary">启用 {activeCount}</Badge>
              <Badge variant="warning">停用 {disabledCount}</Badge>
              <Badge variant="secondary">成功率 {successRate}</Badge>
            </div>
          </div>

          <div className="glass-table-wrap workspace-table-scroll">
            <table className="workspace-table min-w-[920px] w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--color-border)]">
                  <th className="px-4 py-3 text-left">代理地址</th>
                  <th className="px-4 py-3 text-left">地区</th>
                  <th className="px-4 py-3 text-left">成功 / 失败</th>
                  <th className="px-4 py-3 text-left">状态</th>
                  <th className="px-4 py-3 text-left">操作</th>
                </tr>
              </thead>
              <tbody>
                {proxies.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-4 py-8">
                      <div className="empty-state-panel">
                        当前代理池为空。先在左侧录入一条代理或直接批量导入。
                      </div>
                    </td>
                  </tr>
                )}

                {proxies.map((proxy) => (
                  <tr key={proxy.id} className="border-b border-[var(--color-border)]/40 hover:bg-[var(--color-surface-hover)]/70">
                    <td className="px-4 py-3">
                      <div className="max-w-[360px] break-all font-mono text-xs leading-5 text-[var(--color-text-secondary)]">
                        {proxy.url}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-[var(--color-text-secondary)]">{proxy.region || '-'}</td>
                    <td className="px-4 py-3 text-sm tabular-nums">
                      <span className="text-emerald-400">{proxy.success_count}</span>
                      <span className="text-[var(--color-text-muted)]"> / </span>
                      <span className="text-red-400">{proxy.fail_count}</span>
                    </td>
                    <td className="px-4 py-3">
                      <Badge variant={proxy.is_active ? 'success' : 'danger'}>
                        {proxy.is_active ? '启用中' : '已停用'}
                      </Badge>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <button onClick={() => toggle(proxy.id)} className="table-action-btn">
                          {proxy.is_active ? (
                            <ToggleRight className="mr-1.5 h-4 w-4" />
                          ) : (
                            <ToggleLeft className="mr-1.5 h-4 w-4" />
                          )}
                          {proxy.is_active ? '停用' : '启用'}
                        </button>
                        <button onClick={() => del(proxy.id)} className="table-action-btn table-action-btn-danger">
                          <Trash2 className="mr-1.5 h-4 w-4" />
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  )
}
