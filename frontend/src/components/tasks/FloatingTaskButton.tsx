import { Suspense, lazy, useState } from 'react'
import { createPortal } from 'react-dom'
import { useActiveTask } from '@/context/ActiveTaskContext'
import type { ActiveTask } from '@/context/ActiveTaskContext'
import { getTaskStatusText, isTerminalTaskStatus, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Loader2, ScrollText, Square, X } from 'lucide-react'

const TaskLogPanel = lazy(async () => {
  const mod = await import('@/components/tasks/TaskLogPanel')
  return { default: mod.TaskLogPanel }
})

function TaskPill({
  task,
  onOpenLogs,
}: {
  task: ActiveTask
  onOpenLogs: (task: ActiveTask) => void
}) {
  const { cancelActiveTask, clearActiveTask } = useActiveTask()
  const [cancelling, setCancelling] = useState(false)
  const terminal = isTerminalTaskStatus(task.status)
  const variant = TASK_STATUS_VARIANTS[task.status] || 'secondary'
  const progress = (task.succeeded ?? 0) + (task.failed ?? 0)
  const total = task.count ?? 0

  const handleCancel = async () => {
    setCancelling(true)
    await cancelActiveTask(task.id)
    setCancelling(false)
  }

  return (
    <div className="flex items-center gap-2 rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-2.5 shadow-xl backdrop-blur-sm">
      {!terminal && (
        <Loader2 className="h-4 w-4 animate-spin text-[var(--color-accent)]" />
      )}
      <span className="max-w-[120px] truncate text-sm font-medium text-[var(--color-text)]">
        {task.platform}
      </span>
      {total > 0 && (
        <span className="text-xs tabular-nums text-[var(--color-text-secondary)]">
          {progress}/{total}
        </span>
      )}
      <Badge variant={variant} className="text-[11px] px-1.5 py-0">
        {getTaskStatusText(task.status)}
      </Badge>
      <div className="flex items-center gap-1 ml-1">
        <Button
          size="sm"
          variant="ghost"
          className="h-7 w-7 p-0"
          title="查看日志"
          onClick={() => onOpenLogs(task)}
        >
          <ScrollText className="h-3.5 w-3.5" />
        </Button>
        {!terminal && (
          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0 text-red-400 hover:text-red-300"
            title="停止注册"
            onClick={handleCancel}
            disabled={cancelling || task.status === 'cancel_requested'}
          >
            <Square className="h-3.5 w-3.5" />
          </Button>
        )}
        {terminal && (
          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0"
            title="关闭"
            onClick={() => clearActiveTask(task.id)}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
    </div>
  )
}

export function FloatingTaskButton() {
  const { activeTasks } = useActiveTask()
  const [logTask, setLogTask] = useState<ActiveTask | null>(null)

  if (!activeTasks.length) return null

  const visibleLogTask = logTask && activeTasks.some(task => task.id === logTask.id)
    ? activeTasks.find(task => task.id === logTask.id) || logTask
    : null

  const taskStack = (
    <div
      className="fixed bottom-6 right-6 z-[9999] flex max-w-[min(92vw,520px)] flex-col items-end gap-2"
      style={{ animation: 'floatIn 0.3s ease-out' }}
    >
      {activeTasks.map(task => (
        <TaskPill key={task.id} task={task} onOpenLogs={setLogTask} />
      ))}
    </div>
  )

  const logModal = visibleLogTask ? (
    <div className="dialog-backdrop" style={{ zIndex: 10000 }} onClick={() => setLogTask(null)}>
      <div
        className="dialog-panel"
        style={{ maxWidth: 720, width: '90vw', maxHeight: '80vh' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-4">
          <h2 className="text-lg font-semibold text-[var(--color-text)]">
            注册日志 — {visibleLogTask.platform}
          </h2>
          <button onClick={() => setLogTask(null)} className="btn-pill p-1.5">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="p-5" style={{ height: 'calc(80vh - 72px)', overflow: 'hidden' }}>
          <Suspense fallback={<div className="text-sm text-[var(--color-text-muted)]">加载中...</div>}>
            <TaskLogPanel taskId={visibleLogTask.id} onDone={() => {}} />
          </Suspense>
        </div>
      </div>
    </div>
  ) : null

  return createPortal(
    <>
      {taskStack}
      {logModal}
    </>,
    document.body,
  )
}
