import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { getConfig, getConfigOptions, getPlatforms } from '@/lib/app-data'

import type { ConfigOptionsResponse, ProviderOption, ProviderSetting } from '@/lib/config-options'

import { getCaptchaStrategyLabel, getProviderSelectOptions, listProviderFieldKeys } from '@/lib/config-options'

import { apiFetch, cn } from '@/lib/utils'

import { useActiveTask } from '@/context/ActiveTaskContext'

import { buildExecutorOptions, buildRegistrationOptions, hasReusableOAuthBrowser, pickOAuthExecutor } from '@/lib/registration'

import { resolveResinProxyPreview } from '@/lib/resin'

import { Button } from '@/components/ui/button'

import { Badge } from '@/components/ui/badge'

import { Play, CheckCircle, XCircle, Loader2, Orbit, Mail, ScanText, ShieldCheck, Workflow, Smartphone } from 'lucide-react'

import { getTaskStatusText, isTerminalTaskStatus, TASK_STATUS_VARIANTS } from '@/lib/tasks'

const TaskLogPanel = lazy(async () => {
  const mod = await import('@/components/tasks/TaskLogPanel')
  return { default: mod.TaskLogPanel }
})

const TWOAPI_PUSH_PLATFORMS = new Set(['thesys'])

const FALLBACK_PLATFORMS = [
  { name: 'chatgpt', display_name: 'ChatGPT' },
  { name: 'cursor', display_name: 'Cursor' },
  { name: 'grok', display_name: 'Grok' },
  { name: 'kiro', display_name: 'Kiro (AWS Builder ID)' },
  { name: 'openblocklabs', display_name: 'OpenBlockLabs' },
  { name: 'tavily', display_name: 'Tavily' },
  { name: 'trae', display_name: 'Trae.ai' },
  { name: 'atxp', display_name: 'ATXP' },
  { name: 'venice', display_name: 'Venice' },
]

const DEFAULT_FORM: Record<string, any> = {
  platform: 'trae',
  email: '',
  password: '',
  count: 1,
  concurrency: 1,
  proxy: '',
  executor_type: 'protocol',
  captcha_solver: 'auto',
  identity_provider: 'mailbox',
  oauth_provider: '',
  oauth_email_hint: '',
  chatgpt_sso_prefix: '',
  chatgpt_sso_domain: 'edu.pilipala.store',
  chatgpt_sso_password: 'ciallo',
  google_account_source: 'purchase',
  hstockplus_reuse_email: '',
  chrome_user_data_dir: '',
  chrome_cdp_url: '',
  seed_lines: '',
  sub_mail_mode: 'none',
  sub_mail_length: 4,
  outlook_alias_enabled: false,
  outlook_alias_max_count: 0,
  mail_provider: 'moemail',
  extra_mail_providers: '' as string,
  phone_provider_enabled: false,
  phone_provider: 'haozhu',
  phone_otp_timeout: 180,
  phone_project_id: '',
  grok_registration_mode: 'browser',
  swarms_registration_mode: 'browser',
  solver_url: 'http://localhost:8889',
  venice_expected_credits: 500,
  venice_api_key_description: 'seedance-auto',
  twoapi_push_mode: 'none',
  twoapi_push_target_url: '',
}

function getProviderSetting(settings: ProviderSetting[] = [], providerKey: string) {
  return settings.find(item => item.provider_key === providerKey) || null
}

function getProviderMergedValues(setting: ProviderSetting | null) {
  return {
    ...(setting?.config || {}),
    ...(setting?.auth || {}),
  }
}

function isTruthyProviderValue(value: unknown) {
  const raw = String(value || '').trim().toLowerCase()
  return raw === '1' || raw === 'true' || raw === 'yes' || raw === 'on' || raw === 'enabled'
}

function getProviderFieldErrorMessage(error: unknown, fallback: string) {
  if (error instanceof Error && error.message) return error.message
  return fallback
}

function HStockPlusProductSelect({ value, onChange, disabled = false }: any) {
  const [products, setProducts] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const loadProducts = async () => {
    setLoading(true)
    setError('')
    try {
      const data = await apiFetch('/provider-settings/hstockplus-google/products?lang=zh')
      setProducts(Array.isArray(data?.products) ? data.products : [])
    } catch (err) {
      setError(getProviderFieldErrorMessage(err, '加载 HStockPlus 商品失败'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (!disabled) loadProducts() }, [disabled])

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <select value={value || ''} onChange={e => onChange(e.target.value)} disabled={disabled || loading} className="control-surface control-surface-compact appearance-none disabled:opacity-70">
          <option value="">选择 Google/Gmail 商品</option>
          {products.map((item: any) => {
            const service = String(item.service || item._id || '')
            const label = `${service} · ${item.name || item.category || 'Google 商品'} · $${item.rate || '-'} · 库存 ${item.stock ?? '-'}`
            return <option key={service} value={service}>{label}</option>
          })}
        </select>
        <Button type="button" variant="outline" size="sm" onClick={loadProducts} disabled={disabled || loading} className="whitespace-nowrap">
          {loading ? '加载中...' : '刷新'}
        </Button>
      </div>
      <input value={value || ''} onChange={e => onChange(e.target.value)} disabled={disabled} placeholder="也可以手动填写 service id" className="control-surface control-surface-compact disabled:opacity-70" />
      {error ? <div className="text-xs text-red-300">{error}</div> : null}
    </div>
  )
}

type RegisterFormSetter = (key: string, value: any) => void

function FieldLabel({ label, helper }: { label: string; helper?: string }) {
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <span className="workspace-kicker">{label}</span>
      {helper ? <span className="text-[11px] text-[var(--color-text-muted)]">{helper}</span> : null}
    </div>
  )
}

function RegisterTextInput({
  form,
  onSet,
  label,
  k,
  type = 'text',
  placeholder = '',
  helper = '',
  disabled = false,
}: {
  form: Record<string, any>
  onSet: RegisterFormSetter
  label: string
  k: string
  type?: string
  placeholder?: string
  helper?: string
  disabled?: boolean
}) {
  return (
    <label className="block">
      <FieldLabel label={label} helper={helper} />
      <input
        type={type}
        value={form[k] ?? ''}
        onChange={(e) => onSet(k, type === 'number' ? (e.target.value === '' ? '' : Number(e.target.value)) : e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className="control-surface control-surface-compact disabled:opacity-70"
      />
    </label>
  )
}

function RegisterSelect({
  form,
  onSet,
  label,
  k,
  options,
  helper = '',
}: {
  form: Record<string, any>
  onSet: RegisterFormSetter
  label: string
  k: string
  options: Array<any>
  helper?: string
}) {
  return (
    <label className="block">
      <FieldLabel label={label} helper={helper} />
      <select
        value={form[k] ?? ''}
        onChange={(e) => onSet(k, e.target.value)}
        className="control-surface control-surface-compact appearance-none"
      >
        {options.map(([value, optionLabel]: any) => (
          <option key={value} value={value}>
            {optionLabel}
          </option>
        ))}
      </select>
    </label>
  )
}

// 扁平分区：替代旧 Card / CardHeader / CardContent 三层嵌套，单层圆角面板 + 标题 + 正文。
function FormSection({
  kicker,
  title,
  action,
  className = '',
  bodyClassName = 'space-y-4',
  children,
}: {
  kicker?: string
  title?: ReactNode
  action?: ReactNode
  className?: string
  bodyClassName?: string
  children: ReactNode
}) {
  const hasHeader = Boolean(kicker || title || action)
  return (
    <section className={cn('rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-5 shadow-sm', className)}>
      {hasHeader ? (
        <div className="mb-4 flex items-start justify-between gap-3">
          <div className="flex flex-col gap-1">
            {kicker ? <div className="workspace-kicker">{kicker}</div> : null}
            {title ? <h3 className="text-base font-semibold text-[var(--color-text)]">{title}</h3> : null}
          </div>
          {action ? <div className="shrink-0">{action}</div> : null}
        </div>
      ) : null}
      <div className={bodyClassName}>{children}</div>
    </section>
  )
}

export default function Register() {
  const [form, setForm] = useState<Record<string, any>>(DEFAULT_FORM)
  const [globalConfig, setGlobalConfig] = useState<Record<string, any>>({})
  const [platforms, setPlatforms] = useState<any[]>([])
  const [configOptions, setConfigOptions] = useState<ConfigOptionsResponse>({
    mailbox_providers: [],
    captcha_providers: [],
    phone_providers: [],
    mailbox_settings: [],
    captcha_settings: [],
    phone_settings: [],
    captcha_policy: {},
  })
  const [optionsError, setOptionsError] = useState('')
  const [task, setTask] = useState<any>(null)
  const [polling, setPolling] = useState(false)
  const handledTerminalTaskIdsRef = useRef<Set<string>>(new Set())
  const openedCashierTaskIdsRef = useRef<Set<string>>(new Set())
  const previousPlatformRef = useRef<string>('')

  const { setActiveTask } = useActiveTask()

  const set = (k: string, v: any) => setForm(f => ({ ...f, [k]: v }))

  const applyTerminalTask = useCallback((latest: any, statusHint?: string) => {
    setTask(latest)
    const taskKey = String(latest?.task_id || latest?.id || task?.task_id || '')
    if (!taskKey) return
    handledTerminalTaskIdsRef.current.add(taskKey)
    const resolvedStatus = statusHint || latest?.status || ''
    setActiveTask({
      id: taskKey,
      platform: latest?.platform || form.platform,
      status: resolvedStatus,
      count: latest?.count ?? latest?.progress_detail?.total,
      succeeded: latest?.succeeded ?? latest?.success,
      failed: latest?.failed ?? latest?.error_count,
    })
    if (
      resolvedStatus === 'succeeded'
      && latest?.cashier_urls
      && latest.cashier_urls.length > 0
      && !openedCashierTaskIdsRef.current.has(taskKey)
    ) {
      openedCashierTaskIdsRef.current.add(taskKey)
      latest.cashier_urls.forEach((url: string) => window.open(url, '_blank'))
    }
  }, [task?.task_id, form.platform, setActiveTask])

  useEffect(() => {
    Promise.all([
      getConfig().catch(() => ({})),
      getPlatforms().catch(() => []),
      getConfigOptions().catch(() => null),
    ]).then(([cfg, ps, options]) => {
      setGlobalConfig(cfg || {})
      setPlatforms(ps || [])
      if (options) {
        setConfigOptions(options)
        setOptionsError('')
      } else {
        setConfigOptions({ mailbox_providers: [], captcha_providers: [], phone_providers: [], mailbox_settings: [], captcha_settings: [], phone_settings: [], mailbox_drivers: [], captcha_drivers: [], phone_drivers: [], captcha_policy: {} })
        setOptionsError('加载 provider 配置失败，请检查后端接口或刷新重试')
      }
      setForm(f => {
        const nextForm: Record<string, any> = {
          ...f,
          executor_type: cfg.default_executor || f.executor_type,
          captcha_solver: 'auto',
          phone_provider: cfg.phone_provider || f.phone_provider,
          qianchuan_operator: f.qianchuan_operator || '0',
          identity_provider: cfg.default_identity_provider || f.identity_provider,
          oauth_provider: cfg.default_oauth_provider || f.oauth_provider,
          oauth_email_hint: cfg.oauth_email_hint || f.oauth_email_hint,
          chrome_user_data_dir: cfg.chrome_user_data_dir || f.chrome_user_data_dir,
          chrome_cdp_url: cfg.chrome_cdp_url || f.chrome_cdp_url,
          mail_provider: cfg.mail_provider || f.mail_provider,
          solver_url: cfg.solver_url || f.solver_url,
        }
        const providerFieldKeys = listProviderFieldKeys([
          ...((options?.mailbox_providers as ProviderOption[]) || []),
          ...((options?.captcha_providers as ProviderOption[]) || []),
          ...((options?.phone_providers as ProviderOption[]) || []),
        ], ['auth', 'connection', 'config', 'identity'])
        providerFieldKeys.forEach(fieldKey => {
          nextForm[fieldKey] = cfg[fieldKey] || f[fieldKey] || ''
        })
        return nextForm
      })
    })
  }, [])

  const currentPlatform = platforms.find((p: any) => p.name === form.platform) || null
  const isTwoApiPushPlatform = TWOAPI_PUSH_PLATFORMS.has(String(form.platform || ''))
  const twoApiPushPlatformLabel = currentPlatform?.display_name || form.platform
  const platformOptionsSource = platforms.length > 0 ? platforms : FALLBACK_PLATFORMS
  const platformOptions = platformOptionsSource.map((p: any) => [p.name, p.display_name])
  const supportedExecutors = currentPlatform?.supported_executors || ['protocol']
  const registrationOptions = buildRegistrationOptions(currentPlatform)
  const executorOptions = buildExecutorOptions(form.identity_provider, supportedExecutors, hasReusableOAuthBrowser(form))
  const mailboxProviderOptions = getProviderSelectOptions(configOptions.mailbox_providers || [])
  const phoneProviderOptions = getProviderSelectOptions(configOptions.phone_providers || [])
  const currentMailboxProvider = (configOptions.mailbox_providers || []).find(provider => provider.value === form.mail_provider) || null
  const hstockplusProvider = (configOptions.mailbox_providers || []).find(provider => provider.value === 'hstockplus_google') || null
  const currentGoogleAccountMode = form.google_account_source || (form.mail_provider === 'hstockplus_google' ? 'purchase' : 'chrome')
  const currentMailboxSetting = getProviderSetting(configOptions.mailbox_settings || [], form.mail_provider)
  const currentPhoneProvider = (configOptions.phone_providers || []).find(provider => provider.value === form.phone_provider) || null
  const currentPhoneSetting = getProviderSetting(configOptions.phone_settings || [], form.phone_provider)
  const seedLines = String(form.seed_lines || '').split(/\r?\n/).map((item: string) => item.trim()).filter(Boolean)
  const parsedLuckMailLines = seedLines.filter((item: string) => item.includes('----')).length
  const effectiveCount = seedLines.length > 0 ? seedLines.length : Number(form.count || 1)
  const allProviderFieldKeys = listProviderFieldKeys([
    ...(configOptions.mailbox_providers || []),
    ...(configOptions.captcha_providers || []),
    ...(configOptions.phone_providers || []),
  ])
  const phoneSettingFields = (currentPhoneProvider?.fields || []).filter((field: any) => field.category !== 'task')
  const phoneTaskFields = (currentPhoneProvider?.fields || []).filter((field: any) => field.category === 'task')
  const resinPreview = useMemo(() => resolveResinProxyPreview({
    config: globalConfig,
    taskPlatform: String(form.platform || ''),
    taskProxy: String(form.proxy || ''),
  }), [globalConfig, form.platform, form.proxy])

  useEffect(() => {
    if (!currentMailboxProvider) return
    const values = getProviderMergedValues(currentMailboxSetting)
    const fields = currentMailboxProvider.fields || []
    if (fields.length === 0) return
    setForm(current => {
      const next = { ...current }
      let changed = false
      fields.forEach(field => {
        if (field.category === 'task') return
        const nextValue = values[field.key] ?? current[field.key] ?? ''
        if ((next[field.key] ?? '') !== nextValue) {
          next[field.key] = nextValue
          changed = true
        }
      })
      return changed ? next : current
    })
  }, [form.mail_provider, currentMailboxProvider, currentMailboxSetting])

  useEffect(() => {
    if (!currentPhoneProvider) return
    const values = getProviderMergedValues(currentPhoneSetting)
    const fields = currentPhoneProvider.fields || []
    if (fields.length === 0) return
    setForm(current => {
      const next = { ...current }
      let changed = false
      fields.forEach(field => {
        const nextValue = values[field.key] ?? current[field.key] ?? ''
        if ((next[field.key] ?? '') !== nextValue) {
          next[field.key] = nextValue
          changed = true
        }
      })
      return changed ? next : current
    })
  }, [form.phone_provider, currentPhoneProvider, currentPhoneSetting])

  useEffect(() => {
    if (form.identity_provider !== 'oauth_browser' || form.oauth_provider !== 'google') return
    if (form.mail_provider === 'hstockplus_google') return
    if (!hstockplusProvider) return
    const values = getProviderMergedValues(getProviderSetting(configOptions.mailbox_settings || [], 'hstockplus_google'))
    const hasConfiguredHStockPlus = Boolean(String(values.hstockplus_api_key || '').trim())
    if (!hasConfiguredHStockPlus) return
    setForm(current => current.mail_provider === 'hstockplus_google' ? current : { ...current, mail_provider: 'hstockplus_google' })
  }, [form.identity_provider, form.oauth_provider, form.mail_provider, hstockplusProvider, configOptions.mailbox_settings])

  useEffect(() => {
    if (!platformOptionsSource.some((p: any) => p.name === form.platform)) {
      const fallback = platformOptionsSource[0]?.name || 'trae'
      if (fallback !== form.platform) {
        set('platform', fallback)
      }
    }
  }, [form.platform, platforms.length])

  useEffect(() => {
    if (registrationOptions.length === 0) return
    const platformName = String(form.platform || '')
    const platformChanged = previousPlatformRef.current !== platformName
    previousPlatformRef.current = platformName
    const platformDefaultMailProvider = String(currentPlatform?.default_mail_provider || '').trim()
    if (platformChanged && platformName === 'swarms') {
      setForm(current => ({
        ...current,
        swarms_registration_mode: 'browser',
        executor_type: 'headed',
      }))
    }
    const currentMailProvider = String(form.mail_provider || '').trim()
    const availableMailProviders = new Set((configOptions.mailbox_providers || []).map(provider => String(provider.value || '').trim()).filter(Boolean))
    const mailProviderInvalid = Boolean(currentMailProvider && availableMailProviders.size > 0 && !availableMailProviders.has(currentMailProvider))
    // 平台默认邮箱只作为软默认：切换平台、当前为空或当前值无效时才兜底，避免覆盖用户手动选择。
    if (
      platformDefaultMailProvider
      && form.identity_provider === 'mailbox'
      && form.mail_provider !== platformDefaultMailProvider
      && (platformChanged || !currentMailProvider || mailProviderInvalid)
    ) {
      set('mail_provider', platformDefaultMailProvider)
    }
    const currentRegistration = registrationOptions.find(option =>
      option.identityProvider === form.identity_provider &&
      option.oauthProvider === form.oauth_provider,
    )
    if (!currentRegistration) {
      const preferred = registrationOptions.find(option =>
        option.identityProvider === form.identity_provider,
      ) || registrationOptions[0]
      set('identity_provider', preferred.identityProvider)
      set('oauth_provider', preferred.oauthProvider)
    }
  }, [registrationOptions, currentPlatform, form.identity_provider, form.oauth_provider, form.platform, form.mail_provider, configOptions.mailbox_providers])

  useEffect(() => {
    const validExecutors = executorOptions.filter(option => !option.disabled)
    if (validExecutors.length === 0) return
    if (!validExecutors.some(option => option.value === form.executor_type)) {
      const nextExecutor = form.identity_provider === 'oauth_browser'
        ? pickOAuthExecutor(supportedExecutors, form.executor_type, hasReusableOAuthBrowser(form))
        : ((supportedExecutors.includes(form.executor_type) && form.executor_type) ? form.executor_type : supportedExecutors[0] || 'protocol')
      set('executor_type', validExecutors.find(option => option.value === nextExecutor)?.value || validExecutors[0].value)
    }
  }, [executorOptions, supportedExecutors, form.executor_type, form.identity_provider, form.chrome_user_data_dir, form.chrome_cdp_url])

  const submit = async () => {
    const isTwoApiPushPlatform = TWOAPI_PUSH_PLATFORMS.has(String(form.platform || ''))
    if (isTwoApiPushPlatform && form.twoapi_push_mode === 'remote' && !String(form.twoapi_push_target_url || '').trim()) {
      setOptionsError('请先填写远端 2API 后端地址')
      return
    }
    const extraProviders = String(form.extra_mail_providers || '').split(',').map(s => s.trim()).filter(Boolean)
    const allMailProviders = [form.mail_provider, ...extraProviders].filter(Boolean)
    const combinedMailProvider = [...new Set(allMailProviders)].join(',')
    const extra: Record<string, any> = {
      mail_provider: combinedMailProvider,
      identity_provider: form.identity_provider,
      oauth_provider: form.oauth_provider,
      oauth_email_hint: form.oauth_email_hint,
      chatgpt_sso_prefix: form.oauth_provider === 'pilipala_sso' ? String(form.chatgpt_sso_prefix || '').trim() : undefined,
      chatgpt_sso_domain: form.oauth_provider === 'pilipala_sso' ? String(form.chatgpt_sso_domain || 'edu.pilipala.store').trim() : undefined,
      chatgpt_sso_password: form.oauth_provider === 'pilipala_sso' ? String(form.chatgpt_sso_password || 'ciallo').trim() : undefined,
      chrome_user_data_dir: form.chrome_user_data_dir || undefined,
      chrome_cdp_url: form.chrome_cdp_url || undefined,
      sub_mail_mode: form.sub_mail_mode || 'none',
      sub_mail_length: Number(form.sub_mail_length || 4),
      outlook_alias_enabled: Boolean(form.outlook_alias_enabled),
      outlook_alias_max_count: Number(form.outlook_alias_max_count || 0),
      phone_provider_enabled: form.identity_provider === 'phone' ? true : Boolean(form.phone_provider_enabled),
      phone_provider: form.phone_provider || 'haozhu',
      phone_otp_timeout: Number(form.phone_otp_timeout || 180),
      phone_project_id: String(form.phone_project_id || '').trim(),
      grok_registration_mode: form.platform === 'grok' ? (form.grok_registration_mode || 'browser') : undefined,
      swarms_registration_mode: form.platform === 'swarms' ? (form.swarms_registration_mode || 'browser') : undefined,
      twoapi_push_mode: isTwoApiPushPlatform ? String(form.twoapi_push_mode || 'none') : 'none',
      twoapi_push_target_url: isTwoApiPushPlatform ? String(form.twoapi_push_target_url || '').trim() : '',
      twoapi_push_latest_only: false,
    }
    if (form.identity_provider === 'oauth_browser' && form.oauth_provider === 'google' && currentGoogleAccountMode !== 'chrome') {
      extra.mail_provider = 'hstockplus_google'
      extra.oauth_account_source = 'mailbox'
      extra.oauth_email_hint = ''
      extra.chrome_user_data_dir = undefined
      extra.chrome_cdp_url = undefined
      extra.hstockplus_reuse_mode = currentGoogleAccountMode === 'pool'
      extra.hstockplus_reuse_email = currentGoogleAccountMode === 'pool' ? String(form.hstockplus_reuse_email || '').trim() : ''
    }
    allProviderFieldKeys.forEach(fieldKey => {
      if (form[fieldKey] !== undefined) {
        extra[fieldKey] = form[fieldKey]
      }
    })
    const phoneProjectId = String(form.phone_project_id || '').trim()
    if (phoneProjectId) {
      extra.haozhu_project_id = String(extra.haozhu_project_id || phoneProjectId).trim()
      extra.qianchuan_channel_id = String(extra.qianchuan_channel_id || phoneProjectId).trim()
      extra['5sim_product'] = String(extra['5sim_product'] || phoneProjectId).trim()
    }
    if (form.platform === 'venice') {
      extra.venice_expected_credits = Number(form.venice_expected_credits || 500)
      extra.venice_api_key_description = String(form.venice_api_key_description || 'seedance-auto')
    }
    if (form.platform === 'atxp') {
      extra.enable_clowdbot = Boolean(form.enable_clowdbot)
    }
    const res = await apiFetch('/tasks/register', {
      method: 'POST',
      body: JSON.stringify({
        platform: form.platform,
        email: form.email || null,
        password: form.password || null,
        lines: seedLines,
        count: effectiveCount,
        concurrency: Number(form.concurrency || 1),
        proxy: form.proxy || null,
        executor_type: form.platform === 'swarms' && form.swarms_registration_mode === 'browser' ? 'headed' : form.executor_type,
        captcha_solver: form.executor_type === 'cdp_protocol' ? 'cdp_turnstile' : 'auto',
        extra,
      }),
    })
    setTask(res)
    setPolling(true)
    setActiveTask({
      id: res.task_id,
      platform: res.platform || form.platform,
      status: res.status || 'running',
      count: res.count ?? res.progress_detail?.total ?? effectiveCount,
      succeeded: res.succeeded ?? res.success,
      failed: res.failed ?? res.error_count,
    })
  }

  const handleTaskDone = useCallback(async (status: string) => {
    if (!task?.task_id) return
    if (handledTerminalTaskIdsRef.current.has(String(task.task_id))) {
      setPolling(false)
      return
    }
    try {
      const latest = await apiFetch(`/tasks/${task.task_id}`)
      applyTerminalTask(latest, status)
    } finally {
      setPolling(false)
    }
  }, [applyTerminalTask, task?.task_id])

  useEffect(() => {
    if (!task?.task_id || isTerminalTaskStatus(task.status)) {
      if (task?.status) {
        setPolling(false)
      }
      return
    }
    const interval = window.setInterval(async () => {
      if (document.visibilityState !== 'visible') return
      try {
        const latest = await apiFetch(`/tasks/${task.task_id}`)
        setTask(latest)
        if (isTerminalTaskStatus(latest.status)) {
          window.clearInterval(interval)
          setPolling(false)
          applyTerminalTask(latest)
        }
      } catch {
        // passive
      }
    }, 5000)
    return () => window.clearInterval(interval)
  }, [applyTerminalTask, task?.task_id, task?.status])

  const renderProviderField = (field: any, disabled = false) => {
    if (field.type === 'checkbox') {
      return (
        <label key={field.key} className="block rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
          <FieldLabel label={field.label} />
          <span className="inline-flex items-center gap-2 text-sm text-[var(--color-text-secondary)]">
            <input
              type="checkbox"
              checked={isTruthyProviderValue(form[field.key])}
              disabled={disabled}
              onChange={e => set(field.key, e.target.checked ? 'true' : 'false')}
              className="checkbox-accent"
            />
            {isTruthyProviderValue(form[field.key]) ? '已启用' : '未启用'}
          </span>
        </label>
      )
    }
    if (field.type === 'hstockplus_product_select') {
      return (
        <label key={field.key} className="block md:col-span-2">
          <FieldLabel label={field.label} helper={field.placeholder || ''} />
          <HStockPlusProductSelect value={form[field.key]} onChange={(value: string) => set(field.key, value)} />
        </label>
      )
    }
    return (
      <RegisterTextInput form={form} onSet={set}
        key={field.key}
        label={field.label}
        k={field.key}
        type={field.secret ? 'password' : 'text'}
        placeholder={field.placeholder || ''}
        disabled={disabled}
      />
    )
  }

  const summaryRegistration = registrationOptions.find((option) =>
    option.identityProvider === form.identity_provider
    && option.oauthProvider === form.oauth_provider,
  )?.label || '-'
  const summaryExecutor = executorOptions.find((option) => option.value === form.executor_type)?.label || '-'
  const summaryVerification = getCaptchaStrategyLabel(form.executor_type, configOptions.captcha_policy, configOptions.captcha_providers)
  const taskMessages = Array.from(new Set([
    ...(Array.isArray(task?.errors) ? task.errors : []),
    ...(task?.error ? [task.error] : []),
  ]))
  const activeTaskStats = task
    ? [
        { label: '状态', value: getTaskStatusText(task.status), icon: Orbit },
        { label: '进度', value: task.progress || '0/0', icon: Workflow },
        { label: '成功', value: String(task.success ?? 0), icon: CheckCircle },
        { label: '失败', value: String(task.error_count ?? task.errors?.length ?? 0), icon: XCircle },
      ]
    : []
  const summaryTiles = [
    { label: '平台', value: currentPlatform?.display_name || form.platform, icon: Mail },
    { label: '身份', value: summaryRegistration, icon: ShieldCheck },
    { label: '执行', value: summaryExecutor, icon: Workflow },
    { label: '验证', value: summaryVerification, icon: ScanText },
    {
      label: '手机号',
      value: form.phone_provider_enabled ? (currentPhoneProvider?.label || form.phone_provider || '豪猪') : '未启用',
      icon: Smartphone,
    },
    { label: '批量', value: `${effectiveCount}`, icon: Orbit },
    ...(isTwoApiPushPlatform ? [{ label: '2API', value: form.twoapi_push_mode === 'remote' ? '远端推送' : (form.twoapi_push_mode === 'local' ? '本地导入' : '不推送'), icon: Workflow }] : []),
  ]

  return (
    <div className="page-enter space-y-4">
      {/* Header */}
      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-5 py-4 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-[var(--text-2xl)] font-semibold tracking-tight text-[var(--color-text)]">自动注册任务</h1>
          </div>
          <div className="toolbar-strip">
            <Badge variant="default">{currentPlatform?.display_name || form.platform}</Badge>
            <Badge variant="secondary">{summaryRegistration}</Badge>
            <Badge variant="secondary">{summaryExecutor}</Badge>
            {seedLines.length > 0 && <Badge variant="warning">{seedLines.length} 行种子</Badge>}
          </div>
        </div>
      </section>

      {/* 全局错误位：无论哪种身份/执行通道都能看到 provider 加载失败或 2API 远端地址缺失。 */}
      {optionsError ? (
        <div className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{optionsError}</div>
      ) : null}

      {/* Main: Left (config) + Right (submit/task) */}
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(300px,0.8fr)]">
        {/* LEFT: Configuration */}
        <div className="space-y-4">
          {/* Basic Section */}
          <FormSection title="任务参数">
            <div className="grid gap-3 md:grid-cols-2">
              <RegisterSelect form={form} onSet={set} label="平台" k="platform" options={platformOptions} />
              <RegisterTextInput form={form} onSet={set} label="并发数" k="concurrency" type="number" />
            </div>

            {/* Task source: Tabs */}
            <div>
              <FieldLabel label="任务来源" />
              <div className="mb-3 flex rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-0.5">
                <button
                  type="button"
                  onClick={() => { set('seed_lines', ''); set('count', Math.max(1, Number(form.count || 1))) }}
                  className={`flex-1 rounded px-3 py-1.5 text-xs font-medium transition-colors ${!seedLines.length ? 'bg-[var(--color-accent-soft)] text-[var(--color-text)]' : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]'}`}
                >
                  按数量
                </button>
                <button
                  type="button"
                  onClick={() => set('seed_lines', form.seed_lines || '')}
                  className={`flex-1 rounded px-3 py-1.5 text-xs font-medium transition-colors ${seedLines.length > 0 ? 'bg-[var(--color-accent-soft)] text-[var(--color-text)]' : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]'}`}
                >
                  种子文本
                </button>
              </div>
              {seedLines.length > 0 ? (
                <div>
                  <textarea
                    value={form.seed_lines || ''}
                    onChange={(e) => set('seed_lines', e.target.value)}
                    rows={6}
                    placeholder="email----token&#10;email,password&#10;email password [json]"
                    className="control-surface control-surface-mono resize-none"
                  />
                  <div className="mt-2 text-[11px] text-[var(--color-text-muted)]">
                    已检测 {seedLines.length} 行，提交后按行逐个注册（忽略数量设置）
                    {parsedLuckMailLines > 0 ? ` · ${parsedLuckMailLines} 行 LuckMail 格式` : ''}
                  </div>
                </div>
              ) : (
                  <RegisterTextInput form={form} onSet={set} label="批量数量" k="count" type="number" />
              )}
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <RegisterTextInput form={form} onSet={set} label="代理（可选）" k="proxy" placeholder="http://user:pass@host:port" />
                <div className="mt-1 flex items-center gap-1.5 text-[11px]">
                  <span className={`inline-block w-1.5 h-1.5 rounded-full ${resinPreview.proxyUrl ? 'bg-green-400' : 'bg-gray-500'}`} />
                  <span className="text-[var(--color-text-muted)]">
                    {resinPreview.proxyUrl
                      ? form.proxy
                        ? `当前命中 Resin Platform 预览：${resinPreview.resolvedPlatform || 'active'} · 任务代理已覆盖全局 Resin`
                        : `当前命中 Resin Platform 预览：${resinPreview.resolvedPlatform || 'active'} · 沿用全局 Resin 代理`
                      : '无全局 Resin'}
                  </span>
                </div>
              </div>
            </div>
          </FormSection>

          {/* Advanced Section */}
          <details className="group" open>
            <summary className="flex cursor-pointer list-none items-center justify-between rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-5 py-3.5">
              <span className="workspace-kicker">高级编排</span>
              <span className="text-xs text-[var(--color-text-muted)] group-open:hidden">展开</span>
              <span className="hidden text-xs text-[var(--color-text-muted)] group-open:inline">收起</span>
            </summary>

            <div className="mt-4 space-y-4">
              {/* Identity Provider */}
              <FormSection title="注册身份" bodyClassName="">
                <div className="grid gap-3 md:grid-cols-2">
                  {registrationOptions.map((option) => {
                    const active = form.identity_provider === option.identityProvider && form.oauth_provider === option.oauthProvider
                    return (
                      <button
                        key={option.key}
                        type="button"
                        onClick={() => { set('identity_provider', option.identityProvider); set('oauth_provider', option.oauthProvider) }}
                        className={`rounded-lg border px-4 py-3 text-left transition-colors ${active ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)]' : 'border-[var(--color-border)] bg-[var(--color-surface)] hover:border-[var(--color-accent)]/60'}`}
                      >
                        <div className="flex items-center gap-2 text-sm font-medium text-[var(--color-text)]">
                          <Mail className="h-3.5 w-3.5 text-[var(--color-accent)]" />
                          {option.label}
                          {active && <span className="ml-auto text-[11px] font-medium text-[var(--color-text)]">当前</span>}
                        </div>
                        <div className="mt-1 text-xs text-[var(--color-text-muted)]">{option.description}</div>
                      </button>
                    )
                  })}
                </div>
              </FormSection>

              {isTwoApiPushPlatform && (
                <FormSection title={`${twoApiPushPlatformLabel} 注册完成后 2API 推送`} bodyClassName="space-y-3">
                  <div className="grid gap-3 md:grid-cols-3">
                    {[
                      ['none', '不推送', '只保存本机注册结果，后续可在 2API 页面手动推送。'],
                      ['local', '导入本地 2API', `注册成功后直接导入本机 ${twoApiPushPlatformLabel} 2API 账号池。`],
                      ['remote', '推送远端 Linux 2API', '注册成功后把本次账号推到远端 Linux 后端。'],
                    ].map(([value, label, description]) => {
                      const active = (form.twoapi_push_mode || 'none') === value
                      return (
                        <button
                          key={value}
                          type="button"
                          onClick={() => set('twoapi_push_mode', value)}
                          className={active
                            ? 'rounded-lg border border-[var(--color-accent)] bg-[var(--color-accent-soft)] px-4 py-3 text-left transition-colors'
                            : 'rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 text-left transition-colors hover:border-[var(--color-accent)]/60'}
                        >
                          <div className="text-sm font-medium text-[var(--color-text)]">{label}</div>
                          <div className="mt-1 text-xs text-[var(--color-text-muted)]">{description}</div>
                        </button>
                      )
                    })}
                  </div>
                  {form.twoapi_push_mode === 'remote' && (
                    <RegisterTextInput form={form} onSet={set} label="远端 2API 后端地址" k="twoapi_push_target_url" placeholder="http://linux-ip:8000" helper={`会自动拼接 /api/2api/plugins/${form.platform}/import`} />
                  )}
                  <div className="text-[11px] leading-5 text-[var(--color-text-muted)]">
                    本地导入或远端推送失败只写入任务日志，不会把已经注册成功的 {twoApiPushPlatformLabel} 账号判失败。
                  </div>
                </FormSection>
              )}

              {/* Executor */}
              <FormSection title="执行通道" bodyClassName="">
                <div className="grid gap-3 md:grid-cols-3">
                  {executorOptions.map((option) => {
                    const active = form.executor_type === option.value
                    return (
                      <button
                        key={option.value}
                        type="button"
                        disabled={option.disabled}
                        onClick={() => !option.disabled && set('executor_type', option.value)}
                        className={`rounded-lg border px-3 py-3 text-left transition-colors ${option.disabled ? 'cursor-not-allowed opacity-40 border-[var(--color-border)]' : active ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)]' : 'border-[var(--color-border)] bg-[var(--color-surface)] hover:border-[var(--color-accent)]/60'}`}
                      >
                        <div className="text-sm font-medium text-[var(--color-text)]">{option.label}</div>
                        <div className="mt-1 text-xs text-[var(--color-text-muted)]">{option.description}</div>
                        {option.reason && <div className="mt-1 text-[11px] text-amber-400">{option.reason}</div>}
                      </button>
                    )
                  })}
                </div>

                {form.platform === 'swarms' && form.identity_provider === 'mailbox' && (
                  <div className="mt-4 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
                    <div className="workspace-kicker">Swarms 注册链路</div>
                    <div className="mt-3 grid gap-3 md:grid-cols-2">
                      {[
                        ['browser', 'Camoufox 浏览器注册', '使用 Camoufox + Resin IP 打开真实注册页，通过 Vercel 检查后提交邮箱验证，并在浏览器态里创建 API Key；当前推荐链路。'],
                        ['protocol', '协议注册（备用）', '不打开浏览器，走 Supabase/Auth 协议回退；可创建用户和验证邮箱，但当前容易卡在 Vercel/tRPC 或额度刷新。'],
                      ].map(([value, label, description]) => {
                        const active = (form.swarms_registration_mode || 'browser') === value
                        return (
                          <button
                            key={value}
                            type="button"
                            onClick={() => {
                              set('swarms_registration_mode', value)
                              set('executor_type', value === 'browser' ? 'headed' : 'protocol')
                            }}
                            className={active
                              ? 'rounded-lg border border-[var(--color-accent)] bg-[var(--color-accent-soft)] px-4 py-3 text-left transition-colors'
                              : 'rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-4 py-3 text-left transition-colors hover:border-[var(--color-accent)]/60'}
                          >
                            <div className="text-sm font-medium text-[var(--color-text)]">{label}</div>
                            <div className="mt-1 text-xs text-[var(--color-text-muted)]">{description}</div>
                          </button>
                        )
                      })}
                    </div>
                    <div className="mt-3 text-[11px] text-[var(--color-text-muted)]">Camoufox 浏览器注册会强制使用“可视浏览器自动”执行通道；建议配合 Resin 轮换 IP。关闭窗口或取消任务会导致本次注册失败。</div>
                  </div>
                )}

                {form.platform === 'grok' && form.identity_provider === 'mailbox' && form.executor_type === 'cdp_protocol' && (
                  <div className="mt-4 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
                    <div className="workspace-kicker">Grok 注册链路</div>
                    <div className="mt-3 grid gap-3 md:grid-cols-2">
                      {[
                        ['browser', '完整浏览器注册', '真实 Chrome/CDP 打开页面、填写邮箱验证码并等待 sso cookie，不抽取 Turnstile token。'],
                        ['protocol', '协议混合回退', '仅在 Cloudflare 阻断时用 CDP 同步 Cookie，注册提交仍走协议和 Turnstile token。'],
                      ].map(([value, label, description]) => {
                        const active = (form.grok_registration_mode || 'browser') === value
                        return (
                          <button
                            key={value}
                            type="button"
                            onClick={() => set('grok_registration_mode', value)}
                            className={active
                              ? 'rounded-lg border border-[var(--color-accent)] bg-[var(--color-accent-soft)] px-4 py-3 text-left transition-colors'
                              : 'rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-4 py-3 text-left transition-colors hover:border-[var(--color-accent)]/60'}
                          >
                            <div className="text-sm font-medium text-[var(--color-text)]">{label}</div>
                            <div className="mt-1 text-xs text-[var(--color-text-muted)]">{description}</div>
                          </button>
                        )
                      })}
                    </div>
                    <div className="mt-3 text-[11px] text-[var(--color-text-muted)]">默认完整浏览器；仅复现旧协议时切协议混合。</div>
                  </div>
                )}

                {form.identity_provider === 'oauth_browser' && (
                  <div className="mt-4 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
                    {form.oauth_provider === 'pilipala_sso' ? (
                      <>
                        <div className="workspace-kicker">ChatGPT SSO 配置</div>
                        <div className="mt-3 grid gap-3 md:grid-cols-2">
                          <RegisterTextInput form={form} onSet={set} label="SSO 前缀（可选）" k="chatgpt_sso_prefix" placeholder="留空自动随机，例如 aarxxxx" helper="最终邮箱为 前缀@edu.pilipala.store；也可以在“预期登录邮箱”里直接填完整邮箱。" />
                          <RegisterTextInput form={form} onSet={set} label="SSO 域名" k="chatgpt_sso_domain" placeholder="edu.pilipala.store" />
                          <RegisterTextInput form={form} onSet={set} label="SSO 密码" k="chatgpt_sso_password" placeholder="ciallo" type="password" />
                          <RegisterTextInput form={form} onSet={set} label="预期登录邮箱（可选）" k="oauth_email_hint" placeholder="prefix@edu.pilipala.store" />
                        </div>
                        <div className="mt-3 rounded-md border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-3 py-2 text-[11px] leading-5 text-[var(--color-text-muted)]">
                          该方式强制走协议 OAuth：系统会先提交 ChatGPT 企业邮箱，再选择 Ciallo~ SSO，使用前缀和密码完成授权；遇到 add-phone 会自动用同一前缀重跑一次 OAuth。
                        </div>
                      </>
                    ) : (
                      <>
                        <div className="workspace-kicker">OAuth 浏览器配置</div>
                        <div className="mt-3 grid gap-3 md:grid-cols-2">
                          <RegisterTextInput form={form} onSet={set} label="预期登录邮箱" k="oauth_email_hint" placeholder="your-account@example.com" />
                          <RegisterTextInput form={form} onSet={set} label="Chrome CDP 地址" k="chrome_cdp_url" placeholder="http://localhost:9222" />
                          <div className="md:col-span-2">
                            <RegisterTextInput form={form} onSet={set} label="Chrome Profile 路径" k="chrome_user_data_dir" placeholder="~/Library/.../Chrome" />
                          </div>
                          {form.oauth_provider === 'google' && (
                            <div className="md:col-span-2 rounded-md border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-3">
                              <div className="workspace-kicker">Google 账号来源</div>
                              <div className="mt-2 grid gap-3 lg:grid-cols-3">
                                <button
                                  type="button"
                                  onClick={() => { if (hstockplusProvider) { set('google_account_source', 'purchase'); set('mail_provider', 'hstockplus_google') } }}
                                  disabled={!hstockplusProvider}
                                  className={`rounded-lg border px-4 py-3 text-left transition-colors ${currentGoogleAccountMode === 'purchase' ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)]' : 'border-[var(--color-border)] bg-[var(--color-surface)] hover:border-[var(--color-accent)]/60'} ${!hstockplusProvider ? 'cursor-not-allowed opacity-50' : ''}`}
                                >
                                  <div className="text-sm font-medium text-[var(--color-text)]">购买新 Google 账号</div>
                                  <div className="mt-1 text-xs text-[var(--color-text-muted)]">通过 HStockPlus API 新购 Google/Gmail 成品号。</div>
                                </button>
                                <button
                                  type="button"
                                  onClick={() => { if (hstockplusProvider) { set('google_account_source', 'pool'); set('mail_provider', 'hstockplus_google') } }}
                                  disabled={!hstockplusProvider}
                                  className={`rounded-lg border px-4 py-3 text-left transition-colors ${currentGoogleAccountMode === 'pool' ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)]' : 'border-[var(--color-border)] bg-[var(--color-surface)] hover:border-[var(--color-accent)]/60'} ${!hstockplusProvider ? 'cursor-not-allowed opacity-50' : ''}`}
                                >
                                  <div className="text-sm font-medium text-[var(--color-text)]">复用账号池 Google 账号</div>
                                  <div className="mt-1 text-xs text-[var(--color-text-muted)]">从 Google 账号池取号；可指定某个邮箱。</div>
                                </button>
                                <button
                                  type="button"
                                  onClick={() => { set('google_account_source', 'chrome'); set('mail_provider', '') }}
                                  className={`rounded-lg border px-4 py-3 text-left transition-colors ${currentGoogleAccountMode === 'chrome' ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)]' : 'border-[var(--color-border)] bg-[var(--color-surface)] hover:border-[var(--color-accent)]/60'}`}
                                >
                                  <div className="text-sm font-medium text-[var(--color-text)]">复用 Chrome 登录态</div>
                                  <div className="mt-1 text-xs text-[var(--color-text-muted)]">使用上方 Chrome Profile/CDP，不调用账号池。</div>
                                </button>
                              </div>
                              {currentGoogleAccountMode === 'purchase' ? (
                                <div className="mt-4 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
                                  <div className="workspace-kicker">HStockPlus 购买配置</div>
                                  <div className="mt-2 grid gap-3 md:grid-cols-2">
                                    {(hstockplusProvider?.fields || currentMailboxProvider?.fields || []).map((field: any) => renderProviderField(field, !hstockplusProvider))}
                                  </div>
                                  <div className="mt-2 text-[11px] text-[var(--color-text-muted)]">交付后账号会自动写入 Google 账号池。</div>
                                </div>
                              ) : currentGoogleAccountMode === 'pool' ? (
                                <div className="mt-4 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
                                  <div className="workspace-kicker">Google 账号池复用</div>
                                  <div className="mt-2 grid gap-3 md:grid-cols-2">
                                    <RegisterTextInput form={form} onSet={set} label="指定池内邮箱（可选）" k="hstockplus_reuse_email" placeholder="留空则自动选择未注册当前平台的账号" />
                                  </div>
                                  <div className="mt-2 text-[11px] text-[var(--color-text-muted)]">系统会读取池内密码，无需手填。</div>
                                </div>
                              ) : (
                                <div className="mt-4 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3 text-xs text-[var(--color-text-muted)]">
                                  Chrome 登录态模式只使用上方 OAuth 浏览器配置。请填写 Chrome Profile 路径或 Chrome CDP 地址，并确保对应浏览器里已经登录目标 Google 账号。
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                )}
              </FormSection>

              {/* Phone Config (phone identity) */}
              {form.identity_provider === 'phone' && (
                <FormSection title="接码项目" bodyClassName="">
                  <div className="grid gap-3 md:grid-cols-2">
                    <RegisterSelect form={form} onSet={set} label="手机号 Provider" k="phone_provider" options={phoneProviderOptions.length > 0 ? phoneProviderOptions : [['haozhu', '豪猪']]} />
                    <RegisterTextInput form={form} onSet={set} label="短信等待超时（秒）" k="phone_otp_timeout" type="number" />
                    <RegisterTextInput form={form} onSet={set} label="通用项目 / 产品" k="phone_project_id" placeholder="豪猪 sid / 千川 channelId / 5sim product" />
                    {phoneTaskFields.map((field: any) => renderProviderField(field))}
                    {phoneSettingFields.map((field: any) => renderProviderField(field, true))}
                  </div>
                  <div className="mt-3 rounded-md border border-[var(--color-border)] bg-[var(--color-surface-hover)] px-3 py-2 text-xs text-[var(--color-text-muted)]">
                    可选择豪猪、千川或 5sim。5sim 至少需要 API Token、国家、运营商和产品；通用项目 / 产品会兜底映射到 5sim product。
                  </div>
                </FormSection>
              )}

              {/* Mailbox Config (only when mailbox identity) */}
              {form.identity_provider === 'mailbox' && (
                <FormSection title="邮箱来源" bodyClassName="space-y-4">
                  <RegisterSelect form={form} onSet={set} label="邮箱 Provider" k="mail_provider" options={mailboxProviderOptions.length > 0 ? mailboxProviderOptions : [['moemail', 'MoeMail (sall.cc)']]} />

                  {mailboxProviderOptions.length > 1 && (
                    <div>
                      <div className="mb-2 text-[11px] text-[var(--color-text-muted)]">备用邮箱 Provider</div>
                      <div className="flex flex-wrap gap-1.5">
                        {mailboxProviderOptions.filter(([v]: any) => v !== form.mail_provider).map(([value, label]: any) => {
                          const checked = String(form.extra_mail_providers || '').split(',').map((s: string) => s.trim()).includes(value)
                          return (
                            <label key={value} className={`inline-flex cursor-pointer items-center gap-1 rounded-full px-2.5 py-1 text-[11px] transition-colors ${checked ? 'bg-[var(--color-accent-soft)] text-[var(--color-text)]' : 'bg-[var(--color-surface)] text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-hover)]'}`}>
                              <input type="checkbox" className="sr-only" checked={checked} onChange={() => {
                                const cur = String(form.extra_mail_providers || '').split(',').map((s: string) => s.trim()).filter(Boolean)
                                set('extra_mail_providers', checked ? cur.filter((v: string) => v !== value).join(',') : [...cur, value].join(','))
                              }} />
                              {label}
                            </label>
                          )
                        })}
                      </div>
                    </div>
                  )}

                  {(currentMailboxProvider?.fields || []).length > 0 && (
                    <div className="grid gap-3 md:grid-cols-2">{(currentMailboxProvider?.fields || []).map((field: any) => renderProviderField(field))}</div>
                  )}

                  {(form.platform === 'chatgpt' || form.mail_provider === 'outlook_token') && (
                    <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
                      <div className="workspace-kicker">邮箱别名</div>
                      {form.mail_provider === 'outlook_token' && (
                        <label className="mt-2 inline-flex cursor-pointer select-none items-center gap-2">
                          <input
                            type="checkbox"
                            checked={!!form.outlook_alias_enabled}
                            onChange={e => setForm(current => ({
                              ...current,
                              outlook_alias_enabled: e.target.checked,
                              sub_mail_mode: e.target.checked && current.sub_mail_mode === 'none' ? 'plus' : current.sub_mail_mode,
                            }))}
                            className="accent-[var(--color-accent)] h-4 w-4 rounded"
                          />
                          <span className="text-sm text-[var(--color-text)]">启用 Outlook 别名邮箱注册，并把别名加入 Outlook 邮箱池</span>
                        </label>
                      )}
                      {(form.platform === 'chatgpt' || form.outlook_alias_enabled) && (
                        <div className="mt-2 grid gap-3 md:grid-cols-[minmax(0,1fr)_120px_140px]">
                          <RegisterSelect form={form} onSet={set} label="别名模式" k="sub_mail_mode" options={[["none", "关闭"], ["plus", "加号 +"], ["dot", "点号 ."]]} />
                          <RegisterTextInput form={form} onSet={set} label="随机长度" k="sub_mail_length" type="number" />
                          {form.mail_provider === 'outlook_token' && form.outlook_alias_enabled && (
                            <RegisterTextInput form={form} onSet={set} label="父邮箱上限" k="outlook_alias_max_count" type="number" />
                          )}
                        </div>
                      )}
                      {form.mail_provider === 'outlook_token' && form.outlook_alias_enabled && (
                        <div className="mt-2 text-[11px] text-[var(--color-text-muted)]">
                          注册时会使用新别名；收信和刷新 token 仍走父 Outlook 邮箱。父邮箱上限填 0 表示不限，达到上限后不再继续生成新别名。注册成功后，该别名会作为独立条目回收到 Outlook 邮箱池。
                        </div>
                      )}
                    </div>
                  )}

                  {form.platform === 'venice' && (
                    <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
                      <div className="workspace-kicker">Venice 校验</div>
                      <div className="mt-2 grid gap-3 md:grid-cols-[120px_minmax(0,1fr)]">
                        <RegisterTextInput form={form} onSet={set} label="预期 Credits" k="venice_expected_credits" type="number" />
                        <RegisterTextInput form={form} onSet={set} label="API Key 备注" k="venice_api_key_description" placeholder="seedance-auto" />
                      </div>
                    </div>
                  )}

                  <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
                    <div className="workspace-kicker">手机号验证</div>
                    <label className="mt-2 inline-flex cursor-pointer select-none items-center gap-2">
                      <input
                        type="checkbox"
                        checked={!!form.phone_provider_enabled}
                        onChange={e => set('phone_provider_enabled', e.target.checked)}
                        className="accent-[var(--color-accent)] h-4 w-4 rounded"
                      />
                      <span className="text-sm text-[var(--color-text)]">启用手机号接码来源</span>
                    </label>
                    {form.phone_provider_enabled && (
                      <div className="mt-3 grid gap-3 md:grid-cols-2">
                        <RegisterSelect form={form} onSet={set} label="手机号 Provider" k="phone_provider" options={phoneProviderOptions.length > 0 ? phoneProviderOptions : [['haozhu', '豪猪']]} />
                        <RegisterTextInput form={form} onSet={set} label="短信等待超时（秒）" k="phone_otp_timeout" type="number" />
                        {phoneTaskFields.length > 0 && (
                          <div className="md:col-span-2 rounded-md border border-[var(--color-border)] bg-[var(--color-surface-hover)] px-3 py-2 text-xs text-[var(--color-text-muted)]">
                            下面这些是单次任务参数，不会写回手机号来源配置。不同注册平台可以填不同的项目 ID、运营商、地区或指定手机号。
                          </div>
                        )}
                        {phoneTaskFields.map((field: any) => renderProviderField(field))}
                        {phoneSettingFields.map((field: any) => renderProviderField(field, true))}
                      </div>
                    )}
                  </div>

                  {form.platform === 'atxp' && (
                    <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
                      <div className="workspace-kicker">ATXP 验证</div>
                      <label className="mt-2 inline-flex cursor-pointer select-none items-center gap-2">
                        <input
                          type="checkbox"
                          checked={!!form.enable_clowdbot}
                          onChange={e => set('enable_clowdbot', e.target.checked)}
                          className="accent-[var(--color-accent)] h-4 w-4 rounded"
                        />
                        <span className="text-sm text-[var(--color-text)]">启用 Clowdbot 辅助处理验证码</span>
                      </label>
                    </div>
                  )}
                </FormSection>
              )}
            </div>
          </details>
        </div>

        {/* RIGHT: Submit + Task */}
        <div className="space-y-4">
          {/* Submit */}
          <FormSection title="执行注册" className="xl:sticky xl:top-4" bodyClassName="space-y-3">
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
              {summaryTiles.map(({ label, value, icon: Icon }) => (
                <div key={label} className="flex items-center gap-2.5 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2.5">
                  <Icon className="h-3.5 w-3.5 text-[var(--color-text-muted)]" />
                  <div className="min-w-0">
                    <div className="text-[11px] text-[var(--color-text-muted)]">{label}</div>
                    <div className="truncate text-sm font-medium text-[var(--color-text)]">{value}</div>
                  </div>
                </div>
              ))}
            </div>
            <Button onClick={submit} disabled={polling} className="w-full">
              {polling ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" />执行中...</> : <><Play className="mr-2 h-4 w-4" />开始注册</>}
            </Button>
          </FormSection>

          {/* Task Status */}
          {task ? (
            <>
              <FormSection title="任务状态" action={<Badge variant={TASK_STATUS_VARIANTS[task.status] || 'secondary'}>{getTaskStatusText(task.status)}</Badge>} bodyClassName="space-y-3">
                <div className="grid gap-2 sm:grid-cols-2">
                  {activeTaskStats.map(({ label, value, icon: Icon }) => (
                    <div key={label} className="flex items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2">
                      <Icon className="h-3.5 w-3.5 text-[var(--color-text-muted)]" />
                      <div>
                        <div className="text-[11px] text-[var(--color-text-muted)]">{label}</div>
                        <div className="text-sm font-medium tabular-nums text-[var(--color-text)]">{value}</div>
                      </div>
                    </div>
                  ))}
                </div>
                <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2">
                  <div className="text-[11px] text-[var(--color-text-muted)]">任务 ID</div>
                  <div className="break-all font-mono text-xs text-[var(--color-text)]">{task.id}</div>
                </div>
                {taskMessages.length > 0 && (
                  <div className="space-y-1.5">
                    {taskMessages.map((msg: string, i: number) => (
                      <div key={i} className="flex items-start gap-2 rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                        <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        <span className="break-all">{msg}</span>
                      </div>
                    ))}
                  </div>
                )}
                {(task.status === 'interrupted' || task.status === 'cancelled') && !task.error && (
                  <div className="flex items-center gap-2 rounded-md border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
                    <XCircle className="h-3.5 w-3.5" />
                    <span>{task.status === 'cancelled' ? '任务已取消' : '任务已中断，等待恢复或重新提交'}</span>
                  </div>
                )}
              </FormSection>

              <FormSection title="执行日志" bodyClassName="">
                <Suspense fallback={<div className="empty-state-panel">正在加载日志...</div>}>
                  <TaskLogPanel taskId={task.id} onDone={handleTaskDone} />
                </Suspense>
              </FormSection>
            </>
          ) : (
            <FormSection title="任务状态与日志" bodyClassName="">
              <div className="empty-state-panel">提交任务后显示状态和日志。</div>
            </FormSection>
          )}
        </div>
      </div>
    </div>
  )
}
