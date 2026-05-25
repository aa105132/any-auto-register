export interface Account {
  id: number
  email?: string
  password?: string
  overview?: Record<string, any>
  provider_resources?: any[]
  provider_accounts?: any[]
  credentials?: any[]
  lifecycle_status?: string
  display_status?: string
  plan_state?: string
  validity_status?: string
  cashier_url?: string
  created_at?: string
  [key: string]: any
}

export function getAccountOverview(acc: Account) { return acc?.overview || {} }

export function getVerificationMailbox(acc: Account) {
  const resources = Array.isArray(acc?.provider_resources) ? acc.provider_resources : []
  const normalized = resources.find((item: any) => item?.resource_type === 'mailbox')
  if (normalized) {
    return { provider: normalized.provider_name, email: normalized.handle || normalized.display_name, account_id: normalized.resource_identifier }
  }
  return null
}

export function getLifecycleStatus(acc: Account) { return acc?.lifecycle_status || 'registered' }
export function getDisplayStatus(acc: Account) { return acc?.display_status || acc?.plan_state || getLifecycleStatus(acc) }
export function getPlanState(acc: Account) { return acc?.plan_state || acc?.overview?.plan_state || 'unknown' }
export function getValidityStatus(acc: Account) { return acc?.validity_status || acc?.overview?.validity_status || 'unknown' }

export function getCompactStatusMeta(acc: Account) {
  return `生命周期:${getLifecycleStatus(acc)} / 套餐:${getPlanState(acc)} / 有效:${getValidityStatus(acc)}`
}

export function getProviderAccounts(acc: Account) { return Array.isArray(acc?.provider_accounts) ? acc.provider_accounts : [] }
export function getCredentials(acc: Account) { return Array.isArray(acc?.credentials) ? acc.credentials : [] }

export function getCredentialValue(acc: Account, key: string) {
  return getCredentials(acc).find((item: any) => item?.scope === 'platform' && item?.key === key)?.value || ''
}

export function getCashierUrl(acc: Account) {
  const overview = getAccountOverview(acc)
  return overview?.cashier_url || acc?.cashier_url || ''
}

export function getPrimaryToken(acc: Account) {
  const overview = getAccountOverview(acc)
  return overview?.primary_token || acc?.primary_token || ''
}

export function getAccessToken(acc: Account) {
  const overview = getAccountOverview(acc)
  return overview?.access_token || acc?.access_token || ''
}

export function getOAuthStatus(acc: Account) {
  const overview = getAccountOverview(acc)
  return overview?.oauth_status || acc?.oauth_status || ''
}

export function escapeCsvField(v: any): string {
  if (v == null) return ''
  const s = String(v)
  if (s.includes(',') || s.includes('"') || s.includes('\n')) return `"${s.replace(/"/g, '""')}"`
  return s
}
