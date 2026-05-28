import { createPortal } from 'react-dom'
import { Button } from '@/components/ui/button'
import { X } from 'lucide-react'

export function ActionResultModal({ title, payload, onClose }: { title: string; payload: any; onClose: () => void }) {
  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-4">
          <h2 className="text-lg font-semibold text-[var(--color-text)]">{title}</h2>
          <button onClick={onClose} className="btn-pill p-1.5"><X className="h-4 w-4" /></button>
        </div>
        <div className="p-5">
          <pre className="text-xs font-mono text-[var(--color-text-secondary)] whitespace-pre-wrap break-all max-h-[60vh] overflow-auto">
            {typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)}
          </pre>
          <div className="mt-4 flex justify-end">
            <Button size="sm" onClick={onClose}>关闭</Button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
