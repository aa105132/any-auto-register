import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { apiFetch } from '@/lib/utils'
import { isTerminalTaskStatus, TASK_STATUS_VARIANTS, getTaskStatusText } from '@/lib/tasks'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { X, RefreshCw } from 'lucide-react'

export function ActionTaskModal({
  title, taskId, taskStatus: initialStatus,
  onClose, onDone, onRetryFailed,
}: {
  title: string
  taskId: string
  taskStatus: string | null
  onClose: () => void
  onDone: (status: string) => void
  onRetryFailed: (newTaskId: string, failedCount: number) => void
}) {
  useEffect(() => {
    if (!initialStatus || isTerminalTaskStatus(initialStatus)) return
    const interval = setInterval(async () => {
      try {
        const task = await apiFetch(`/tasks/${taskId}`)
        onDone(task.status)
        if (isTerminalTaskStatus(task.status)) clearInterval(interval)
      } catch {}
    }, 3000)
    return () => clearInterval(interval)
  }, [taskId])

  const status = initialStatus || 'pending'
  const terminal = isTerminalTaskStatus(status)

  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-md" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-4">
          <h2 className="text-lg font-semibold text-[var(--color-text)]">{title}</h2>
          <button onClick={onClose} className="btn-pill p-1.5"><X className="h-4 w-4" /></button>
        </div>
        <div className="p-5 space-y-4">
          <div className="flex items-center gap-3">
            <Badge variant={TASK_STATUS_VARIANTS[status] || 'secondary'}>{getTaskStatusText(status)}</Badge>
            <span className="text-xs text-[var(--color-text-muted)]">Task: {taskId}</span>
          </div>
          {!terminal && <div className="text-sm text-[var(--color-text-secondary)]">任务执行中，请稍候...</div>}
          <TaskLogPanel taskId={taskId} onDone={onDone} />
          <div className="flex justify-end gap-2">
            {terminal && status === 'partial_failure' && (
              <Button size="sm" variant="outline" onClick={async () => {
                const task = await apiFetch(`/tasks/${taskId}`)
                const failedCount = task?.result?.failed_count || 0
                if (failedCount <= 0) return
                try {
                  const res = await apiFetch('/tasks/create-from-task', { method: 'POST', body: JSON.stringify({ task_id: taskId, failed_only: true }) })
                  onRetryFailed(res.task_id, failedCount)
                } catch {}
              }}><RefreshCw className="mr-1 h-3.5 w-3.5" />重跑失败项</Button>
            )}
            <Button size="sm" variant="outline" onClick={onClose}>关闭</Button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
