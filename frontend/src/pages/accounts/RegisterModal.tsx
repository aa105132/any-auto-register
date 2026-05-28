import { createPortal } from 'react-dom'
import { Button } from '@/components/ui/button'
import { X, ExternalLink } from 'lucide-react'

export function RegisterModal({ platform, platformMeta, onClose }: {
  platform: string
  platformMeta: any
  onClose: () => void
  onDone?: () => void
}) {
  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-4">
          <h2 className="text-lg font-semibold text-[var(--color-text)]">注册新账号</h2>
          <button onClick={onClose} className="btn-pill p-1.5"><X className="h-4 w-4" /></button>
        </div>
        <div className="p-5 space-y-4">
          <p className="text-sm text-[var(--color-text-secondary)]">
            前往注册页面完成 {platformMeta?.display_name || platform} 平台的自动注册流程。
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>取消</Button>
            <a href="/register" target="_blank" rel="noopener noreferrer">
              <Button size="sm"><ExternalLink className="mr-1 h-3.5 w-3.5" />打开注册页面</Button>
            </a>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
