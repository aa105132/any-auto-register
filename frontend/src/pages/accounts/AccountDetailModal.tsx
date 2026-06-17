import { createPortal } from 'react-dom'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { X, Copy, ExternalLink, Coins, RefreshCw } from 'lucide-react'
import type { Account } from '@/lib/account-utils'
import {
  getPlanState, getValidityStatus,
  getPrimaryToken, getCashierUrl, getCredentials, getBalance,
} from '@/lib/account-utils'
import { apiFetch } from '@/lib/utils'
import { isTerminalTaskStatus } from '@/lib/tasks'

const copy = (text: string) => {
  if (navigator.clipboard) navigator.clipboard.writeText(text)
}

export function AccountDetailModal({ acc, platform, onClose }: { acc: Account; platform?: string; onClose: () => void; onSave: () => void }) {
  const token = getPrimaryToken(acc)
  const cashierUrl = getCashierUrl(acc)
  const credentials = getCredentials(acc).filter((item: any) => item?.value)
  const plat = platform || (acc as any).platform || ''
  const [balance, setBalance] = useState<string>(getBalance(acc))
  const [balanceState, setBalanceState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle')
  const [balanceMsg, setBalanceMsg] = useState<string>('')

  const queryBalance = async () => {
    if (!plat) return
    setBalanceState('loading'); setBalanceMsg('')
    try {
      const res = await apiFetch(`/actions/${plat}/${acc.id}/query_balance`, { method: 'POST', body: JSON.stringify({ params: {} }) })
      const taskId = res?.task_id || res?.id
      if (!taskId) { setBalanceState('error'); setBalanceMsg('任务创建失败'); return }
      let task: any = null
      for (let i = 0; i < 80; i++) {
        await new Promise((r) => setTimeout(r, 3000))
        try { task = await apiFetch(`/tasks/${taskId}`) } catch { continue }
        if (task && isTerminalTaskStatus(task.status)) break
      }
      const bal = task?.result?.balance_usd || task?.result?.account_overview?.balance_usd || ''
      if (bal) { setBalance(String(bal)); setBalanceState('done'); setBalanceMsg('已更新') }
      else { setBalanceState('error'); setBalanceMsg(task?.error || '查询超时或失败') }
    } catch (e: any) {
      setBalanceState('error'); setBalanceMsg(String(e?.message || e))
    }
  }

  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-md" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-4">
          <h2 className="text-lg font-semibold text-[var(--color-text)]">账号详情</h2>
          <button onClick={onClose} className="btn-pill p-1.5"><X className="h-4 w-4" /></button>
        </div>
        <div className="p-5 space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
              <div className="text-[11px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]">邮箱</div>
              <div className="mt-1 text-sm font-medium text-[var(--color-text)]">{acc.email || '-'}</div>
            </div>
            <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
              <div className="text-[11px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]">密码</div>
              <div className="mt-1 flex items-center gap-2">
                <span className="text-sm font-mono text-[var(--color-text)]">{acc.password || '-'}</span>
                {acc.password && <button onClick={() => copy(acc.password!)} className="text-[var(--color-accent)] hover:text-[var(--color-accent-hover)]"><Copy className="h-3.5 w-3.5" /></button>}
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="default">余额: {balance ? `$${balance}` : '—'}</Badge>
            <Badge variant="success">套餐: {getPlanState(acc)}</Badge>
            <Badge variant="warning">有效: {getValidityStatus(acc)}</Badge>
            {acc.overview?.oauth_status && <Badge variant="secondary">OAuth: {acc.overview.oauth_status}</Badge>}
            {plat === 'vellum' && (
              <button onClick={queryBalance} disabled={balanceState === 'loading'}
                className="btn-pill inline-flex items-center gap-1 disabled:opacity-60">
                {balanceState === 'loading'
                  ? <><RefreshCw className="h-3 w-3 animate-spin" />查询中…</>
                  : <><Coins className="h-3 w-3" />查询余额</>}
              </button>
            )}
            {balanceMsg && <span className={`text-xs ${balanceState === 'error' ? 'text-[var(--color-danger,#dc2626)]' : 'text-[var(--color-text-muted)]'}`}>{balanceMsg}</span>}
          </div>

          {credentials.length > 0 ? (
            <div className="space-y-3">
              {credentials.map((credential: any) => (
                <div key={`${credential.key}-${credential.id || ''}`} className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
                  <div className="text-[11px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]">
                    {credential.key || 'Credential'}{credential.is_primary ? ' · PRIMARY' : ''}
                  </div>
                  <div className="mt-1 text-xs font-mono text-[var(--color-text-secondary)] break-all">{credential.value}</div>
                  <button onClick={() => copy(String(credential.value || ''))} className="mt-2 btn-pill"><Copy className="mr-1 h-3 w-3" />复制</button>
                </div>
              ))}
            </div>
          ) : token && (
            <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
              <div className="text-[11px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]">Access Token</div>
              <div className="mt-1 text-xs font-mono text-[var(--color-text-secondary)] break-all">{token}</div>
              <button onClick={() => copy(token)} className="mt-2 btn-pill"><Copy className="mr-1 h-3 w-3" />复制</button>
            </div>
          )}

          {cashierUrl && (
            <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
              <div className="text-[11px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]">Cashier URL</div>
              <div className="mt-1 text-xs text-[var(--color-text-secondary)] break-all">{cashierUrl}</div>
              <a href={cashierUrl} target="_blank" rel="noopener noreferrer" className="mt-2 btn-pill inline-flex"><ExternalLink className="mr-1 h-3 w-3" />打开</a>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
