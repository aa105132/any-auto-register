import { useEffect, useRef, useState } from 'react'

import { API_BASE, apiFetch } from '@/lib/utils'
import { getTaskStatusText, isTerminalTaskStatus } from '@/lib/tasks'

export function TaskLogPanel({
  taskId,
  onDone,
}: {
  taskId: string
  onDone: (status: string) => void
}) {
  const [lines, setLines] = useState<string[]>([])
  const [doneStatus, setDoneStatus] = useState<string | null>(null)
  const [successCount, setSuccessCount] = useState(0)
  const [failCount, setFailCount] = useState(0)
  const viewportRef = useRef<HTMLDivElement>(null)
  const followOutputRef = useRef(true)
  const seenEventIdsRef = useRef<Set<number>>(new Set())
  const cursorRef = useRef(0)
  const doneRef = useRef(false)
  const onDoneRef = useRef(onDone)
  const sseHealthyRef = useRef(false)
  const eventSourceRef = useRef<EventSource | null>(null)

  useEffect(() => {
    onDoneRef.current = onDone
  }, [onDone])

  useEffect(() => {
    if (!taskId) return
    seenEventIdsRef.current = new Set()
    cursorRef.current = 0
    doneRef.current = false
    sseHealthyRef.current = false
    followOutputRef.current = true
    setLines([])
    setDoneStatus(null)
    setSuccessCount(0)
    setFailCount(0)

    const pushEvent = (payload: any) => {
      const eventId = Number(payload?.id || 0)
      if (eventId && seenEventIdsRef.current.has(eventId)) return
      if (eventId) {
        seenEventIdsRef.current.add(eventId)
        cursorRef.current = Math.max(cursorRef.current, eventId)
      }
      if (payload?.line) {
        const l = payload.line as string
        if (l.includes('✓') && l.includes('注册成功')) setSuccessCount(c => c + 1)
        if ((l.includes('✗') && l.includes('注册失败')) || (l.includes('✗') && l.includes('失败'))) setFailCount(c => c + 1)
        setLines(prev => [...prev, l])
      }
      if (payload?.done && !doneRef.current) {
        doneRef.current = true
        sseHealthyRef.current = false
        eventSourceRef.current?.close()
        eventSourceRef.current = null
        const nextStatus = payload.status || 'succeeded'
        setDoneStatus(nextStatus)
        onDoneRef.current(nextStatus)
      }
    }

    const es = new EventSource(`${API_BASE}/tasks/${taskId}/logs/stream`)
    eventSourceRef.current = es
    es.onopen = () => {
      sseHealthyRef.current = true
    }
    es.onmessage = (e) => {
      sseHealthyRef.current = true
      pushEvent(JSON.parse(e.data))
    }
    es.onerror = () => {
      if (doneRef.current) {
        es.close()
        if (eventSourceRef.current === es) {
          eventSourceRef.current = null
        }
        return
      }
      sseHealthyRef.current = false
    }

    const poll = window.setInterval(async () => {
      if (doneRef.current || sseHealthyRef.current) return
      try {
        const data = await apiFetch(`/tasks/${taskId}/events?since=${cursorRef.current}`)
        for (const item of data.items || []) {
          pushEvent(item)
        }
        const task = await apiFetch(`/tasks/${taskId}`)
        if (isTerminalTaskStatus(task.status) && !doneRef.current) {
          pushEvent({ done: true, status: task.status })
        }
      } catch {
        // passive
      }
    }, 1000)

    return () => {
      sseHealthyRef.current = false
      eventSourceRef.current?.close()
      eventSourceRef.current = null
      window.clearInterval(poll)
    }
  }, [taskId])

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    if (!followOutputRef.current) return
    viewport.scrollTop = viewport.scrollHeight
  }, [lines])

  const handleViewportScroll = () => {
    const viewport = viewportRef.current
    if (!viewport) return
    const distanceFromBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight
    followOutputRef.current = distanceFromBottom <= 24
  }

  const total = successCount + failCount

  return (
    <div className="flex flex-col h-full gap-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span className="text-sm font-medium text-[var(--color-text)]">执行日志</span>
          <span className="rounded-full border border-[var(--color-border)] bg-[var(--color-surface)] px-2.5 py-0.5 text-[11px] font-medium text-[var(--color-text-secondary)]">
            {doneStatus ? getTaskStatusText(doneStatus) : '进行中'}
          </span>
        </div>
        {total > 0 && (
          <div className="flex items-center gap-1.5 text-[11px] tabular-nums">
            <span className="text-emerald-400 font-medium">{successCount}</span>
            <span className="text-[var(--color-text-muted)]">/</span>
            <span className="text-red-400 font-medium">{failCount}</span>
            <span className="text-[var(--color-text-muted)]">/</span>
            <span className="text-[var(--color-text-secondary)]">{total}</span>
          </div>
        )}
      </div>
      <div
        ref={viewportRef}
        onScroll={handleViewportScroll}
        className="flex-1 overflow-y-auto rounded-xl border border-[var(--color-border)] bg-[var(--color-bg)]/60 px-3 py-2.5 font-mono text-[11px] leading-[1.7] space-y-px min-h-[200px] max-h-[50vh]"
      >
        {lines.length === 0 && <div className="text-[var(--color-text-muted)] py-2">等待日志...</div>}
        {lines.map((line, index) => (
          <div
            key={index}
            className={`rounded-md px-2 py-0.5 ${
              line.includes('✓') || line.includes('成功') ? 'text-emerald-400' :
              line.includes('✗') || line.includes('失败') || line.includes('错误') ? 'text-red-400' :
              'text-[var(--color-text-secondary)]'
            }`}
          >
            {line}
          </div>
        ))}
      </div>
    </div>
  )
}
