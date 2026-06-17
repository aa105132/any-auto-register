export const ALL_OAUTH_PROVIDERS = [
  { value: 'google', label: 'Google' },
  { value: 'github', label: 'GitHub' },
  { value: 'microsoft', label: 'Microsoft' },
  { value: 'linkedin', label: 'LinkedIn' },
  { value: 'apple', label: 'Apple' },
  { value: 'x', label: 'X' },
  { value: 'builderid', label: 'Builder ID' },
  { value: 'pilipala_sso', label: 'Pilipala SSO' },
]

export const EXECUTOR_LABELS: Record<string, string> = {
  protocol: '协议模式',
  headless: '后台浏览器自动',
  headed: '可视浏览器自动',
  cdp_protocol: 'CDP 协议混合',
}

export const IDENTITY_MODE_LABELS: Record<string, string> = {
  mailbox: '系统邮箱',
  oauth_browser: '第三方账号',
  phone: '手机号',
}

export function hasReusableOAuthBrowser(config: { chrome_user_data_dir?: string; chrome_cdp_url?: string }) {
  return Boolean(config.chrome_user_data_dir?.trim() || config.chrome_cdp_url?.trim())
}

export function getOAuthProviderLabel(provider: string) {
  return ALL_OAUTH_PROVIDERS.find(item => item.value === provider)?.label || provider
}

export function getIdentityModeLabel(mode: string) {
  return IDENTITY_MODE_LABELS[mode] || mode
}

export function pickOAuthExecutor(
  supportedExecutors: string[],
  preferredExecutor: string,
  reusableBrowser: boolean,
) {
  if (supportedExecutors.includes(preferredExecutor) && preferredExecutor !== 'protocol') {
    return preferredExecutor
  }
  if (reusableBrowser && supportedExecutors.includes('headless')) {
    return 'headless'
  }
  if (supportedExecutors.includes('headed')) {
    return 'headed'
  }
  if (supportedExecutors.includes('headless')) {
    return 'headless'
  }
  return supportedExecutors[0] || 'protocol'
}

export function buildRegistrationOptions(platformMeta: any) {
  const supportedModes: string[] = platformMeta?.supported_identity_modes || ['mailbox']
  const supportedOAuth: string[] = platformMeta?.supported_oauth_providers || []
  const options: Array<{
    key: string
    label: string
    description: string
    identityProvider: string
    oauthProvider: string
  }> = []

  if (supportedModes.includes('mailbox')) {
    options.push({
      key: 'mailbox',
      label: '系统邮箱',
      description: '使用系统集成邮箱自动收验证码并完成注册',
      identityProvider: 'mailbox',
      oauthProvider: '',
    })
  }

  if (supportedModes.includes('oauth_browser')) {
    supportedOAuth.forEach((provider: string) => {
      options.push({
        key: `oauth:${provider}`,
        label: getOAuthProviderLabel(provider),
        description: provider === 'pilipala_sso'
          ? '使用 edu.pilipala.store SSO 纯协议 OAuth 授权'
          : `使用 ${getOAuthProviderLabel(provider)} 账号自动创建平台账号`,
        identityProvider: 'oauth_browser',
        oauthProvider: provider,
      })
    })
  }

  if (supportedModes.includes('phone')) {
    options.push({
      key: 'phone',
      label: '手机号注册',
      description: '输入接码项目 ID，系统自动取号、收码并创建 API Key',
      identityProvider: 'phone',
      oauthProvider: '',
    })
  }

  return options
}

export function buildExecutorOptions(
  identityProvider: string,
  supportedExecutors: string[],
  reusableBrowser: boolean,
) {
  return supportedExecutors.map((executor) => {
    const option = {
      value: executor,
      label: EXECUTOR_LABELS[executor] || executor,
      description: '',
      disabled: false,
      reason: '',
    }

    if (executor === 'protocol') {
      option.description = '不打开浏览器，直接通过协议流程自动注册'
      return option
    }

    if (executor === 'headless') {
      option.description = identityProvider === 'mailbox'
        ? '浏览器在后台自动执行，界面不可见'
        : '复用本机浏览器登录态，在后台自动完成第三方登录'
      if (identityProvider === 'oauth_browser' && !reusableBrowser) {
        option.disabled = true
        option.reason = '需要先在全局配置里填写 Chrome Profile 路径或 Chrome CDP 地址'
      }
      return option
    }

    if (executor === 'cdp_protocol') {
      option.description = identityProvider === 'mailbox'
        ? 'CDP 启动 Chrome 过 Turnstile，其余步骤走协议，兼顾速度和通过率'
        : '使用隔离 Chrome Profile 执行 OAuth，再通过协议创建 API Key'
      return option
    }

    option.description = '会打开浏览器窗口，但系统仍自动执行，无需额外交互'
    return option
  })
}
