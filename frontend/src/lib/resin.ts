type ResinConfigValues = Record<string, any>

export type ResinPreview = {
  enabled: boolean
  source: 'override' | 'structured' | 'legacy_url' | 'disabled' | 'none'
  resolvedPlatform: string
  proxyUrl: string
  detail: string
  sourceLabel: string
}

function isTruthyConfigValue(value: unknown) {
  const raw = String(value || '').trim().toLowerCase()
  return raw === '1' || raw === 'true' || raw === 'yes' || raw === 'on' || raw === 'enabled'
}

export function parseResinPlatformMap(raw: unknown) {
  const mapping: Record<string, string> = {}
  for (const line of String(raw || '').split(/\r?\n/)) {
    const normalized = line.trim()
    if (!normalized || normalized.startsWith('#')) continue

    let left = ''
    let right = ''
    if (normalized.includes('=')) {
      ;[left, right] = normalized.split('=', 2)
    } else if (normalized.includes(':')) {
      ;[left, right] = normalized.split(':', 2)
    } else {
      continue
    }

    const taskPlatform = left.trim().toLowerCase()
    const resinPlatform = right.trim()
    if (taskPlatform && resinPlatform) {
      mapping[taskPlatform] = resinPlatform
    }
  }
  return mapping
}

function resolveResinPlatform(taskPlatform: string, config: ResinConfigValues) {
  const normalizedTaskPlatform = String(taskPlatform || '').trim().toLowerCase()
  const mapping = parseResinPlatformMap(config.resin_platform_map)
  if (normalizedTaskPlatform && mapping[normalizedTaskPlatform]) {
    return mapping[normalizedTaskPlatform]
  }
  return String(config.resin_default_platform || 'Default').trim() || 'Default'
}

function normalizeResinScheme(value: unknown) {
  const raw = String(value || '').trim().toLowerCase()
  return raw === 'socks5' ? 'socks5' : 'http'
}

function normalizeResinPort(value: unknown) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed <= 0) return '2260'
  return String(Math.trunc(parsed))
}

function normalizeProxyUrl(value: string) {
  const trimmed = value.trim()
  if (!trimmed) return ''
  if (/^[a-z]+:\/\//i.test(trimmed)) {
    return trimmed
  }
  return `http://${trimmed}`
}

function encodeProxyPart(value: string) {
  return encodeURIComponent(value).replace(/%3A/gi, ':')
}

function buildStructuredProxyUrl(config: ResinConfigValues, resolvedPlatform: string) {
  const host = String(config.resin_host || '').trim()
  if (!host) return ''

  const scheme = normalizeResinScheme(config.resin_scheme)
  const port = normalizeResinPort(config.resin_port)
  const token = String(config.resin_token || '').trim()
  const encodedPlatform = encodeProxyPart(resolvedPlatform)
  const encodedToken = token ? encodeProxyPart(token) : ''

  const auth = encodedPlatform
    ? `${encodedPlatform}${encodedToken ? `:${encodedToken}` : ''}@`
    : encodedToken
      ? `:${encodedToken}@`
      : ''

  return `${scheme}://${auth}${host}:${port}`
}

export function resolveResinProxyPreview({
  config,
  taskPlatform,
  taskProxy,
}: {
  config?: ResinConfigValues | null
  taskPlatform?: string
  taskProxy?: string
}): ResinPreview {
  const values = config || {}
  const normalizedTaskProxy = String(taskProxy || '').trim()
  const resolvedPlatform = resolveResinPlatform(String(taskPlatform || ''), values)

  if (normalizedTaskProxy) {
    return {
      enabled: true,
      source: 'override',
      resolvedPlatform,
      proxyUrl: normalizedTaskProxy,
      detail: '当前任务会直接使用输入框里的代理地址，不再套用全局 Resin 平台映射。',
      sourceLabel: '任务级代理覆盖',
    }
  }

  const enabled = isTruthyConfigValue(values.resin_enabled)
  if (!enabled) {
    return {
      enabled: false,
      source: 'disabled',
      resolvedPlatform,
      proxyUrl: '',
      detail: '当前留空 proxy 时不会命中 Resin，全局出口会继续回退到后端默认代理池。',
      sourceLabel: '未启用',
    }
  }

  const structuredProxyUrl = buildStructuredProxyUrl(values, resolvedPlatform)
  if (structuredProxyUrl) {
    return {
      enabled: true,
      source: 'structured',
      resolvedPlatform,
      proxyUrl: structuredProxyUrl,
      detail: '当前会根据任务平台命中 Resin Platform，再用主机、端口和 Token 组装最终代理 URL。',
      sourceLabel: '结构化 Resin 配置',
    }
  }

  const legacyProxyUrl = normalizeProxyUrl(String(values.resin_proxy_url || ''))
  if (legacyProxyUrl) {
    return {
      enabled: true,
      source: 'legacy_url',
      resolvedPlatform,
      proxyUrl: legacyProxyUrl,
      detail: '当前走兼容 URL 模式；如果需要按平台细分流量，建议切换到结构化 Resin 字段。',
      sourceLabel: '兼容 URL 模式',
    }
  }

  return {
    enabled: false,
    source: 'none',
    resolvedPlatform,
    proxyUrl: '',
    detail: '已启用 Resin，但还没有可用的 Resin 主机或兼容 URL，当前无法生成最终代理出口。',
    sourceLabel: '未配置出口',
  }
}
