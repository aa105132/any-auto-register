import { useEffect, useMemo, useState } from 'react'
import { getPlatforms } from '@/lib/app-data'
import { apiFetch } from '@/lib/utils'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { getTaskStatusText, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { Activity, AlertTriangle, CheckCircle2, Clock3, RefreshCw } from 'lucide-react'

export default function TaskHistory() {
  const [tasks, setTasks] = useState<any[]>([])
  const [platform, setPlatform] = useState('')
  const [status, setStatus] = useState('')
  const [platforms, setPlatforms] = useState<any[]>([])
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ page: '1', page_size: '50' })
      if (platform) params.set('platform', platform)
      if (status) params.set('status', status)
      const data = await apiFetch(`/tasks?${params}`)
      setTasks(data.items || [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    getPlatforms().then((data) => setPlatforms(data || [])).catch(() => setPlatforms([]))
  }, [])

  useEffect(() => {
    load()
  }, [platform, status])

  const succeeded = tasks.filter((task) => task.status === 'succeeded').length
  const failed = tasks.filter((task) => task.status === 'failed').length
  const running = tasks.filter((task) => ['running', 'claimed', 'pending', 'cancel_requested'].includes(task.status)).length
  const interrupted = tasks.filter((task) => ['interrupted', 'cancelled'].includes(task.status)).length

  const metricCards = useMemo(
    () => [
      {
        label: '任务总数',
        value: tasks.length,
        note: '当前筛选范围内的任务记录',
        icon: Activity,
        tone: 'text-[var(--color-accent)]',
      },
      {
        label: '成功',
        value: succeeded,
        note: '已完成并写回结果的任务',
        icon: CheckCircle2,
        tone: 'text-emerald-400',
      },
      {
        label: '失败',
        value: failed,
        note: '执行失败或关键步骤异常',
        icon: AlertTriangle,
        tone: 'text-red-400',
      },
      {
        label: '运行 / 中断',
        value: `${running}/${interrupted}`,
        note: '前者是进行中，后者是中断或取消',
        icon: Clock3,
        tone: 'text-amber-400',
      },
    ],
    [failed, interrupted, running, succeeded, tasks.length],
  )

  return (
    <div className="page-enter space-y-5">
      <section className="rounded-xl border border-[var(--color-border)] bg-[linear-gradient(180deg,rgba(255,255,255,0.05),rgba(255,255,255,0.018))] p-5 shadow-[var(--shadow-sm)]">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(280px,0.65fr)]">
          <div className="space-y-3">
            <div className="workspace-kicker">系统 / 任务记录</div>
            <div>
              <h1 className="text-[1.7rem] font-semibold tracking-[-0.045em] text-[var(--color-text)]">
                任务回看工作台
              </h1>
              <p className="mt-2 max-w-[64ch] text-sm leading-6 text-[var(--color-text-secondary)]">
                按平台和状态筛选任务，追踪进度与错误详情。
              </p>
            </div>
            <div className="toolbar-strip">
              <Badge variant="default">任务 {tasks.length}</Badge>
              <Badge variant="secondary">成功 {succeeded}</Badge>
              <Badge variant="danger">失败 {failed}</Badge>
              <Badge variant="warning">运行中 {running}</Badge>
              {platform ? <Badge variant="secondary">{platform}</Badge> : null}
              {status ? <Badge variant="warning">{status}</Badge> : null}
            </div>
          </div>

          <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
            <div className="workspace-kicker">快速操作</div>
            <div className="mt-2 text-sm font-medium text-[var(--color-text)]">加载最近 50 条任务记录。</div>
            <Button variant="outline" size="sm" onClick={load} disabled={loading} className="mt-4">
              <RefreshCw className={`mr-1 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              {loading ? '刷新中...' : '刷新任务列表'}
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

      <Card className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
        <div className="space-y-4">
          <div>
            <div className="workspace-kicker">筛选与范围</div>
            <div className="mt-2 text-base font-semibold text-[var(--color-text)]">按平台和状态回看任务</div>
            <p className="mt-2 text-sm leading-6 text-[var(--color-text-secondary)]">
              缩小范围查看具体任务执行情况。
            </p>
          </div>

          <div className="grid gap-3 lg:grid-cols-[minmax(0,240px)_minmax(0,240px)_1fr]">
            <div className="space-y-2">
              <label className="workspace-kicker">平台</label>
              <select
                value={platform}
                onChange={(event) => setPlatform(event.target.value)}
                className="control-surface appearance-none"
              >
                <option value="">全部平台</option>
                {platforms.map((item: any) => (
                  <option key={item.name} value={item.name}>
                    {item.display_name}
                  </option>
                ))}
              </select>
            </div>

            <div className="space-y-2">
              <label className="workspace-kicker">状态</label>
              <select
                value={status}
                onChange={(event) => setStatus(event.target.value)}
                className="control-surface appearance-none"
              >
                <option value="">全部状态</option>
                <option value="pending">pending</option>
                <option value="claimed">claimed</option>
                <option value="running">running</option>
                <option value="succeeded">succeeded</option>
                <option value="failed">failed</option>
                <option value="interrupted">interrupted</option>
                <option value="cancel_requested">cancel_requested</option>
                <option value="cancelled">cancelled</option>
              </select>
            </div>

            <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3">
              <div className="workspace-kicker">当前结果</div>
              <div className="mt-2 text-sm text-[var(--color-text-secondary)]">
                {platform || status
                  ? `当前正在查看 ${platform || '全部平台'} / ${status || '全部状态'} 的任务记录。`
                  : '当前显示全部平台、全部状态的最近任务。'}
              </div>
              <div className="mt-3 toolbar-strip">
                {!platform && !status ? <Badge variant="secondary">全部任务</Badge> : null}
                {platform ? <Badge variant="secondary">{platform}</Badge> : null}
                {status ? <Badge variant="warning">{status}</Badge> : null}
              </div>
            </div>
          </div>
        </div>
      </Card>

      <Card className="overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-0">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] px-5 py-4">
          <div>
            <div className="workspace-kicker">数据区</div>
            <div className="mt-1 text-base font-semibold text-[var(--color-text)]">最近任务</div>
            <p className="mt-1 text-xs leading-5 text-[var(--color-text-secondary)]">
              横向滚动查看完整表头。
            </p>
          </div>
          <div className="toolbar-strip">
            <Badge variant="secondary">成功 {succeeded}</Badge>
            <Badge variant="danger">失败 {failed}</Badge>
            <Badge variant="warning">运行中 {running}</Badge>
          </div>
        </div>

        <div className="glass-table-wrap workspace-table-scroll">
          <table className="workspace-table min-w-[1160px] w-full text-sm">
            <thead>
              <tr className="border-b border-[var(--color-border)]">
                <th className="px-4 py-3 text-left">时间</th>
                <th className="px-4 py-3 text-left">任务 ID</th>
                <th className="px-4 py-3 text-left">平台</th>
                <th className="px-4 py-3 text-left">状态</th>
                <th className="px-4 py-3 text-left">进度</th>
                <th className="px-4 py-3 text-left">结果</th>
                <th className="px-4 py-3 text-left">错误</th>
              </tr>
            </thead>
            <tbody>
              {tasks.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-8">
                    <div className="empty-state-panel">
                      {loading ? '正在加载任务记录...' : '当前筛选范围内没有任务记录。'}
                    </div>
                  </td>
                </tr>
              )}

              {tasks.map((task) => (
                <tr key={task.id} className="border-b border-[var(--color-border)]/40 hover:bg-[var(--color-surface-hover)]/70">
                  <td className="px-4 py-3 text-xs tabular-nums text-[var(--color-text-muted)]">
                    {task.created_at
                      ? new Date(task.created_at).toLocaleString('zh-CN', { hour12: false })
                      : '-'}
                  </td>
                  <td className="px-4 py-3">
                    <div className="max-w-[220px] break-all font-mono text-xs leading-5 text-[var(--color-text-secondary)]">
                      {task.id}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant="secondary">{task.platform || '-'}</Badge>
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant={TASK_STATUS_VARIANTS[task.status] || 'secondary'}>
                      {getTaskStatusText(task.status)}
                    </Badge>
                  </td>
                  <td className="px-4 py-3 text-[var(--color-text-secondary)]">
                    <span className="rounded-full border border-[var(--color-border)] bg-[var(--color-surface)] px-2.5 py-1 font-mono text-xs tabular-nums">
                      {task.progress || '-'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs leading-5 text-[var(--color-text-secondary)]">
                    <span className="tabular-nums">成功 {task.success || 0}</span>
                    <span className="text-[var(--color-text-muted)]"> / </span>
                    <span className="tabular-nums">失败 {task.error_count || 0}</span>
                  </td>
                  <td className="px-4 py-3 text-xs leading-5">
                    <div
                      title={task.error || ''}
                      className={`max-w-[360px] whitespace-normal break-all ${
                        task.error ? 'text-red-300' : 'text-[var(--color-text-muted)]'
                      }`}
                    >
                      {task.error || '-'}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
