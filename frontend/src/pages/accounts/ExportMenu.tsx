import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { Button } from '@/components/ui/button'
import { apiDownload, apiFetch, triggerBrowserDownload } from '@/lib/utils'
import { Download, FileJson, FileText, Search, X } from 'lucide-react'

const FORMATS = [
  { key: 'txt', label: 'TXT 文本', icon: FileText, ext: '.txt' },
  { key: 'csv', label: 'CSV', icon: FileText, ext: '.csv' },
  { key: 'json', label: 'JSON', icon: FileJson, ext: '.json' },
]

type ExportField = {
  key: string
  label: string
}

export function ExportMenu({
  platform, total, statusFilter, searchFilter, selectedIds,
}: {
  platform: string
  total: number
  statusFilter: string
  searchFilter: string
  selectedIds: number[]
}) {
  const [open, setOpen] = useState(false)
  const [format, setFormat] = useState('txt')
  const [exporting, setExporting] = useState(false)
  const [loadingFields, setLoadingFields] = useState(false)
  const [fields, setFields] = useState<ExportField[]>([])
  const [selectedFields, setSelectedFields] = useState<string[]>([])
  const [fieldSearch, setFieldSearch] = useState('')
  const [error, setError] = useState('')

  const loadFields = async () => {
    setLoadingFields(true)
    setError('')
    try {
      const data = await apiFetch(`/accounts/export/fields?platform=${encodeURIComponent(platform)}`)
      const nextFields = Array.isArray(data?.fields) ? data.fields : []
      const defaults = Array.isArray(data?.default_fields) ? data.default_fields : []
      setFields(nextFields)
      setSelectedFields(defaults.length > 0 ? defaults : nextFields.slice(0, 1).map((field: ExportField) => field.key))
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载导出字段失败')
      setFields([])
      setSelectedFields([])
    } finally {
      setLoadingFields(false)
    }
  }

  useEffect(() => {
    if (open) loadFields()
  }, [open, platform])

  const filteredFields = useMemo(() => {
    const q = fieldSearch.trim().toLowerCase()
    if (!q) return fields
    return fields.filter((field) => `${field.key} ${field.label}`.toLowerCase().includes(q))
  }, [fields, fieldSearch])

  const toggleField = (key: string) => {
    setSelectedFields((current) => current.includes(key) ? current.filter((item) => item !== key) : [...current, key])
  }

  const selectOnly = (key: string) => setSelectedFields([key])

  const doExport = async () => {
    if (selectedFields.length === 0) {
      setError('至少选择一个导出字段')
      return
    }
    setExporting(true)
    setError('')
    try {
      const { blob, filename } = await apiDownload(`/accounts/export/${format}`, {
        method: 'POST',
        body: JSON.stringify({
          platform,
          ids: selectedIds,
          select_all: selectedIds.length === 0,
          status_filter: statusFilter || '',
          search_filter: searchFilter || '',
          field_keys: selectedFields,
        }),
      })
      triggerBrowserDownload(blob, filename || `${platform}_accounts${FORMATS.find((item) => item.key === format)?.ext || '.txt'}`)
      setOpen(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : '导出失败')
    } finally {
      setExporting(false)
    }
  }

  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)} disabled={total === 0}>
        <Download className="mr-1 h-3.5 w-3.5" />导出
      </Button>
      {open && createPortal(
        <div className="dialog-backdrop" onClick={() => setOpen(false)}>
          <div className="dialog-panel max-w-3xl" onClick={(event) => event.stopPropagation()}>
            <div className="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-4">
              <div>
                <h2 className="text-lg font-semibold text-[var(--color-text)]">导出账号字段</h2>
                <div className="mt-1 text-xs text-[var(--color-text-muted)]">
                  {selectedIds.length > 0 ? `导出选中的 ${selectedIds.length} 个账号` : `导出当前筛选范围内 ${total} 个账号`}
                </div>
              </div>
              <button onClick={() => setOpen(false)} className="btn-pill p-1.5"><X className="h-4 w-4" /></button>
            </div>

            <div className="grid gap-4 p-5 lg:grid-cols-[minmax(0,1fr)_260px]">
              <div className="space-y-4">
                <div>
                  <div className="workspace-kicker mb-2">导出格式</div>
                  <div className="flex flex-wrap gap-2">
                    {FORMATS.map((item) => (
                      <Button key={item.key} size="sm" variant={format === item.key ? 'default' : 'outline'} onClick={() => setFormat(item.key)}>
                        <item.icon className="mr-1 h-3.5 w-3.5" />{item.label}
                      </Button>
                    ))}
                  </div>
                  <div className="mt-2 text-xs text-[var(--color-text-muted)]">TXT 会一行一个账号；多个字段之间使用 <span className="font-mono">----</span> 分隔。</div>
                </div>

                <div>
                  <div className="mb-2 flex items-center justify-between gap-3">
                    <div className="workspace-kicker">选择字段</div>
                    <div className="relative w-56">
                      <Search className="pointer-events-none absolute left-2 top-2.5 h-3.5 w-3.5 text-[var(--color-text-muted)]" />
                      <input value={fieldSearch} onChange={(event) => setFieldSearch(event.target.value)} placeholder="搜索字段" className="control-surface control-surface-compact pl-7" />
                    </div>
                  </div>
                  {loadingFields ? (
                    <div className="empty-state-panel">正在加载字段...</div>
                  ) : (
                    <div className="max-h-[360px] overflow-y-auto rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-2">
                      {filteredFields.map((field) => {
                        const checked = selectedFields.includes(field.key)
                        return (
                          <label key={field.key} className={`flex items-center justify-between gap-3 rounded-md px-2.5 py-2 text-sm transition-colors ${checked ? 'bg-[var(--color-accent-soft)] text-[var(--color-text)]' : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-hover)]'}`}>
                            <span className="min-w-0">
                              <span className="font-medium">{field.label}</span>
                              <span className="ml-2 font-mono text-[11px] text-[var(--color-text-muted)]">{field.key}</span>
                            </span>
                            <span className="flex items-center gap-2">
                              <button type="button" onClick={(event) => { event.preventDefault(); selectOnly(field.key) }} className="text-[11px] text-[var(--color-text)] hover:underline">只选</button>
                              <input type="checkbox" checked={checked} onChange={() => toggleField(field.key)} className="checkbox-accent" />
                            </span>
                          </label>
                        )
                      })}
                      {filteredFields.length === 0 && <div className="empty-state-panel">没有匹配字段</div>}
                    </div>
                  )}
                </div>
              </div>

              <aside className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
                <div className="workspace-kicker">导出预览</div>
                <div className="mt-3 space-y-2 text-sm text-[var(--color-text-secondary)]">
                  <div>格式：<span className="text-[var(--color-text)]">{format.toUpperCase()}</span></div>
                  <div>字段数：<span className="text-[var(--color-text)]">{selectedFields.length}</span></div>
                  <div>字段顺序：</div>
                  <div className="max-h-36 overflow-y-auto rounded border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-2 font-mono text-xs text-[var(--color-text)]">
                    {selectedFields.length > 0 ? selectedFields.join(' ---- ') : '未选择'}
                  </div>
                  <div className="text-xs text-[var(--color-text-muted)]">例如只选 <span className="font-mono">api_key</span>，导出后就是一行一个 API Key。</div>
                </div>
                {error ? <div className="mt-3 rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-300">{error}</div> : null}
              </aside>
            </div>

            <div className="flex justify-end gap-2 border-t border-[var(--color-border)] px-5 py-4">
              <Button variant="outline" onClick={() => setOpen(false)}>取消</Button>
              <Button onClick={doExport} disabled={exporting || selectedFields.length === 0}>{exporting ? '导出中...' : '导出'}</Button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  )
}
