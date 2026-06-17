import { Badge } from '@/components/ui/badge'
import { Eye } from 'lucide-react'
import type { Account } from '@/lib/account-utils'
import {
  getDisplayStatus, getVerificationMailbox, getPrimaryToken, getCashierUrl, getBalance,
} from '@/lib/account-utils'

const STATUS_VARIANT: Record<string, any> = {
  registered: 'default', trial: 'success', subscribed: 'success',
  expired: 'warning', invalid: 'danger',
  free: 'secondary', eligible: 'secondary', valid: 'success', unknown: 'secondary',
}

export function AccountsTable({
  accounts, loading, selectedIds, toggleOne, togglePage, allSelectedOnPage,
  onDetail, tab, search, filterStatus,
}: {
  accounts: Account[]
  loading: boolean
  selectedIds: Set<number>
  toggleOne: (id: number) => void
  togglePage: () => void
  allSelectedOnPage: boolean
  onDetail: (acc: Account) => void
  tab: string
  search: string
  filterStatus: string
}) {
  if (loading) {
    return (
      <div className="px-5 py-10 text-center text-sm text-[var(--color-text-muted)]">
        正在加载账号列表...
      </div>
    )
  }

  if (accounts.length === 0) {
    return (
      <div className="empty-state-panel m-5">
        {search || filterStatus ? '没有匹配当前筛选条件的账号' : `暂无 ${tab} 平台的账号记录`}
      </div>
    )
  }

  return (
    <div className="table-wrap">
      <table className="table-data">
        <thead>
          <tr>
            <th className="w-10">
              <input type="checkbox" checked={allSelectedOnPage} onChange={togglePage} className="checkbox-accent" />
            </th>
            <th>邮箱</th>
            <th>密码</th>
            <th>状态</th>
            <th className="hidden lg:table-cell">余额</th>
            <th className="hidden xl:table-cell">验证邮箱</th>
            <th className="hidden xl:table-cell">Token</th>
            <th className="hidden xl:table-cell">Cashier</th>
            <th className="w-12"></th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((acc) => {
            const mailbox = getVerificationMailbox(acc)
            const token = getPrimaryToken(acc)
            const cashierUrl = getCashierUrl(acc)
            return (
              <tr key={acc.id}>
                <td>
                  <input type="checkbox" checked={selectedIds.has(acc.id)} onChange={() => toggleOne(acc.id)} className="checkbox-accent" />
                </td>
                <td className="font-medium text-[var(--color-text)]">{acc.email || '-'}</td>
                <td className="text-[var(--color-text-secondary)] font-mono text-xs">{acc.password || '-'}</td>
                <td>
                  <Badge variant={STATUS_VARIANT[getDisplayStatus(acc)] || 'secondary'}>
                    {getDisplayStatus(acc)}
                  </Badge>
                </td>
                <td className="hidden lg:table-cell text-xs text-[var(--color-text-secondary)]">
                  {getBalance(acc) ? <span className="font-medium text-[var(--color-text)]">${getBalance(acc)}</span> : '—'}
                </td>
                <td className="hidden xl:table-cell text-xs text-[var(--color-text-secondary)]">
                  {mailbox ? `${mailbox.provider}: ${mailbox.email}` : '-'}
                </td>
                <td className="hidden xl:table-cell text-xs font-mono text-[var(--color-text-secondary)] max-w-[140px] truncate">
                  {token || '-'}
                </td>
                <td className="hidden xl:table-cell">
                  {cashierUrl ? (
                    <a href={cashierUrl} target="_blank" rel="noopener noreferrer" className="text-[var(--color-text)] text-xs hover:underline">打开</a>
                  ) : '-'}
                </td>
                <td>
                  <button onClick={() => onDetail(acc)} className="btn-pill p-1.5" title="查看详情">
                    <Eye className="h-3.5 w-3.5" />
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
