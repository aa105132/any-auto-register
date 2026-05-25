import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { apiFetch } from '@/lib/utils'
import { isTerminalTaskStatus } from '@/lib/tasks'

export interface ActiveTask {
  id: string
  platform: string
  status: string
  count?: number
  succeeded?: number
  failed?: number
}

interface ActiveTaskCtx {
  activeTasks: ActiveTask[]
  activeTask: ActiveTask | null
  setActiveTask: (t: ActiveTask) => void
  clearActiveTask: (taskId?: string) => void
  cancelActiveTask: (taskId?: string) => Promise<void>
}

const Ctx = createContext<ActiveTaskCtx>({
  activeTasks: [],
  activeTask: null,
  setActiveTask: () => {},
  clearActiveTask: () => {},
  cancelActiveTask: async () => {},
})

export function useActiveTask() {
  return useContext(Ctx)
}

function mergeTask(prev: ActiveTask[], task: ActiveTask) {
  const taskId = String(task.id || '')
  if (!taskId) return prev
  const existingIndex = prev.findIndex(item => item.id === taskId)
  if (existingIndex < 0) return [...prev, task]
  return prev.map((item, index) => index === existingIndex ? { ...item, ...task } : item)
}

export function ActiveTaskProvider({ children }: { children: ReactNode }) {
  const [activeTasks, setActiveTasks] = useState<ActiveTask[]>([])
  const timersRef = useRef<Record<string, number>>({})
  const fadeTimersRef = useRef<Record<string, number>>({})

  const setActiveTask = useCallback((task: ActiveTask) => {
    const taskId = String(task.id || '')
    if (!taskId) return
    window.clearTimeout(fadeTimersRef.current[taskId])
    setActiveTasks(prev => mergeTask(prev, { ...task, id: taskId }))
  }, [])

  const clearActiveTask = useCallback((taskId?: string) => {
    if (!taskId) {
      Object.values(timersRef.current).forEach(timer => window.clearInterval(timer))
      Object.values(fadeTimersRef.current).forEach(timer => window.clearTimeout(timer))
      timersRef.current = {}
      fadeTimersRef.current = {}
      setActiveTasks([])
      return
    }
    window.clearInterval(timersRef.current[taskId])
    window.clearTimeout(fadeTimersRef.current[taskId])
    delete timersRef.current[taskId]
    delete fadeTimersRef.current[taskId]
    setActiveTasks(prev => prev.filter(item => item.id !== taskId))
  }, [])

  const cancelActiveTask = useCallback(async (taskId?: string) => {
    const targetId = taskId || activeTasks[activeTasks.length - 1]?.id
    if (!targetId) return
    try {
      await apiFetch(`/tasks/${targetId}/cancel`, { method: 'POST' })
      setActiveTasks(prev => prev.map(item => item.id === targetId ? { ...item, status: 'cancel_requested' } : item))
    } catch { /* ignore */ }
  }, [activeTasks])

  useEffect(() => {
    const currentIds = new Set(activeTasks.map(task => task.id))
    Object.keys(timersRef.current).forEach(taskId => {
      if (!currentIds.has(taskId)) {
        window.clearInterval(timersRef.current[taskId])
        delete timersRef.current[taskId]
      }
    })

    activeTasks.forEach(task => {
      if (isTerminalTaskStatus(task.status)) {
        window.clearInterval(timersRef.current[task.id])
        delete timersRef.current[task.id]
        if (!fadeTimersRef.current[task.id]) {
          fadeTimersRef.current[task.id] = window.setTimeout(() => clearActiveTask(task.id), 5000)
        }
        return
      }
      if (timersRef.current[task.id]) return
      timersRef.current[task.id] = window.setInterval(async () => {
        try {
          const latest = await apiFetch(`/tasks/${task.id}`)
          setActiveTasks(prev => prev.map(item => {
            if (item.id !== latest.id) return item
            return {
              ...item,
              status: latest.status || item.status,
              count: latest.count ?? latest.progress_detail?.total ?? item.count,
              succeeded: latest.succeeded ?? latest.success ?? item.succeeded,
              failed: latest.failed ?? latest.error_count ?? item.failed,
            }
          }))
        } catch { /* ignore */ }
      }, 3000)
    })
  }, [activeTasks, clearActiveTask])

  useEffect(() => {
    return () => {
      Object.values(timersRef.current).forEach(timer => window.clearInterval(timer))
      Object.values(fadeTimersRef.current).forEach(timer => window.clearTimeout(timer))
    }
  }, [])

  const activeTask = activeTasks[activeTasks.length - 1] || null

  return (
    <Ctx.Provider value={{ activeTasks, activeTask, setActiveTask, clearActiveTask, cancelActiveTask }}>
      {children}
    </Ctx.Provider>
  )
}
