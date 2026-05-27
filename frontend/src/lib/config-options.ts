export type ProviderField = {
  key: string
  label: string
  placeholder?: string
  secret?: boolean
  category?: string
  type?: string
  options?: Array<{ value: string; label: string }>
}

export type ProviderOption = {
  provider_type?: string
  provider_key?: string
  value: string
  label: string
  description?: string
  driver_type?: string
  default_auth_mode?: string
  auth_modes?: Array<{ value: string; label: string }>
  fields: ProviderField[]
}

export type ProviderDriver = {
  provider_type?: string
  driver_type: string
  label: string
  description?: string
  default_auth_mode?: string
  auth_modes?: Array<{ value: string; label: string }>
  fields: ProviderField[]
}

export type CaptchaPolicy = {
  protocol_mode?: string
  protocol_order?: string[]
  browser_mode?: string
}

export type ConfigOptionsResponse = {
  mailbox_providers: ProviderOption[]
  captcha_providers: ProviderOption[]
  phone_providers?: ProviderOption[]
  mailbox_drivers?: ProviderDriver[]
  captcha_drivers?: ProviderDriver[]
  phone_drivers?: ProviderDriver[]
  mailbox_settings?: ProviderSetting[]
  captcha_settings?: ProviderSetting[]
  phone_settings?: ProviderSetting[]
  captcha_policy?: CaptchaPolicy
}

export type ProviderSetting = {
  id: number
  provider_type: string
  provider_key: string
  display_name: string
  catalog_label: string
  description?: string
  driver_type?: string
  auth_mode: string
  auth_modes: Array<{ value: string; label: string }>
  enabled: boolean
  is_default: boolean
  fields: ProviderField[]
  config: Record<string, string>
  auth: Record<string, string>
  auth_preview?: Record<string, string>
  metadata?: Record<string, unknown>
}

export function getProviderSelectOptions(providers: ProviderOption[]): Array<[string, string]> {
  return providers.map(provider => [provider.value, provider.label])
}

export function listProviderFieldKeys(providers: ProviderOption[] = [], categories?: string[]): string[] {
  const keys = new Set<string>()
  const allowed = categories ? new Set(categories) : null
  providers.forEach(provider => {
    ;(provider.fields || []).forEach(field => {
      if (field.key && (!allowed || allowed.has(field.category || ''))) {
        keys.add(field.key)
      }
    })
  })
  return Array.from(keys)
}

export function getCaptchaStrategyLabel(executorType: string, policy?: CaptchaPolicy, providers?: ProviderOption[]) {
  if (executorType === 'headless' || executorType === 'headed') {
    const browserDefault = policy?.browser_mode || 'local_solver'
    const label = providers?.find(item => item.value === browserDefault)?.label || '本地 Solver (Camoufox)'
    return `浏览器模式自动使用 ${label}`
  }
  const order = policy?.protocol_order || ['yescaptcha', '2captcha']
  const labels = order.map(value => providers?.find(item => item.value === value)?.label || value)
  return `协议模式按顺序自动选择远程打码服务：${labels.join(' -> ')}`
}
