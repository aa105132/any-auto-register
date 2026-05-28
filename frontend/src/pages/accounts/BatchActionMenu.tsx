import { useState, useEffect } from 'react'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { Button } from '@/components/ui/button'
import { apiFetch } from '@/lib/utils'
import { Zap, ChevronDown } from 'lucide-react'

interface PlatformAction {
  id: string
  label: string
}

let cachedActions: Record<string, PlatformAction[]> | null = null

function usePlatformActions(platform: string): PlatformAction[] {
  const [actions, setActions] = useState<PlatformAction[]>(cachedActions?.[platform] || [])

  useEffect(() => {
    if (cachedActions?.[platform]) { setActions(cachedActions[platform]); return }
    apiFetch(`/platforms/${platform}/actions`).then((data) => {
      const list = Array.isArray(data) ? data : []
      cachedActions = { ...(cachedActions || {}), [platform]: list }
      setActions(list)
    }).catch(() => {})
  }, [platform])

  return actions
}

export function BatchActionMenu({
  platform, selectedIds, statusFilter, searchFilter, onTaskCreated,
}: {
  platform: string
  total: number
  selectedIds: number[]
  statusFilter: string
  searchFilter: string
  onTaskCreated: (taskId: string, title: string, actionId: string) => void
}) {
  const actions = usePlatformActions(platform)
  const [loading, setLoading] = useState<string | null>(null)

  const runAction = async (action: PlatformAction) => {
    setLoading(action.id)
    try {
      const body: any = { platform, action_id: action.id }
      if (selectedIds.length > 0) body.account_ids = selectedIds
      if (statusFilter) body.status = statusFilter
      if (searchFilter) body.search = searchFilter
      const res = await apiFetch('/accounts/batch-action', { method: 'POST', body: JSON.stringify(body) })
      onTaskCreated(res.task_id, `批量 ${action.label}`, action.id)
    } finally { setLoading(null) }
  }

  if (actions.length === 0) return null

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button size="sm" variant="outline">
          <Zap className="mr-1 h-3.5 w-3.5" />批量操作 <ChevronDown className="ml-1 h-3 w-3" />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="min-w-[180px] rounded-md border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-1 shadow-lg">
          {actions.map((action) => (
            <DropdownMenu.Item
              key={action.id}
              disabled={loading === action.id}
              onClick={() => runAction(action)}
              className="flex cursor-pointer items-center gap-2 rounded-sm px-3 py-2 text-sm text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-text)] outline-none"
            >
              {loading === action.id ? '执行中...' : action.label}
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}
