import { useState } from 'react'
import { createPortal } from 'react-dom'
import { Button } from '@/components/ui/button'
import { apiFetch } from '@/lib/utils'
import { Upload, X } from 'lucide-react'

export function ImportModal({ platform, onClose, onDone }: { platform: string; onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)

  const handleImport = async () => {
    if (!text.trim()) return
    setLoading(true)
    try {
      const lines = text.trim().split('\n').filter(Boolean)
      for (const line of lines) {
        const [email, password] = line.split(/[,\t]/).map((s) => s.trim())
        if (email && password) {
          await apiFetch('/accounts', {
            method: 'POST',
            body: JSON.stringify({ platform, email, password }),
          }).catch(() => {})
        }
      }
      onDone()
    } finally { setLoading(false) }
  }

  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-4">
          <h2 className="text-lg font-semibold text-[var(--color-text)]">批量导入账号</h2>
          <button onClick={onClose} className="btn-pill p-1.5"><X className="h-4 w-4" /></button>
        </div>
        <div className="p-5 space-y-4">
          <p className="text-sm text-[var(--color-text-secondary)]">每行格式：邮箱,密码（或 Tab 分隔）</p>
          <textarea
            rows={12}
            value={text}
            onChange={(e) => setText(e.target.value)}
            className="input-field font-mono text-xs"
            placeholder="user@example.com,password123"
          />
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>取消</Button>
            <Button size="sm" onClick={handleImport} disabled={loading || !text.trim()}>
              <Upload className="mr-1 h-3.5 w-3.5" />{loading ? '导入中...' : '导入'}
            </Button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
