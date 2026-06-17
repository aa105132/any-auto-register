import { useEffect, useMemo, useState } from 'react'
import { getPlatforms } from '@/lib/app-data'
import { apiFetch } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { CheckCircle, Clock, RefreshCw, Users, XCircle } from 'lucide-react'

const PLATFORM_COLORS: Record<string, string> = {
  trae: 'text-blue-400',
  tavily: 'text-purple-400',
  cursor: 'text-emerald-400',
}

const STATUS_VARIANT: Record<string, any> = {
  registered: 'default',
  trial: 'success',
  subscribed: 'success',
  expired: 'warning',
  invalid: 'danger',
  oauth_pending: 'warning',
  register_failed: 'danger',
  existing_account: 'warning',
  existing_suspected: 'warning',
  free: 'secondary',
  eligible: 'secondary',
  unknown: 'secondary',
  valid: 'success',
}

const STATUS_LABELS: Record<string, string> = {
  registered: '已注册', trial: '试用', subscribed: '订阅',
  expired: '过期', invalid: '失效', oauth_pending: '待补 OAuth',
  register_failed: '注册失败', existing_account: '已存在账号',
  existing_suspected: '疑似已注册', free: '空闲', eligible: '可用',
  unknown: '未知', valid: '有效', active: '活跃',
  inactive: '未激活', pending: '待处理',
}

export default function Dashboard() {
  const [stats, setStats] = useState<any>(null)
  const [desktopStates, setDesktopStates] = useState<Record<string, any>>({})
  const [loading, setLoading] = useState(false)
  const desktopPlatforms = ['cursor', 'kiro', 'chatgpt']

  const load = async () => {
    setLoading(true)
    try {
      const [data, platforms] = await Promise.all([
        apiFetch('/accounts/stats'),
        getPlatforms().catch(() => []),
      ])
      setStats(data)
      const entries = await Promise.all(
        (platforms || [])
          .filter((item: any) => desktopPlatforms.includes(item.name))
          .map(async (item: any) => {
            const state = await apiFetch(`/platforms/${item.name}/desktop-state`).catch(() => ({ available: false }))
            return [item.name, { ...state, platform: item.name, display_name: item.display_name }] as const
          }),
      )
      setDesktopStates(Object.fromEntries(entries))
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const statCards = useMemo(() => [
    { label: '总账号数', value: stats?.total ?? '-', note: '已收录进账号资产的全部记录', icon: Users, color: 'text-[var(--color-text)]' },
    { label: '试用中', value: stats?.by_plan_state?.trial ?? 0, note: '仍处于试用套餐的账号', icon: Clock, color: 'text-amber-400' },
    { label: '已订阅', value: stats?.by_plan_state?.subscribed ?? 0, note: '已经进入付费或订阅状态', icon: CheckCircle, color: 'text-emerald-400' },
    { label: '已失效', value: (stats?.by_display_status?.expired ?? 0) + (stats?.by_validity_status?.invalid ?? 0), note: '过期与无效账号合计', icon: XCircle, color: 'text-red-400' },
  ], [stats])

  const platformEntries = Object.entries(stats?.by_platform || {})
  const totalCount = Math.max(Number(stats?.total || 0), 0)
  const readyDesktopCount = desktopPlatforms.filter((p) => desktopStates[p]?.ready).length

  const renderStatusGroup = (title: string, values: Record<string, number> | undefined, emptyCopy = '暂无数据') => (
    <div className="space-y-2">
      <div className="text-sm font-semibold text-[var(--color-text)]">{title}</div>
      {values && Object.keys(values).length > 0 ? (
        Object.entries(values).map(([status, count]) => (
          <div key={status} className="flex items-center justify-between rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2.5">
            <Badge variant={STATUS_VARIANT[status] || 'secondary'}>{STATUS_LABELS[status] || status}</Badge>
            <span className="text-sm tabular-nums text-[var(--color-text-secondary)]">{count}</span>
          </div>
        ))
      ) : (
        <div className="empty-state-panel">{emptyCopy}</div>
      )}
    </div>
  )

  return (
    <div className="page-enter space-y-4">
      <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-5">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(280px,0.65fr)]">
          <div className="space-y-3">
            <div className="workspace-kicker">总览 / 账号资产</div>
            <h1 className="text-[var(--text-2xl)] font-semibold tracking-tight text-[var(--color-text)]">资产概览工作台</h1>
            <p className="max-w-[64ch] text-sm text-[var(--color-text-secondary)]">各平台账号分布、桌面就绪状态与生命周期状态一览。</p>
            <div className="toolbar-strip">
              <Badge variant="default">总量 {stats?.total ?? 0}</Badge>
              <Badge variant="secondary">平台 {platformEntries.length}</Badge>
              <Badge variant="success">桌面就绪 {readyDesktopCount}</Badge>
            </div>
          </div>
          <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
            <div className="workspace-kicker">快速操作</div>
            <div className="mt-2 text-sm text-[var(--color-text)]">拉取最新统计与桌面端状态。</div>
            <Button variant="outline" size="sm" onClick={load} disabled={loading} className="mt-4">
              <RefreshCw className={`mr-1 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              {loading ? '刷新中...' : '刷新概览'}
            </Button>
          </div>
        </div>
      </section>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {statCards.map(({ label, value, note, icon: Icon, color }) => (
          <div key={label} className="workspace-metric-panel">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="workspace-kicker">{label}</div>
                <div className="workspace-metric-value tabular-nums">{value}</div>
                <div className="mt-1 text-xs text-[var(--color-text-secondary)]">{note}</div>
              </div>
              <div className="workspace-metric-icon">
                <Icon className={`h-5 w-5 ${color}`} />
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.55fr)]">
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <div>
              <div className="workspace-kicker">平台分布</div>
              <CardTitle className="mt-1">平台资产占比</CardTitle>
            </div>
            <Button variant="outline" size="sm" onClick={load} disabled={loading}>
              <RefreshCw className={`mr-1 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              刷新
            </Button>
          </CardHeader>
          <CardContent className="space-y-3">
            {platformEntries.length > 0 ? (
              platformEntries.map(([platform, count]) => {
                const countValue = Number(count) || 0
                const ratio = totalCount > 0 ? Math.round((countValue / totalCount) * 100) : 0
                return (
                  <div key={platform} className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-3">
                    <div className="flex items-center justify-between gap-3">
                      <span className={`text-sm font-medium ${PLATFORM_COLORS[platform] || 'text-[var(--color-text-secondary)]'}`}>{platform}</span>
                      <span className="text-xs tabular-nums text-[var(--color-text-muted)]">{countValue} / {ratio}%</span>
                    </div>
                    <div className="progress-track mt-2">
                      <div className="progress-fill" style={{ width: `${ratio}%` }} />
                    </div>
                  </div>
                )
              })
            ) : (
              <div className="empty-state-panel">{stats ? '暂无平台分布数据' : '正在加载统计数据...'}</div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="workspace-kicker">桌面环境</div>
            <CardTitle className="mt-1">桌面应用状态</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {desktopPlatforms.map((platform) => {
              const state = desktopStates[platform]
              const label = state?.app_name || state?.display_name || platform
              const badges = state
                ? [
                    { label: state.installed ? '已安装' : '未安装', variant: state.installed ? 'success' : 'secondary' },
                    { label: state.configured ? '已配置' : '未配置', variant: state.configured ? 'success' : 'warning' },
                    { label: state.running ? '已打开' : '未打开', variant: state.running ? 'success' : 'secondary' },
                    { label: state.ready ? '已就绪' : '未就绪', variant: state.ready ? 'success' : 'warning' },
                  ]
                : []
              return (
                <div key={platform} className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-[var(--color-text)]">{label}</div>
                      <div className="mt-1 text-xs text-[var(--color-text-muted)]">
                        {state?.available === false ? state?.message || '当前平台暂未接入桌面状态探测' : state?.ready_label || state?.status_label || '桌面账号切换与本地就绪状态'}
                      </div>
                    </div>
                    <Badge variant={state?.ready ? 'success' : 'secondary'}>{state?.ready ? '就绪' : '待命'}</Badge>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {badges.length > 0 ? (
                      badges.map((b) => <Badge key={`${platform}-${b.label}`} variant={b.variant as any}>{b.label}</Badge>)
                    ) : (
                      <span className="text-xs text-[var(--color-text-muted)]">加载中...</span>
                    )}
                  </div>
                </div>
              )
            })}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <div className="workspace-kicker">状态分布</div>
          <CardTitle className="mt-1">账号状态切面</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 xl:grid-cols-3">
          {renderStatusGroup('套餐', stats?.by_plan_state, '暂无套餐分布数据')}
          {renderStatusGroup('生命周期', stats?.by_lifecycle_status, '暂无生命周期分布数据')}
          {renderStatusGroup('有效性', stats?.by_validity_status, '暂无有效性分布数据')}
        </CardContent>
      </Card>
    </div>
  )
}
