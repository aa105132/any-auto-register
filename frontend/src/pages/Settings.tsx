import { Fragment, useEffect, useState } from 'react'
import { getConfig, getConfigOptions, getPlatforms, invalidateConfigCache, invalidateConfigOptionsCache, invalidatePlatformsCache } from '@/lib/app-data'
import type { ConfigOptionsResponse, ProviderDriver, ProviderOption, ProviderSetting } from '@/lib/config-options'
import { getCaptchaStrategyLabel } from '@/lib/config-options'
import { apiDownload, apiFetch, triggerBrowserDownload } from '@/lib/utils'
import { ALL_OAUTH_PROVIDERS, getIdentityModeLabel, getOAuthProviderLabel } from '@/lib/registration'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'
import { Save, Eye, EyeOff, Mail, Shield, Cpu, RefreshCw, CheckCircle, XCircle, Sliders, Plus, X, Orbit, Sparkles, Download, Smartphone } from 'lucide-react'
import { cn } from '@/lib/utils'

const ALL_IDENTITY_MODES = ['mailbox', 'oauth_browser']
type ProviderType = 'mailbox' | 'captcha' | 'phone'

const SUB_MAIL_MODE_OPTIONS = [
  { label: '关闭别名重试', value: 'none' },
  { label: '原邮箱+', value: 'plus' },
  { label: '原邮箱.', value: 'dot' },
]

function normalizeSubMailMode(value: unknown) {
  const raw = String(value || '').trim().toLowerCase()
  if (raw === 'plus' || raw === '原邮箱+') return 'plus'
  if (raw === 'dot' || raw === '原邮箱.') return 'dot'
  return 'none'
}

function normalizeSubMailLength(value: unknown, fallback = 4) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(1, Math.min(16, Math.trunc(parsed)))
}

function isTruthyConfigValue(value: unknown) {
  const raw = String(value || '').trim().toLowerCase()
  return raw === '1' || raw === 'true' || raw === 'yes' || raw === 'on' || raw === 'enabled'
}

function getProviderFieldErrorMessage(error: unknown, fallback: string) {
  if (error instanceof Error && error.message) return error.message
  return fallback
}

function SettingsMetric({
  label,
  value,
  icon: Icon,
}: {
  label: string
  value: string | number
  icon: any
}) {
  return (
    <div className="rounded-[16px] border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[11px] tracking-[0.16em] text-[var(--color-text-muted)]">{label}</div>
          <div className="mt-0.5 text-lg font-semibold tracking-[-0.03em] text-[var(--color-text)]">{value}</div>
        </div>
        <div className="flex h-8 w-8 items-center justify-center rounded-[12px] border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-accent)]">
          <Icon className="h-3.5 w-3.5" />
        </div>
      </div>
    </div>
  )
}

function PlatformCapsTab() {
  const [platforms, setPlatforms] = useState<any[]>([])
  const [drafts, setDrafts] = useState<Record<string, any>>({})
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  const [saved, setSaved] = useState<Record<string, boolean>>({})

  useEffect(() => {
    getPlatforms().then((list: any[]) => {
      setPlatforms(list)
      const init: Record<string, any> = {}
      list.forEach(p => {
        init[p.name] = {
          supported_identity_modes: [...p.supported_identity_modes],
          supported_oauth_providers: [...p.supported_oauth_providers],
        }
      })
      setDrafts(init)
    })
  }, [])

  const toggle = (name: string, field: string, value: string) => {
    setDrafts(d => {
      const arr: string[] = [...(d[name]?.[field] || [])]
      const idx = arr.indexOf(value)
      if (idx >= 0) arr.splice(idx, 1); else arr.push(value)
      return { ...d, [name]: { ...d[name], [field]: arr } }
    })
  }

  const save = async (name: string) => {
    setSaving(s => ({ ...s, [name]: true }))
    try {
      await apiFetch(`/platforms/${name}/capabilities`, { method: 'PUT', body: JSON.stringify(drafts[name]) })
      invalidatePlatformsCache()
      setSaved(s => ({ ...s, [name]: true }))
      setTimeout(() => setSaved(s => ({ ...s, [name]: false })), 2000)
    } finally { setSaving(s => ({ ...s, [name]: false })) }
  }

  const reset = async (name: string) => {
    await apiFetch(`/platforms/${name}/capabilities`, { method: 'DELETE' })
    invalidatePlatformsCache()
    const list = await getPlatforms({ force: true })
    const p = list.find((x: any) => x.name === name)
    if (p) setDrafts(d => ({ ...d, [name]: { supported_identity_modes: [...p.supported_identity_modes], supported_oauth_providers: [...p.supported_oauth_providers] } }))
  }

  return (
    <div className="space-y-4">
      {platforms.map(p => {
        const draft = drafts[p.name] || {}
        const modes: string[] = draft.supported_identity_modes || []
        const oauths: string[] = draft.supported_oauth_providers || []
        return (
          <div key={p.name} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="text-sm font-semibold text-[var(--color-text)]">{p.display_name}</h3>
                <p className="text-xs text-[var(--color-text-muted)] mt-0.5">{p.name} v{p.version}</p>
              </div>
              <button onClick={() => reset(p.name)}
                className="table-action-btn">
                恢复默认
              </button>
            </div>
            <div className="space-y-3">
              <div>
                <p className="text-xs text-[var(--color-text-muted)] mb-2">注册身份</p>
                <div className="flex gap-4">
                  {ALL_IDENTITY_MODES.map(m => (
                    <label key={m} className="flex items-center gap-1.5 text-xs text-[var(--color-text-secondary)] cursor-pointer">
                      <input type="checkbox" checked={modes.includes(m)}
                        onChange={() => toggle(p.name, 'supported_identity_modes', m)}
                        className="checkbox-accent" />
                      {getIdentityModeLabel(m)}
                    </label>
                  ))}
                </div>
              </div>
              <div>
                <p className="text-xs text-[var(--color-text-muted)] mb-2">第三方入口</p>
                <div className="flex flex-wrap gap-4">
                  {ALL_OAUTH_PROVIDERS.map(o => (
                    <label key={o.value} className="flex items-center gap-1.5 text-xs text-[var(--color-text-secondary)] cursor-pointer">
                      <input type="checkbox" checked={oauths.includes(o.value)}
                        onChange={() => toggle(p.name, 'supported_oauth_providers', o.value)}
                        className="checkbox-accent" />
                      {getOAuthProviderLabel(o.value)}
                    </label>
                  ))}
                </div>
              </div>
            </div>
            <div className="mt-4">
              <Button size="sm" onClick={() => save(p.name)} disabled={saving[p.name]}>
                <Save className="h-3.5 w-3.5 mr-1" />
                {saved[p.name] ? '已保存 ✓' : saving[p.name] ? '保存中...' : '保存'}
              </Button>
            </div>
          </div>
        )
      })}
    </div>
  )
}

const SELECT_FIELDS: Record<string, { label: string; value: string }[]> = {
  default_executor: [
    { label: '协议模式', value: 'protocol' },
    { label: '后台浏览器自动', value: 'headless' },
    { label: '可视浏览器自动', value: 'headed' },
    { label: 'CDP 协议混合', value: 'cdp_protocol' },
  ],
  default_identity_provider: [
    { label: '系统邮箱', value: 'mailbox' },
    { label: '第三方账号', value: 'oauth_browser' },
  ],
  default_oauth_provider: [
    { label: '不预选，由当前页面选择', value: '' },
    { label: 'GitHub', value: 'github' },
    { label: 'Google', value: 'google' },
    { label: 'Microsoft', value: 'microsoft' },
    { label: 'LinkedIn', value: 'linkedin' },
    { label: 'Apple', value: 'apple' },
    { label: 'X', value: 'x' },
    { label: 'Builder ID', value: 'builderid' },
  ],
  sub_mail_mode: SUB_MAIL_MODE_OPTIONS,
  resin_scheme: [
    { label: 'HTTP Forward Proxy', value: 'http' },
    { label: 'SOCKS5 Forward Proxy', value: 'socks5' },
  ],
  scdn_runtime_protocol: [
    { label: 'HTTP', value: 'http' },
    { label: 'HTTPS', value: 'https' },
    { label: 'SOCKS4', value: 'socks4' },
    { label: 'SOCKS5', value: 'socks5' },
  ],
}

const RESIN_TEMPLATE_PLATFORMS = ['chatgpt', 'cursor', 'grok', 'kiro', 'openblocklabs', 'tavily', 'trae', 'atxp', 'venice']
const RESIN_SAME_NAME_TEMPLATE = RESIN_TEMPLATE_PLATFORMS.map(platform => `${platform}=${platform}`).join('\n')
const RESIN_EXAMPLE_TEMPLATE = [
  'venice=SeedancePool',
  'chatgpt=OpenAIPool',
  'cursor=CursorPool',
  'grok=GrokPool',
].join('\n')

const TABS: { id: string; label: string; icon: any; sections?: any[] }[] = [
  {
    id: 'register', label: '注册策略', icon: Cpu,
    sections: [{
      section: '默认注册策略',
      desc: '这里配置的是默认行为，自动注册弹窗和账号列表会直接复用这些设置。',
      items: [
        { key: 'default_identity_provider', label: '默认注册身份' },
        { key: 'default_oauth_provider', label: '默认第三方入口', placeholder: '' },
        { key: 'default_executor', label: '默认执行方式' },
        { key: 'sub_mail_mode', label: '子邮箱模式' },
        { key: 'sub_mail_length', label: '子邮箱长度', placeholder: '4', type: 'number', min: 1, max: 16 },
      ],
    }, {
      section: '验证码 / OTP',
      desc: '这里控制协议注册时等待验证码与登录 OTP 的超时策略。修改后新任务立即生效。',
      items: [
        { key: 'registration.otp_timeout', label: '注册验证码总超时（秒）', placeholder: '120', type: 'number', min: 1 },
        { key: 'registration.otp_resend_interval', label: '注册验证码重发间隔（秒）', placeholder: '10', type: 'number', min: 1 },
        { key: 'registration.login_otp_timeout', label: '登录 OTP 总超时（秒）', placeholder: '120', type: 'number', min: 1 },
      ],
    }, {
      section: '浏览器复用',
      desc: '第三方账号走后台浏览器自动时，通常需要复用本机已登录浏览器。',
      items: [
        { key: 'oauth_email_hint', label: '预期登录邮箱', placeholder: 'your-account@example.com' },
        { key: 'chrome_user_data_dir', label: 'Chrome Profile 路径', placeholder: '~/Library/Application Support/Google/Chrome' },
        { key: 'chrome_cdp_url', label: 'Chrome CDP 地址', placeholder: 'http://localhost:9222' },
      ],
    }],
  },
  {
    id: 'mailbox', label: '邮箱服务', icon: Mail,
    sections: [],
  },
  {
    id: 'captcha', label: '验证服务', icon: Shield,
    sections: [],
  },
  {
    id: 'phone', label: '手机号服务', icon: Smartphone,
    sections: [],
  },
  {
    id: 'proxy', label: '代理 / Resin / Decodo / BrightData', icon: Orbit,
    sections: [{
      section: 'Resin 统一代理入口',
      desc: '给留空 proxy 的注册任务提供统一出口。单次任务里手动填写 proxy 时，仍然以任务内输入为准。推荐填写结构化字段；旧的完整 URL 仅作为兼容兜底。',
      items: [
        { key: 'resin_enabled', label: '启用 Resin 全局代理', type: 'checkbox' },
        { key: 'resin_scheme', label: '代理协议' },
        { key: 'resin_host', label: 'Resin 主机', placeholder: '127.0.0.1' },
        { key: 'resin_port', label: 'Resin 端口', placeholder: '2260', type: 'number', min: 1 },
        { key: 'resin_token', label: 'Resin Token', secret: true, placeholder: 'my-token' },
        { key: 'resin_default_platform', label: '默认 Resin Platform', placeholder: 'Default' },
        { key: 'resin_platform_map', label: '平台映射', type: 'textarea', placeholder: 'venice=SeedancePool\nchatgpt=OpenAIPool' },
        { key: 'resin_proxy_url', label: '兼容 URL（可选）', placeholder: 'http://Default:my-token@127.0.0.1:2260' },
      ],
    }, {
      section: 'Decodo 数据中心代理',
      desc: 'Decodo (原 Smartproxy) 数据中心代理。优先级在 Resin 之后、SCDN 之前。留空端口则按线程分配静态端口（sticky IP），填 10000 则所有线程共享 rotating 模式。',
      items: [
        { key: 'decodo_enabled', label: '启用 Decodo', type: 'checkbox' },
        { key: 'decodo_host', label: '主机', placeholder: 'dc.decodo.com' },
        { key: 'decodo_username', label: '用户名', placeholder: 'spXXXXXX' },
        { key: 'decodo_password', label: '密码', secret: true, placeholder: '' },
        { key: 'decodo_port', label: '端口（留空=sticky）', placeholder: '留空或 10000', type: 'number', min: 0 },
        { key: 'decodo_port_base', label: 'Sticky 起始端口', placeholder: '10001', type: 'number', min: 1 },
      ],
    }, {
      section: 'Bright Data 数据中心代理',
      desc: 'Bright Data 数据中心代理。优先级在 Decodo 之后、SCDN 之前。通过 session 参数实现 sticky IP，用于 Venice 等平台的 IP 去重。',
      items: [
        { key: 'brightdata_enabled', label: '启用 Bright Data', type: 'checkbox' },
        { key: 'brightdata_username', label: '用户名', placeholder: 'brd-customer-hl_xxx-zone-datacenter_proxy1' },
        { key: 'brightdata_password', label: '密码', secret: true, placeholder: '' },
        { key: 'brightdata_host', label: '主机', placeholder: 'brd.superproxy.io' },
        { key: 'brightdata_port', label: '端口', placeholder: '33335', type: 'number', min: 1 },
      ],
    }, {
      section: 'SCDN 运行时来源',
      desc: '作为 Resin 之后、后端代理池之前的第三来源。运行时即时拉取候选代理，并先做可用性探测，再下发给任务使用。',
      items: [
        { key: 'scdn_runtime_enabled', label: '启用 SCDN 运行时来源', type: 'checkbox' },
        { key: 'scdn_runtime_protocol', label: '拉取协议' },
        { key: 'scdn_runtime_country_code', label: '国家代码', placeholder: 'HK / US / SG' },
        { key: 'scdn_runtime_count', label: '单次拉取数量', placeholder: '10', type: 'number', min: 1 },
        { key: 'scdn_runtime_validate_url', label: '可用性检测 URL', placeholder: 'https://httpbin.org/ip' },
        { key: 'scdn_runtime_validate_timeout_sec', label: '检测超时（秒）', placeholder: '8', type: 'number', min: 1 },
        { key: 'scdn_runtime_cache_ttl_sec', label: '缓存 TTL（秒）', placeholder: '120', type: 'number', min: 1 },
        { key: 'scdn_runtime_cache_size', label: '缓存上限', placeholder: '20', type: 'number', min: 1 },
      ],
    }, {
      section: '订阅代理 / sing-box',
      desc: '通过机场订阅链接拉取节点，启动 sing-box 内核作为本地代理。优先级在 SCDN 之后、后端代理池之前。需要 sing-box 内核（可自动下载）。',
      items: [
        { key: 'subscription_proxy_enabled', label: '启用订阅代理', type: 'checkbox' },
        { key: 'subscription_proxy_url', label: '订阅链接', secret: true, placeholder: 'https://example.com/sub?token=xxx' },
        { key: 'subscription_proxy_listen', label: '本地监听地址', placeholder: 'http://127.0.0.1:18080' },
        { key: 'subscription_proxy_strategy', label: '节点选择策略', placeholder: 'urltest / round_robin / manual' },
        { key: 'subscription_proxy_kernel_path', label: 'sing-box 内核路径', placeholder: 'auto（自动检测或下载）' },
        { key: 'subscription_proxy_max_nodes', label: '最大节点数', placeholder: '50', type: 'number', min: 1 },
        { key: 'subscription_proxy_refresh_interval_min', label: '订阅刷新间隔（分钟）', placeholder: '30', type: 'number', min: 1 },
        { key: 'subscription_proxy_check', label: '延迟检测 URL', placeholder: 'https://www.gstatic.com/generate_204' },
        { key: 'subscription_proxy_check_interval', label: '延迟检测间隔（秒）', placeholder: '30', type: 'number', min: 1 },
        { key: 'subscription_proxy_fetch_via_proxy', label: '通过上游代理拉取订阅', type: 'checkbox' },
        { key: 'subscription_proxy_manual_node_tag', label: '手动指定节点 tag', placeholder: '仅 manual 策略生效' },
        { key: 'subscription_proxy_whitelist_tags', label: '白名单节点 tags', type: 'textarea', placeholder: '每行一个 tag，仅 whitelist_round_robin 策略生效' },
        { key: 'subscription_proxy_blacklist_tags', label: '黑名单节点 tags', type: 'textarea', placeholder: '每行一个 tag，仅 blacklist_round_robin 策略生效' },
      ],
    }],
  },
  {
    id: 'platform_caps', label: '高级：平台能力', icon: Sliders,
    sections: [],
  },
  {
    id: 'chatgpt', label: 'ChatGPT', icon: Shield,
    sections: [{
      section: 'CPA 面板',
      desc: '注册完成后自动上传到 CPA 管理平台',
      items: [
        { key: 'cpa_api_url', label: 'API URL', placeholder: 'https://your-cpa.example.com' },
        { key: 'cpa_api_key', label: 'API Key', secret: true },
      ],
    }, {
      section: 'Team Manager',
      desc: '上传到自建 Team Manager 系统',
      items: [
        { key: 'team_manager_url', label: 'API URL', placeholder: 'https://your-tm.example.com' },
        { key: 'team_manager_key', label: 'API Key', secret: true },
      ],
    }],
  },
  {
    id: 'enter', label: 'Enter', icon: Shield,
    sections: [{
      section: 'Enter2API',
      desc: 'Enter 注册成功后可自动导入到 enter2api 管理端，同时导出 AI API Token 到 output/keys.txt 和 output/ai_api_tokens.txt。',
      items: [
        { key: 'enter2api_enabled', label: '启用自动推送', type: 'checkbox' },
        { key: 'enter2api_url', label: 'API URL', placeholder: 'http://127.0.0.1:8899' },
      ],
    }],
  },
  {
    id: 'codebanana', label: 'CodeBanana', icon: Shield,
    sections: [{
      section: 'CodeBanana2API',
      desc: 'CodeBanana 注册成功后可自动导入到 codebanana2api 管理端。支持填写基础地址，系统会自动补全 /api/admin/accounts。',
      items: [
        { key: 'codebanana2api_enabled', label: '启用自动导入', type: 'checkbox' },
        { key: 'codebanana2api_url', label: 'API URL', placeholder: 'http://127.0.0.1:8080' },
      ],
    }],
  },
  {
    id: 'anuma', label: 'Anuma', icon: Shield,
    sections: [{
      section: 'Anuma2API',
      desc: 'Anuma 注册成功后可自动导入到 anuma2api 管理端。Session 模式支持 JWT 自动刷新。',
      items: [
        { key: 'anuma2api_enabled', label: '启用自动导入', type: 'checkbox' },
        { key: 'anuma2api_url', label: 'API URL', placeholder: 'http://127.0.0.1:8800' },
      ],
    }],
  },
]

function Field({ field, form, setForm, showSecret, setShowSecret }: any) {
  const { key, label, placeholder, secret, type = 'text', min, max } = field
  const options = field.options || SELECT_FIELDS[key]
  if (type === 'checkbox') {
    return (
      <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5 last:border-0">
        <label className="text-sm text-[var(--color-text-secondary)] font-medium">{label}</label>
        <div className="col-span-2">
          <label className="inline-flex items-center gap-2 text-sm text-[var(--color-text-secondary)] cursor-pointer">
            <input
              type="checkbox"
              checked={isTruthyConfigValue(form[key])}
              onChange={e => setForm((f: any) => ({ ...f, [key]: e.target.checked ? 'true' : 'false' }))}
              className="checkbox-accent"
            />
            <span>{isTruthyConfigValue(form[key]) ? '已开启' : '已关闭'}</span>
          </label>
        </div>
      </div>
    )
  }
  if (type === 'textarea') {
    return (
      <div className="grid grid-cols-3 gap-4 py-3 border-b border-white/5 last:border-0">
        <label className="pt-2 text-sm text-[var(--color-text-secondary)] font-medium">{label}</label>
        <div className="col-span-2">
          <textarea
            value={form[key] || ''}
            onChange={e => setForm((f: any) => ({ ...f, [key]: e.target.value }))}
            placeholder={placeholder}
            rows={4}
            className="control-surface resize-y"
          />
        </div>
      </div>
    )
  }
  return (
    <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5 last:border-0">
      <label className="text-sm text-[var(--color-text-secondary)] font-medium">{label}</label>
      <div className="col-span-2 relative">
        {options ? (
          <select
            value={form[key] || options[0].value}
            onChange={e => setForm((f: any) => ({ ...f, [key]: e.target.value }))}
            className="control-surface appearance-none"
          >
            {options.map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        ) : (
          <>
            <input
              type={secret && !showSecret[key] ? 'password' : type}
              value={form[key] || ''}
              onChange={e => setForm((f: any) => ({ ...f, [key]: e.target.value }))}
              placeholder={placeholder}
              min={type === 'number' ? min : undefined}
              max={type === 'number' ? max : undefined}
              className="control-surface pr-10"
            />
            {secret && (
              <button
                onClick={() => setShowSecret((s: any) => ({ ...s, [key]: !s[key] }))}
                className="absolute right-3 top-2.5 text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)]"
              >
                {showSecret[key] ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            )}
          </>
        )}
      </div>
    </div>
  )
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
        <select value={value || ''} onChange={e => onChange(e.target.value)} disabled={disabled || loading} className="control-surface appearance-none disabled:opacity-70">
          <option value="">选择 Google/Gmail 商品</option>
          {products.map((item: any) => {
            const service = String(item.service || item._id || '')
            const label = `${service} ? ${item.name || item.category || 'Google ??'} ? $${item.rate || '-'} ? ?? ${item.stock ?? '-'}`
            return <option key={service} value={service}>{label}</option>
          })}
        </select>
        <button type="button" onClick={loadProducts} disabled={disabled || loading} className="table-action-btn whitespace-nowrap">{loading ? '加载中...' : '刷新'}</button>
      </div>
      <input value={value || ''} onChange={e => onChange(e.target.value)} disabled={disabled} placeholder="也可以手动填写 service id" className="control-surface disabled:opacity-70" />
      {error ? <div className="text-xs text-red-300">{error}</div> : null}
      <div className="text-[11px] text-[var(--color-text-muted)]">自动筛选名称包含 Google、Gmail、Workspace、EDU 的商品。</div>
    </div>
  )
}

function ProviderField({ field, value, onChange, showSecret, setShowSecret, secretKey, disabled = false }: any) {
  const { label, placeholder, secret, type = 'text', min, max } = field
  const options = field.options || []
  if (type === 'checkbox') {
    return (
      <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5 last:border-0">
        <label className="text-sm text-[var(--color-text-secondary)] font-medium">{label}</label>
        <div className="col-span-2">
          <label className="inline-flex items-center gap-2 text-sm text-[var(--color-text-secondary)]">
            <input type="checkbox" checked={isTruthyConfigValue(value)} onChange={e => onChange(e.target.checked ? 'true' : 'false')} disabled={disabled} className="checkbox-accent" />
            <span>{isTruthyConfigValue(value) ? '已开启' : '已关闭'}</span>
          </label>
        </div>
      </div>
    )
  }
  return (
    <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5 last:border-0">
      <label className="text-sm text-[var(--color-text-secondary)] font-medium">{label}</label>
      <div className="col-span-2 relative">
        {type === 'hstockplus_product_select' ? (
          <HStockPlusProductSelect value={value} onChange={onChange} disabled={disabled} />
        ) : options.length > 0 ? (
          <select
            value={value || options[0].value}
            onChange={e => onChange(e.target.value)}
            disabled={disabled}
            className="control-surface appearance-none disabled:opacity-70"
          >
            {options.map((option: any) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        ) : (
          <>
            <input
              type={secret && !showSecret[secretKey] ? 'password' : type}
              value={value || ''}
              onChange={e => onChange(e.target.value)}
              disabled={disabled}
              placeholder={placeholder}
              min={type === 'number' ? min : undefined}
              max={type === 'number' ? max : undefined}
              className="control-surface pr-10 disabled:opacity-70"
            />
            {secret && (
              <button
                onClick={() => setShowSecret((s: any) => ({ ...s, [secretKey]: !s[secretKey] }))}
                disabled={disabled}
                className="absolute right-3 top-2.5 text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)]"
              >
                {showSecret[secretKey] ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            )}
          </>
        )}
      </div>
    </div>
  )
}

type MailboxInventoryItem = {
  id: number
  provider_key: string
  email: string
  token_preview: string
  status: string
  note: string
  last_error: string
  last_task_id?: string
  last_platform?: string
  updated_at?: string | null
  metadata?: Record<string, unknown>
}

type MailboxInventoryRow =
  | { kind: 'plain'; item: MailboxInventoryItem }
  | { kind: 'outlook_group'; item: MailboxInventoryItem; children: MailboxInventoryItem[] }
  | { kind: 'outlook_alias_orphan'; item: MailboxInventoryItem }

const INVENTORY_STATUS_VARIANT: Record<string, any> = {
  unused: 'secondary',
  running: 'warning',
  registered: 'success',
  blacklisted: 'danger',
  oauth_pending: 'warning',
  register_failed: 'danger',
  existing_account: 'warning',
  existing_suspected: 'warning',
}

const INVENTORY_STATUS_LABEL: Record<string, string> = {
  unused: '未使用',
  running: '运行中',
  registered: '已注册',
  blacklisted: '已拉黑',
  oauth_pending: '待 OAuth',
  register_failed: '注册失败',
  existing_account: '已存在账号',
  existing_suspected: '疑似已注册',
}

const MAILBOX_INVENTORY_CONFIG: Record<string, { title: string; helper: string; placeholder: string; importButton: string; emptyText: string }> = {
  luckmail: {
    title: '已购邮箱池',
    helper: '每行一条 `email----token`。当默认邮箱 Provider 设为 LuckMail 且注册任务未填写批量种子文本时，后端会优先从这里领取 `unused` 邮箱，并把注册结果回写到状态列。验证码超时会自动拉黑；注册成功但未达到 4 次复用上限时，会自动回到 `unused` 继续复用。',
    placeholder: 'email----token\nexample@hotmail.com----tok_xxx',
    importButton: '导入已购邮箱',
    emptyText: '当前还没有已购邮箱',
  },
  outlook_token: {
    title: 'Outlook 令牌池',
    helper: '每行一条 `email----password----client_id----refresh_token`。注册任务未填写批量种子文本时，后端会优先从这里领取 `unused` 邮箱；收件时会自动刷新 refresh token，并把新令牌回写到邮箱池。注册成功后邮箱会自动回池，可继续用于其他网站。',
    placeholder: 'email----password----client_id----refresh_token\ndemo@outlook.com----mail-pass----client-id----0.ABC',
    importButton: '导入 Outlook 令牌',
    emptyText: '当前还没有 Outlook 令牌邮箱',
  },
}

function formatInventoryTime(value?: string | null) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${date.getMonth() + 1}-${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

function getInventoryUsedPlatforms(metadata?: Record<string, unknown>) {
  const raw = metadata?.used_platforms
  if (!Array.isArray(raw)) return []
  return raw.filter(Boolean).map((item) => String(item))
}

function isOutlookAliasInventoryItem(item: MailboxInventoryItem) {
  const metadata = item.metadata || {}
  const source = String(metadata.source || '').trim().toLowerCase()
  const local = String(item.email || '').split('@')[0] || ''
  return item.provider_key === 'outlook_token'
    && (source === 'outlook_alias_auto'
      || Boolean(metadata.alias_parent_email || metadata.outlook_login_email)
      || local.includes('+'))
}

function normalizeInventoryEmailKey(value: string) {
  return String(value || '').trim().toLowerCase()
}

function getOutlookAliasParentEmail(item: MailboxInventoryItem) {
  const metadata = item.metadata || {}
  const directParent = String(metadata.alias_parent_email || metadata.outlook_login_email || '').trim()
  if (directParent) return directParent
  const email = String(item.email || '').trim()
  if (!email.includes('@')) return ''
  const [local, domain] = email.split('@', 2)
  if (!local.includes('+')) return ''
  return `${local.split('+', 1)[0]}@${domain}`
}

function getOutlookAliasParentKey(item: MailboxInventoryItem) {
  const parentEmail = getOutlookAliasParentEmail(item)
  return parentEmail ? normalizeInventoryEmailKey(parentEmail) : ''
}

function groupOutlookAliasInventoryItems(items: MailboxInventoryItem[]): MailboxInventoryRow[] {
  const parentKeySet = new Set<string>()
  for (const item of items) {
    if (!isOutlookAliasInventoryItem(item)) {
      parentKeySet.add(normalizeInventoryEmailKey(item.email))
    }
  }

  const groupedChildren = new Map<string, MailboxInventoryItem[]>()
  for (const item of items) {
    if (!isOutlookAliasInventoryItem(item)) continue
    const parentKey = getOutlookAliasParentKey(item)
    if (!parentKey || !parentKeySet.has(parentKey)) continue
    const current = groupedChildren.get(parentKey) || []
    current.push(item)
    groupedChildren.set(parentKey, current)
  }

  const emittedParents = new Set<string>()
  const rows: MailboxInventoryRow[] = []
  for (const item of items) {
    if (isOutlookAliasInventoryItem(item)) {
      const parentKey = getOutlookAliasParentKey(item)
      if (parentKey && parentKeySet.has(parentKey)) continue
      rows.push({ kind: 'outlook_alias_orphan', item })
      continue
    }

    const parentKey = normalizeInventoryEmailKey(item.email)
    if (emittedParents.has(parentKey)) continue
    emittedParents.add(parentKey)
    const children = groupedChildren.get(parentKey) || []
    rows.push(children.length > 0 ? { kind: 'outlook_group', item, children } : { kind: 'plain', item })
  }
  return rows
}

function MailboxInventoryPanel({ providerKey }: { providerKey: string }) {
  const inventoryConfig = MAILBOX_INVENTORY_CONFIG[providerKey] || MAILBOX_INVENTORY_CONFIG.luckmail
  const [items, setItems] = useState<MailboxInventoryItem[]>([])
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState(false)
  const [importing, setImporting] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [resetting, setResetting] = useState<Record<number, boolean>>({})
  const [text, setText] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const data = await apiFetch(`/mailbox-inventory?provider_key=${encodeURIComponent(providerKey)}`)
      setItems(Array.isArray(data?.items) ? data.items : [])
      setCounts(data?.counts || {})
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载邮箱池失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [providerKey])

  const importLines = async () => {
    const lines = text.split(/\r?\n/).map(item => item.trim()).filter(Boolean)
    if (lines.length === 0) {
      setError(`请先粘贴 ${inventoryConfig.placeholder.split('\n')[0]} 文本`)
      return
    }
    setImporting(true)
    setError('')
    setNotice('')
    try {
      const data = await apiFetch('/mailbox-inventory/import', {
        method: 'POST',
        body: JSON.stringify({
          provider_key: providerKey,
          lines,
        }),
      })
      setNotice(`导入完成：新增 ${data?.created || 0}，更新 ${data?.updated || 0}，跳过 ${data?.skipped || 0}`)
      setText('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入邮箱池失败')
    } finally {
      setImporting(false)
    }
  }

  const resetItem = async (itemId: number) => {
    setResetting(current => ({ ...current, [itemId]: true }))
    setError('')
    try {
      await apiFetch(`/mailbox-inventory/${itemId}`, {
        method: 'PATCH',
        body: JSON.stringify({
          status: 'unused',
          last_error: '',
        }),
      })
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '重置邮箱状态失败')
    } finally {
      setResetting(current => ({ ...current, [itemId]: false }))
    }
  }

  const getResetActionLabel = (item: MailboxInventoryItem) => {
    if (resetting[item.id]) {
      return item.status === 'blacklisted' ? '拉回中...' : '重置中...'
    }
    return item.status === 'blacklisted' ? '从黑名单拉回' : '重置为未使用'
  }

  const exportLines = async () => {
    setExporting(true)
    setError('')
    try {
      const { blob, filename } = await apiDownload(`/mailbox-inventory/export?provider_key=${encodeURIComponent(providerKey)}`)
      triggerBrowserDownload(blob, filename)
      setNotice(`已导出 ${inventoryConfig.title}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '导出邮箱池失败')
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="mt-2 rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface-hover)]/40 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm font-semibold text-[var(--color-text)]">{inventoryConfig.title}</div>
          <div className="mt-1 text-xs leading-5 text-[var(--color-text-muted)]">
            {inventoryConfig.helper.split('`').map((chunk, index) => (
              index % 2 === 1 ? <code key={`${providerKey}-${index}`}>{chunk}</code> : <span key={`${providerKey}-${index}`}>{chunk}</span>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={exportLines} disabled={exporting || loading || items.length === 0}>
            <Download className="mr-1.5 h-3.5 w-3.5" />
            {exporting ? '导出中...' : '导出邮箱池'}
          </Button>
          <Button variant="outline" size="sm" onClick={load} disabled={loading}>
            <RefreshCw className={cn('mr-1.5 h-3.5 w-3.5', loading ? 'animate-spin' : '')} />
            刷新
          </Button>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {['unused', 'running', 'registered', 'blacklisted', 'existing_account', 'existing_suspected', 'oauth_pending', 'register_failed'].map((status) => (
          <Badge key={status} variant={INVENTORY_STATUS_VARIANT[status] || 'secondary'}>
            {INVENTORY_STATUS_LABEL[status] || status} {counts?.[status] || 0}
          </Badge>
        ))}
      </div>

      {notice ? (
        <div className="mt-3 rounded-xl border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
          {notice}
        </div>
      ) : null}
      {error ? (
        <div className="mt-3 rounded-xl border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          {error}
        </div>
      ) : null}

      <div className="mt-4 space-y-2">
        <label className="text-xs text-[var(--color-text-muted)]">批量导入</label>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={5}
          placeholder={inventoryConfig.placeholder}
          className="control-surface control-surface-mono resize-none"
        />
        <Button onClick={importLines} disabled={importing} size="sm">
          {importing ? '导入中...' : inventoryConfig.importButton}
        </Button>
      </div>

      <div className="mt-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
        <div className="glass-table-wrap workspace-table-scroll">
        <table className="workspace-table min-w-[1120px] w-full text-xs">
          <thead className="bg-[var(--color-surface)] text-[var(--color-text-muted)]">
            <tr>
              <th className="px-3 py-2 text-left">邮箱</th>
              <th className="px-3 py-2 text-left">Token</th>
              <th className="px-3 py-2 text-left">状态</th>
              <th className="px-3 py-2 text-left">已用平台</th>
              <th className="px-3 py-2 text-left">备注 / 错误</th>
              <th className="px-3 py-2 text-left">更新时间</th>
              <th className="px-3 py-2 text-left">操作</th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-[var(--color-text-muted)]">
                  {loading ? '加载中...' : inventoryConfig.emptyText}
                </td>
              </tr>
            ) : groupOutlookAliasInventoryItems(items).map((row) => {
              const item = row.item
              const usedPlatforms = getInventoryUsedPlatforms(item.metadata)
              const isAliasGroup = row.kind === 'outlook_group'
              const isAliasOrphan = row.kind === 'outlook_alias_orphan'
              return (
                <Fragment key={item.id}>
                  <tr className="border-t border-[var(--color-border)]/60">
                    <td className="px-3 py-2">
                      <div className="max-w-[220px] break-all text-[var(--color-text)]">{item.email}</div>
                      {isAliasGroup ? (
                        <div className="mt-1 flex flex-wrap items-center gap-1.5">
                          <Badge variant="warning">子邮箱组</Badge>
                          <span className="text-[11px] text-[var(--color-text-muted)]">子邮箱 {row.children.length} 个</span>
                        </div>
                      ) : isAliasOrphan ? (
                        <div className="mt-1 flex flex-wrap items-center gap-1.5">
                          <Badge variant="warning">子邮箱</Badge>
                          <span className="text-[11px] text-[var(--color-text-muted)]">父邮箱 {getOutlookAliasParentEmail(item) || '-'}</span>
                        </div>
                      ) : null}
                    </td>
                    <td className="px-3 py-2">
                      <div className="max-w-[220px] break-all font-mono text-[var(--color-text-secondary)]">{item.token_preview || '-'}</div>
                    </td>
                    <td className="px-3 py-2">
                      <Badge variant={INVENTORY_STATUS_VARIANT[item.status] || 'secondary'}>
                        {INVENTORY_STATUS_LABEL[item.status] || item.status}
                      </Badge>
                    </td>
                    <td className="px-3 py-2 text-[var(--color-text-secondary)]">
                      <div className="max-w-[220px] truncate">{usedPlatforms.length > 0 ? usedPlatforms.join(', ') : '-'}</div>
                    </td>
                    <td className="px-3 py-2 text-[var(--color-text-secondary)]">
                      <div className="max-w-[280px] truncate">{item.note || '-'}</div>
                      {item.last_error ? (
                        <div className="mt-1 max-w-[280px] truncate text-red-300">{item.last_error}</div>
                      ) : null}
                    </td>
                    <td className="px-3 py-2 text-[var(--color-text-secondary)] tabular-nums">{formatInventoryTime(item.updated_at)}</td>
                    <td className="px-3 py-2">
                      <button
                        type="button"
                        onClick={() => resetItem(item.id)}
                        disabled={!!resetting[item.id]}
                        className="table-action-btn"
                      >
                        {getResetActionLabel(item)}
                      </button>
                    </td>
                  </tr>
                  {isAliasGroup ? (
                    <tr className="border-t border-[var(--color-border)]/40 bg-[var(--color-surface-hover)]/40">
                      <td colSpan={7} className="px-3 py-2">
                        <div className="ml-3 rounded-lg border border-dashed border-[var(--color-border)]/80 bg-[var(--color-surface)] px-3 py-2">
                          <div className="text-[11px] font-medium uppercase tracking-[0.16em] text-[var(--color-text-muted)]">子邮箱列表</div>
                          <div className="mt-2 space-y-1.5">
                            {row.children.map((child) => {
                              const childPlatforms = getInventoryUsedPlatforms(child.metadata)
                              return (
                                <div key={child.id} className="flex flex-wrap items-center justify-between gap-3 rounded-md bg-[var(--color-surface-hover)]/60 px-3 py-2">
                                  <div className="min-w-0">
                                    <div className="break-all text-sm text-[var(--color-text)]">{child.email}</div>
                                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                                      <Badge variant={INVENTORY_STATUS_VARIANT[child.status] || 'secondary'}>
                                        {INVENTORY_STATUS_LABEL[child.status] || child.status}
                                      </Badge>
                                      <span className="text-[11px] text-[var(--color-text-muted)]">{childPlatforms.length > 0 ? childPlatforms.join(', ') : '-'}</span>
                                    </div>
                                  </div>
                                  <button
                                    type="button"
                                    onClick={() => resetItem(child.id)}
                                    disabled={!!resetting[child.id]}
                                    className="table-action-btn"
                                  >
                                    {getResetActionLabel(child)}
                                  </button>
                                </div>
                              )
                            })}
                          </div>
                        </div>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              )
            })}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  )
}

function ProviderDetailModal({
  title,
  item,
  readOnly,
  saving,
  saved,
  showSecret,
  setShowSecret,
  onClose,
  onEdit,
  onChangeName,
  onChangeAuthMode,
  onChangeField,
  onSave,
}: any) {
  const isLuckMailMailbox = item.provider_type === 'mailbox' && item.provider_key === 'luckmail'
  const luckMailAliasFields = [
    { key: 'sub_mail_mode', label: '子邮箱模式', options: SUB_MAIL_MODE_OPTIONS },
    { key: 'sub_mail_length', label: '子邮箱长度', placeholder: '4', type: 'number', min: 1, max: 16 },
  ]

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-md overflow-y-auto" style={{ maxHeight: '90vh' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--color-border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--color-text)]">{title}</h2>
            <p className="text-xs text-[var(--color-text-muted)] mt-0.5">{item.display_name || item.catalog_label} · {item.provider_key}</p>
          </div>
          <button onClick={onClose} className="text-[var(--color-text-muted)] hover:text-[var(--color-text)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-6 py-4 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-full border border-[var(--color-border)] bg-[var(--color-surface-hover)] px-2 py-0.5 text-[11px] text-[var(--color-text-secondary)]">
              {item.auth_modes.find((mode: any) => mode.value === item.auth_mode)?.label || item.auth_mode || '未设置认证方式'}
            </span>
            {item.is_default ? (
              <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[11px] text-emerald-300">默认 Provider</span>
            ) : null}
          </div>
          {item.description ? (
            <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-hover)] px-3 py-2 text-xs text-[var(--color-text-secondary)]">
              {item.description}
            </div>
          ) : null}
          <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5">
            <label className="text-sm text-[var(--color-text-secondary)] font-medium">配置名称</label>
            <div className="col-span-2">
              <input
                type="text"
                value={item.display_name || ''}
                onChange={e => onChangeName(e.target.value)}
                disabled={readOnly}
                placeholder={item.catalog_label}
                className="control-surface disabled:opacity-70"
              />
            </div>
          </div>
          {item.auth_modes?.length > 0 && (
            <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5">
              <label className="text-sm text-[var(--color-text-secondary)] font-medium">认证方式</label>
              <div className="col-span-2">
                <select
                  value={item.auth_mode}
                  onChange={e => onChangeAuthMode(e.target.value)}
                  disabled={readOnly}
                  className="control-surface appearance-none disabled:opacity-70"
                >
                  {item.auth_modes.map((mode: any) => <option key={mode.value} value={mode.value}>{mode.label}</option>)}
                </select>
              </div>
            </div>
          )}
          {item.fields.filter((field: any) => field.category !== 'task').length === 0 && !isLuckMailMailbox ? (
            <div className="text-sm text-[var(--color-text-muted)] py-3">这个 provider 当前无需额外配置。</div>
          ) : item.fields.filter((field: any) => field.category !== 'task').map((field: any) => (
            <ProviderField
              key={field.key}
              field={field}
              value={field.category === 'auth' ? item.auth?.[field.key] : item.config?.[field.key]}
              onChange={(value: string) => onChangeField(field, value)}
              showSecret={showSecret}
              setShowSecret={setShowSecret}
              secretKey={String(item.provider_key || '') + ':' + String(field.key || '')}
              disabled={readOnly}
            />
          ))}
          {item.fields.some((field: any) => field.category === 'task') ? (
            <div className="rounded-xl border border-sky-500/20 bg-sky-500/10 px-3 py-2 text-xs leading-5 text-[var(--color-text-secondary)]">
              项目 ID、通道 ID、运营商、地区范围、指定手机号等属于单次注册任务参数，请在注册页的“手机号验证”面板填写；这里仅保存可复用的账号和 API 连接配置。
            </div>
          ) : null}
          {isLuckMailMailbox ? (
            <>
              <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs leading-5 text-[var(--color-text-secondary)]">
                这里配置的是 LuckMail 默认别名策略。自动注册弹窗会先读取这里的值，你也可以在弹窗里对单次任务临时覆盖。
                当邮箱命中"已注册 / 疑似已注册"时，后端会按这里的模式把邮箱改成 <code>原邮箱+</code> 或 <code>原邮箱.</code> 再试一轮。
              </div>
              {luckMailAliasFields.map((field) => (
                <ProviderField
                  key={field.key}
                  field={field}
                  value={field.key === 'sub_mail_mode'
                    ? normalizeSubMailMode(item.config?.[field.key])
                    : String(normalizeSubMailLength(item.config?.[field.key]))}
                  onChange={(value: string) => onChangeField({ key: field.key, category: 'config' }, value)}
                  showSecret={showSecret}
                  setShowSecret={setShowSecret}
                  secretKey={`${item.provider_key}:${field.key}`}
                  disabled={readOnly}
                />
              ))}
            </>
          ) : null}
          {isLuckMailMailbox ? (
            <MailboxInventoryPanel providerKey={item.provider_key} />
          ) : null}
        </div>
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--color-border)]">
          {readOnly ? (
            <>
              <Button onClick={onEdit} className="flex-1">切换到编辑</Button>
              <Button variant="outline" onClick={onClose} className="flex-1">关闭</Button>
            </>
          ) : (
            <>
              <Button onClick={onSave} disabled={saving} className="flex-1">
                <Save className="h-4 w-4 mr-2" />
                {saved ? '已保存 ✓' : saving ? '保存中...' : '保存'}
              </Button>
              <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function AddProviderModal({
  title,
  providerType,
  providers,
  selectedKey,
  creating,
  onSelect,
  onClose,
  onCreate,
}: any) {
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--color-border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--color-text)]">{title}</h2>
            <p className="text-xs text-[var(--color-text-muted)] mt-0.5">{providerType === 'mailbox' ? '从邮箱 provider catalog 中选择' : '从验证 provider catalog 中选择'}</p>
          </div>
          <button onClick={onClose} className="text-[var(--color-text-muted)] hover:text-[var(--color-text)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-6 py-4">
          {providers.length === 0 ? (
            <div className="empty-state-panel">
              当前可新增的 provider 已全部加入列表。
            </div>
          ) : (
            <div className="space-y-3">
              <label className="block text-sm text-[var(--color-text-secondary)]">选择 Provider</label>
              <select
                value={selectedKey}
                onChange={e => onSelect(e.target.value)}
                className="control-surface appearance-none"
              >
                {providers.map((provider: ProviderOption) => (
                  <option key={provider.value} value={provider.value}>{provider.label}</option>
                ))}
              </select>
              {providers.find((provider: ProviderOption) => provider.value === selectedKey)?.description ? (
                <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-hover)] px-3 py-2 text-xs text-[var(--color-text-secondary)]">
                  {providers.find((provider: ProviderOption) => provider.value === selectedKey)?.description}
                </div>
              ) : null}
            </div>
          )}
        </div>
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--color-border)]">
          <Button
            onClick={() => onCreate(selectedKey)}
            disabled={providers.length === 0 || !selectedKey || creating}
            className="flex-1"
          >
            <Plus className="h-4 w-4 mr-2" />
            {creating ? '新增中...' : '新增'}
          </Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

function CreateProviderDefinitionModal({
  title,
  providerType,
  drivers,
  form,
  creating,
  showSecret,
  setShowSecret,
  onChange,
  onClose,
  onCreate,
}: any) {
  const currentDriver = drivers.find((item: ProviderDriver) => item.driver_type === form.driver_type) || null
  const currentAuthModes = currentDriver?.auth_modes || []
  const currentFields = currentDriver?.fields || []

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-md overflow-y-auto" style={{ maxHeight: '90vh' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--color-border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--color-text)]">{title}</h2>
            <p className="text-xs text-[var(--color-text-muted)] mt-0.5">新增一个动态 provider definition，并同时创建首个可用配置。</p>
          </div>
          <button onClick={onClose} className="text-[var(--color-text-muted)] hover:text-[var(--color-text)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-6 py-4 space-y-3">
          <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5">
            <label className="text-sm text-[var(--color-text-secondary)] font-medium">Provider 名称</label>
            <div className="col-span-2">
              <input value={form.label} onChange={e => onChange('label', e.target.value)} placeholder="My Mail Provider" className="control-surface" />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5">
            <label className="text-sm text-[var(--color-text-secondary)] font-medium">Provider Key</label>
            <div className="col-span-2">
              <input value={form.provider_key} onChange={e => onChange('provider_key', e.target.value)} placeholder="my_mail_provider" className="control-surface" />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5">
            <label className="text-sm text-[var(--color-text-secondary)] font-medium">描述</label>
            <div className="col-span-2">
              <input value={form.description} onChange={e => onChange('description', e.target.value)} placeholder="可选" className="control-surface" />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5">
            <label className="text-sm text-[var(--color-text-secondary)] font-medium">驱动族</label>
            <div className="col-span-2">
              <select value={form.driver_type} onChange={e => onChange('driver_type', e.target.value)} className="control-surface appearance-none">
                {drivers.map((driver: ProviderDriver) => (
                  <option key={driver.driver_type} value={driver.driver_type}>{driver.label}</option>
                ))}
              </select>
              {currentDriver?.description ? <p className="mt-2 text-xs text-[var(--color-text-muted)]">{currentDriver.description}</p> : null}
            </div>
          </div>
          {currentAuthModes.length > 0 && (
            <div className="grid grid-cols-3 gap-4 items-center py-3 border-b border-white/5">
              <label className="text-sm text-[var(--color-text-secondary)] font-medium">认证方式</label>
              <div className="col-span-2">
                <select value={form.auth_mode} onChange={e => onChange('auth_mode', e.target.value)} className="control-surface appearance-none">
                  {currentAuthModes.map((mode: any) => (
                    <option key={mode.value} value={mode.value}>{mode.label}</option>
                  ))}
                </select>
              </div>
            </div>
          )}
          {currentFields.filter((field: any) => field.category !== 'task').length === 0 ? (
            <div className="text-sm text-[var(--color-text-muted)] py-3">这个驱动族当前无需额外配置字段。</div>
          ) : currentFields.filter((field: any) => field.category !== 'task').map((field: any) => (
            <ProviderField
              key={field.key}
              field={field}
              value={field.category === 'auth' ? form.auth[field.key] : form.config[field.key]}
              onChange={(value: string) => {
                if (field.category === 'auth') {
                  onChange('auth', { ...form.auth, [field.key]: value })
                } else {
                  onChange('config', { ...form.config, [field.key]: value })
                }
              }}
              showSecret={showSecret}
              setShowSecret={setShowSecret}
              secretKey={'create:' + String(providerType || '') + ':' + String(field.key || '')}
            />
          ))}
          {currentFields.some((field: any) => field.category === 'task') ? (
            <div className="rounded-xl border border-sky-500/20 bg-sky-500/10 px-3 py-2 text-xs leading-5 text-[var(--color-text-secondary)]">
              此驱动包含任务级参数。创建来源时不会保存这些字段，注册页会按不同目标平台单独填写。
            </div>
          ) : null}
        </div>
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--color-border)]">
          <Button onClick={onCreate} disabled={creating} className="flex-1">
            <Plus className="h-4 w-4 mr-2" />
            {creating ? '创建中...' : '创建并启用'}
          </Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

export default function Settings() {
  const [activeTab, setActiveTab] = useState('register')
  const [form, setForm] = useState<Record<string, string>>({})
  const [resinCheckPlatform, setResinCheckPlatform] = useState('')
  const [resinChecking, setResinChecking] = useState(false)
  const [resinCheckResult, setResinCheckResult] = useState<any | null>(null)
  const [resinCheckError, setResinCheckError] = useState('')
  const [configOptions, setConfigOptions] = useState<ConfigOptionsResponse>({ mailbox_providers: [], captcha_providers: [], phone_providers: [], mailbox_drivers: [], captcha_drivers: [], phone_drivers: [], captcha_policy: {} })
  const [providerSettings, setProviderSettings] = useState<{ mailbox: ProviderSetting[]; captcha: ProviderSetting[]; phone: ProviderSetting[] }>({ mailbox: [], captcha: [], phone: [] })
  const [newProviderKey, setNewProviderKey] = useState<{ mailbox: string; captcha: string; phone: string }>({ mailbox: '', captcha: '', phone: '' })
  const [providerDialog, setProviderDialog] = useState<{ providerType: ProviderType | null; providerKey: string; readOnly: boolean }>({ providerType: null, providerKey: '', readOnly: false })
  const [providerAddDialog, setProviderAddDialog] = useState<ProviderType | null>(null)
  const [providerCreateDialog, setProviderCreateDialog] = useState<ProviderType | null>(null)
  const [providerDefinitionCreating, setProviderDefinitionCreating] = useState<Record<string, boolean>>({})
  const [providerDefinitionForm, setProviderDefinitionForm] = useState<Record<ProviderType, any>>({
    mailbox: { provider_key: '', label: '', description: '', driver_type: '', auth_mode: '', config: {}, auth: {} },
    captcha: { provider_key: '', label: '', description: '', driver_type: '', auth_mode: '', config: {}, auth: {} },
    phone: { provider_key: '', label: '', description: '', driver_type: '', auth_mode: '', config: {}, auth: {} },
  })
  const [optionsError, setOptionsError] = useState('')
  const [providerNotice, setProviderNotice] = useState<{ mailbox: string; captcha: string; phone: string }>({ mailbox: '', captcha: '', phone: '' })
  const [providerError, setProviderError] = useState<{ mailbox: string; captcha: string; phone: string }>({ mailbox: '', captcha: '', phone: '' })
  const [showSecret, setShowSecret] = useState<Record<string, boolean>>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [providerSaving, setProviderSaving] = useState<Record<string, boolean>>({})
  const [providerSaved, setProviderSaved] = useState<Record<string, boolean>>({})
  const [providerDeleting, setProviderDeleting] = useState<Record<string, boolean>>({})
  const [providerCreating, setProviderCreating] = useState<Record<string, boolean>>({})
  const [solverRunning, setSolverRunning] = useState<boolean | null>(null)

  const applyResinPlatformTemplate = (template: string) => {
    setForm(current => ({
      ...current,
      resin_platform_map: template,
      resin_default_platform: String(current.resin_default_platform || '').trim() || 'Default',
    }))
  }

  const loadConfigData = async () => {
    const [cfg, options] = await Promise.all([
      getConfig().catch(() => ({})),
      getConfigOptions().catch(() => null),
    ])
    setForm(cfg)
    if (options) {
      setConfigOptions(options)
      const nextMailbox = options.mailbox_settings || []
      const nextCaptcha = options.captcha_settings || []
      const nextPhone = options.phone_settings || []
      setProviderSettings({
        mailbox: nextMailbox,
        captcha: nextCaptcha,
        phone: nextPhone,
      })
      setOptionsError('')
    } else {
      setConfigOptions({ mailbox_providers: [], captcha_providers: [], phone_providers: [], mailbox_drivers: [], captcha_drivers: [], phone_drivers: [], mailbox_settings: [], captcha_settings: [], phone_settings: [], captcha_policy: {} })
      setProviderSettings({ mailbox: [], captcha: [], phone: [] })
      setOptionsError('未加载到 provider 元数据。请重启后端后刷新页面。')
    }
  }

  useEffect(() => {
    loadConfigData()
  }, [])

  const checkSolver = async () => {
    try { const d = await apiFetch('/solver/status'); setSolverRunning(d.running) }
    catch { setSolverRunning(false) }
  }
  const restartSolver = async () => {
    await apiFetch('/solver/restart', { method: 'POST' })
    setSolverRunning(null)
    setTimeout(checkSolver, 4000)
  }
  useEffect(() => { checkSolver() }, [])

  const save = async () => {
    setSaving(true)
    try {
      await apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data: form }) })
      invalidateConfigCache()
      setSaved(true); setTimeout(() => setSaved(false), 2000)
    } finally { setSaving(false) }
  }

  const checkResin = async () => {
    setResinChecking(true)
    setResinCheckError('')
    try {
      const result = await apiFetch('/config/resin/check', {
        method: 'POST',
        body: JSON.stringify({
          data: form,
          task_platform: resinCheckPlatform,
        }),
      })
      setResinCheckResult(result)
    } catch (error) {
      setResinCheckResult(null)
      setResinCheckError(error instanceof Error ? error.message : 'Resin 连通性测试失败')
    } finally {
      setResinChecking(false)
    }
  }

  const tab = TABS.find(t => t.id === activeTab) ?? TABS[0]
  const sections = tab.sections ?? []
  const mailboxCatalog = configOptions.mailbox_providers || []
  const captchaCatalog = configOptions.captcha_providers || []
  const phoneCatalog = configOptions.phone_providers || []
  const mailboxDrivers = configOptions.mailbox_drivers || []
  const captchaDrivers = configOptions.captcha_drivers || []
  const phoneDrivers = configOptions.phone_drivers || []
  const unusedMailboxProviders = mailboxCatalog.filter(item => !providerSettings.mailbox.some(setting => setting.provider_key === item.value))
  const unusedCaptchaProviders = captchaCatalog.filter(item => !providerSettings.captcha.some(setting => setting.provider_key === item.value))
  const unusedPhoneProviders = phoneCatalog.filter(item => !providerSettings.phone.some(setting => setting.provider_key === item.value))
  const getProviderCatalog = (providerType: ProviderType) => providerType === 'mailbox' ? mailboxCatalog : providerType === 'captcha' ? captchaCatalog : phoneCatalog
  const resinMapPreview = String(form.resin_platform_map || '').trim() || '# 这里会显示 resin_platform_map 当前内容'

  useEffect(() => {
    setNewProviderKey(current => {
      const nextMailbox = unusedMailboxProviders.some(item => item.value === current.mailbox) ? current.mailbox : (unusedMailboxProviders[0]?.value || '')
      const nextCaptcha = unusedCaptchaProviders.some(item => item.value === current.captcha) ? current.captcha : (unusedCaptchaProviders[0]?.value || '')
      const nextPhone = unusedPhoneProviders.some(item => item.value === current.phone) ? current.phone : (unusedPhoneProviders[0]?.value || '')
      if (current.mailbox === nextMailbox && current.captcha === nextCaptcha && current.phone === nextPhone) {
        return current
      }
      return {
        mailbox: nextMailbox,
        captcha: nextCaptcha,
        phone: nextPhone,
      }
    })
  }, [mailboxCatalog, captchaCatalog, phoneCatalog, providerSettings.mailbox, providerSettings.captcha, providerSettings.phone])

  useEffect(() => {
    setProviderDefinitionForm(current => {
      const next = { ...current }
      const mailboxDriver = mailboxDrivers.find(item => item.driver_type === current.mailbox.driver_type) || mailboxDrivers[0] || null
      const captchaDriver = captchaDrivers.find(item => item.driver_type === current.captcha.driver_type) || captchaDrivers[0] || null
      const phoneDriver = phoneDrivers.find(item => item.driver_type === current.phone.driver_type) || phoneDrivers[0] || null
      next.mailbox = {
        ...next.mailbox,
        driver_type: mailboxDriver?.driver_type || '',
        auth_mode: mailboxDriver?.auth_modes?.some(mode => mode.value === next.mailbox.auth_mode)
          ? next.mailbox.auth_mode
          : (mailboxDriver?.default_auth_mode || mailboxDriver?.auth_modes?.[0]?.value || ''),
      }
      next.captcha = {
        ...next.captcha,
        driver_type: captchaDriver?.driver_type || '',
        auth_mode: captchaDriver?.auth_modes?.some(mode => mode.value === next.captcha.auth_mode)
          ? next.captcha.auth_mode
          : (captchaDriver?.default_auth_mode || captchaDriver?.auth_modes?.[0]?.value || ''),
      }
      next.phone = {
        ...next.phone,
        driver_type: phoneDriver?.driver_type || '',
        auth_mode: phoneDriver?.auth_modes?.some(mode => mode.value === next.phone.auth_mode)
          ? next.phone.auth_mode
          : (phoneDriver?.default_auth_mode || phoneDriver?.auth_modes?.[0]?.value || ''),
      }
      return next
    })
  }, [mailboxDrivers, captchaDrivers, phoneDrivers])

  const getErrorMessage = (error: unknown, fallback: string) => {
    if (error instanceof Error && error.message) {
      return error.message
    }
    return fallback
  }

  const updateProviderDefinitionForm = (providerType: ProviderType, key: string, value: any) => {
    setProviderDefinitionForm(current => {
      const next = {
        ...current,
        [providerType]: {
          ...current[providerType],
          [key]: value,
        },
      }
      if (key === 'driver_type') {
        const drivers = providerType === 'mailbox' ? mailboxDrivers : providerType === 'captcha' ? captchaDrivers : phoneDrivers
        const driver = drivers.find(item => item.driver_type === value) || null
        next[providerType].auth_mode = driver?.default_auth_mode || driver?.auth_modes?.[0]?.value || ''
        next[providerType].config = {}
        next[providerType].auth = {}
      }
      return next
    })
  }

  const updateProviderSetting = (providerType: ProviderType, providerKey: string, updater: (item: ProviderSetting) => ProviderSetting) => {
    setProviderSettings(current => ({
      ...current,
      [providerType]: current[providerType].map(item => item.provider_key === providerKey ? updater(item) : item),
    }))
  }

  const updateProviderSettingField = (providerType: ProviderType, providerKey: string, field: any, value: string) => {
    updateProviderSetting(providerType, providerKey, item => {
      if (field.category === 'auth') {
        return { ...item, auth: { ...item.auth, [field.key]: value } }
      }
      return { ...item, config: { ...item.config, [field.key]: value } }
    })
  }

  const markProviderDefault = (providerType: ProviderType, providerKey: string) => {
    setProviderSettings(current => ({
      ...current,
      [providerType]: current[providerType].map(item => ({
        ...item,
        is_default: item.provider_key === providerKey,
      })),
    }))
  }

  const persistProviderDefault = async (providerType: ProviderType, item: ProviderSetting) => {
    markProviderDefault(providerType, item.provider_key)
    await saveProviderSetting(providerType, {
      ...item,
      is_default: true,
    })
  }

  const saveProviderSetting = async (providerType: ProviderType, item: ProviderSetting) => {
    const stateKey = `${providerType}:${item.provider_key}`
    setProviderSaving(current => ({ ...current, [stateKey]: true }))
    setProviderError(current => ({ ...current, [providerType]: '' }))
    try {
      await apiFetch('/provider-settings', {
        method: 'PUT',
        body: JSON.stringify({
          id: item.id || undefined,
          provider_type: providerType,
          provider_key: item.provider_key,
          display_name: item.display_name,
          auth_mode: item.auth_mode,
          enabled: item.enabled,
          is_default: item.is_default,
          config: item.config,
          auth: item.auth,
          metadata: item.metadata || {},
        }),
      })
      invalidateConfigOptionsCache()
      invalidateConfigCache()
      await loadConfigData()
      setProviderNotice(current => ({ ...current, [providerType]: `已保存 ${item.catalog_label || item.provider_key} 配置` }))
      setProviderSaved(current => ({ ...current, [stateKey]: true }))
      setTimeout(() => setProviderSaved(current => ({ ...current, [stateKey]: false })), 2000)
    } catch (error) {
      setProviderError(current => ({ ...current, [providerType]: getErrorMessage(error, '保存 provider 配置失败') }))
    } finally {
      setProviderSaving(current => ({ ...current, [stateKey]: false }))
    }
  }

  const createProviderSetting = async (providerType: ProviderType, providerKey: string) => {
    if (!providerKey) return
    const catalog = getProviderCatalog(providerType).find(item => item.value === providerKey)
    if (!catalog) return
    const existing = providerSettings[providerType].some(item => item.provider_key === providerKey)
    if (existing) {
      setProviderDialog({ providerType, providerKey, readOnly: false })
      return
    }
    const stateKey = `${providerType}:${providerKey}`
    setProviderCreating(current => ({ ...current, [stateKey]: true }))
    setProviderError(current => ({ ...current, [providerType]: '' }))
    try {
      await apiFetch('/provider-settings', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType,
          provider_key: providerKey,
          display_name: catalog.label,
          auth_mode: catalog.default_auth_mode || catalog.auth_modes?.[0]?.value || '',
          enabled: true,
          is_default: providerSettings[providerType].length === 0,
          config: {},
          auth: {},
          metadata: {},
        }),
      })
      invalidateConfigOptionsCache()
      await loadConfigData()
      setProviderNotice(current => ({ ...current, [providerType]: `已新增 ${catalog.label}` }))
      setProviderAddDialog(null)
    } catch (error) {
      setProviderError(current => ({ ...current, [providerType]: getErrorMessage(error, '新增 provider 失败') }))
    } finally {
      setProviderCreating(current => ({ ...current, [stateKey]: false }))
    }
  }

  const createProviderDefinitionAndSetting = async (providerType: ProviderType) => {
    const payload = providerDefinitionForm[providerType]
    const driverList = providerType === 'mailbox' ? mailboxDrivers : providerType === 'captcha' ? captchaDrivers : phoneDrivers
    const driver = driverList.find(item => item.driver_type === payload.driver_type) || null
    const definitionKey = `${providerType}:${payload.provider_key || 'new'}`
    if (!payload.provider_key || !payload.label || !payload.driver_type) {
      setProviderError(current => ({ ...current, [providerType]: '请先填写 Provider 名称、Key 和驱动族' }))
      return
    }
    setProviderDefinitionCreating(current => ({ ...current, [definitionKey]: true }))
    setProviderError(current => ({ ...current, [providerType]: '' }))
    try {
      await apiFetch('/provider-definitions', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType,
          provider_key: payload.provider_key,
          label: payload.label,
          description: payload.description || '',
          driver_type: payload.driver_type,
          enabled: true,
          default_auth_mode: payload.auth_mode || driver?.default_auth_mode || '',
          metadata: {},
        }),
      })
      await apiFetch('/provider-settings', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType,
          provider_key: payload.provider_key,
          display_name: payload.label,
          auth_mode: payload.auth_mode || driver?.default_auth_mode || '',
          enabled: true,
          is_default: providerSettings[providerType].length === 0,
          config: payload.config || {},
          auth: payload.auth || {},
          metadata: {},
        }),
      })
      invalidateConfigOptionsCache()
      await loadConfigData()
      setProviderNotice(current => ({ ...current, [providerType]: `已创建动态 provider ${payload.label}` }))
      setProviderCreateDialog(null)
      setProviderDefinitionForm(current => ({
        ...current,
        [providerType]: {
          provider_key: '',
          label: '',
          description: '',
          driver_type: driver?.driver_type || '',
          auth_mode: driver?.default_auth_mode || driver?.auth_modes?.[0]?.value || '',
          config: {},
          auth: {},
        },
      }))
    } catch (error) {
      setProviderError(current => ({ ...current, [providerType]: getErrorMessage(error, '创建动态 provider 失败') }))
    } finally {
      setProviderDefinitionCreating(current => ({ ...current, [definitionKey]: false }))
    }
  }

  const deleteProviderSetting = async (providerType: ProviderType, item: ProviderSetting) => {
    const stateKey = `${providerType}:${item.provider_key}`
    setProviderDeleting(current => ({ ...current, [stateKey]: true }))
    setProviderError(current => ({ ...current, [providerType]: '' }))
    try {
      await apiFetch(`/provider-settings/${item.id}`, { method: 'DELETE' })
      invalidateConfigOptionsCache()
      await loadConfigData()
      setProviderNotice(current => ({ ...current, [providerType]: `已删除 ${item.catalog_label || item.provider_key}` }))
    } catch (error) {
      setProviderError(current => ({ ...current, [providerType]: getErrorMessage(error, '删除 provider 失败') }))
    } finally {
      setProviderDeleting(current => ({ ...current, [stateKey]: false }))
    }
  }

  const dialogItem = providerDialog.providerType
    ? providerSettings[providerDialog.providerType].find(item => item.provider_key === providerDialog.providerKey) || null
    : null
  const openProviderDialog = (providerType: ProviderType, providerKey: string, readOnly: boolean) => {
    setProviderDialog({ providerType, providerKey, readOnly })
  }

  const mailboxCount = providerSettings.mailbox.length
  const captchaCount = providerSettings.captcha.length
  const phoneCount = providerSettings.phone.length
  const solverLabel = solverRunning === null ? '检测中' : solverRunning ? '运行中' : '未运行'
  const currentTabMeta = TABS.find(item => item.id === activeTab) ?? TABS[0]

  return (
    <div className="page-enter space-y-4">
      <Card className="overflow-hidden p-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-sm font-semibold text-[var(--color-text)]">配置</div>
            <Badge variant="default">{currentTabMeta.label}</Badge>
            <Badge variant={solverRunning ? 'success' : solverRunning === false ? 'danger' : 'secondary'}>{solverLabel}</Badge>
          </div>
        </div>
      </Card>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <SettingsMetric label="邮箱服务" value={mailboxCount} icon={Mail} />
        <SettingsMetric label="验证码服务" value={captchaCount} icon={Shield} />
        <SettingsMetric label="手机号服务" value={phoneCount} icon={Smartphone} />
        <SettingsMetric label="求解器" value={solverLabel} icon={Orbit} />
      </div>

      <div className="grid gap-4 xl:grid-cols-[240px_minmax(0,1fr)]">
        <Card className="h-fit bg-[var(--color-surface)] xl:sticky xl:top-4">
          <div className="space-y-4">
            <div>
              <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--color-text-muted)]">模块</div>
              <div className="mt-2 text-sm font-medium text-[var(--color-text)]">选择要操作的控制面板</div>
            </div>
            <div className="space-y-1.5">
              {TABS.map(({ id, label, icon: Icon }) => (
                <button
                  key={id}
                  onClick={() => setActiveTab(id)}
                  className={cn(
                    'w-full rounded-2xl border px-3 py-3 text-left transition-colors',
                    activeTab === id
                      ? 'border-[var(--color-accent)] bg-[var(--color-accent-muted)] text-[var(--color-text)]'
                      : 'border-transparent text-[var(--color-text-muted)] hover:border-[var(--color-border)] hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-text)]'
                  )}
                >
                  <div className="flex items-center gap-2.5">
                    <Icon className={cn('h-4 w-4', activeTab === id ? 'text-[var(--color-accent)]' : 'text-[var(--color-text-muted)]')} />
                    <span className="text-sm font-medium">{label}</span>
                  </div>
                </button>
              ))}
            </div>

            <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
              <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-[var(--color-text-muted)]">
                <Sparkles className="h-3.5 w-3.5" />
                求解器
              </div>
              <div className="mt-3 flex items-center gap-2">
                {solverRunning === null
                  ? <RefreshCw className="h-3.5 w-3.5 animate-spin text-[var(--color-text-muted)]" />
                  : solverRunning
                    ? <CheckCircle className="h-3.5 w-3.5 text-emerald-400" />
                    : <XCircle className="h-3.5 w-3.5 text-red-400" />}
                <span className={cn('text-sm font-medium', solverRunning ? 'text-emerald-400' : 'text-[var(--color-text-secondary)]')}>
                  {solverLabel}
                </span>
              </div>
              <Button variant="outline" size="sm" onClick={restartSolver} className="mt-4 w-full">
                <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
                重启 Solver
              </Button>
            </div>
          </div>
        </Card>

        <div className="space-y-4">
          {activeTab === 'platform_caps' ? (
            <PlatformCapsTab />
          ) : (
            <>
              {activeTab === 'register' && (
                <div className="rounded-lg border border-[var(--color-accent)] bg-[var(--color-accent-muted)] px-4 py-3 text-sm text-[var(--color-text-secondary)]">
                  普通使用者只需要理解两件事：注册身份选"系统邮箱"还是"第三方账号"，执行方式选"协议模式 / 后台浏览器自动 / 可视浏览器自动"。这里的配置只是设置默认值。
                </div>
              )}
              {activeTab === 'mailbox' && (
                <>
                  {optionsError && (
                    <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
                      {optionsError}
                    </div>
                  )}
                  {providerError.mailbox && (
                    <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
                      {providerError.mailbox}
                    </div>
                  )}
                  {providerNotice.mailbox && !providerError.mailbox && (
                    <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-200">
                      {providerNotice.mailbox}
                    </div>
                  )}
                  <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-4 py-3 text-sm text-[var(--color-text-secondary)]">
                    只有在注册身份选择"系统邮箱"时，才会使用这里的邮箱服务配置。列表行内可以直接查看详情、编辑、设默认和删除。
                  </div>
                  <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5 space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <h3 className="text-sm font-semibold text-[var(--color-text)]">邮箱 Provider 列表</h3>
                        <p className="text-xs text-[var(--color-text-muted)] mt-0.5">{providerSettings.mailbox.length} 个配置，勾选"启用"后注册任务会随机从已启用的邮箱来源中选取。</p>
                      </div>
                      <div className="flex items-center gap-3">
                        {unusedMailboxProviders.length === 0 ? (
                          <span className="text-xs text-[var(--color-text-muted)]">当前没有可新增的邮箱 provider</span>
                        ) : (
                          <span className="text-xs text-[var(--color-text-muted)]">还有 {unusedMailboxProviders.length} 个邮箱 provider 可新增</span>
                        )}
                        <Button size="sm" variant="outline" onClick={() => setProviderCreateDialog('mailbox')}>
                          <Plus className="h-3.5 w-3.5 mr-1" />
                          新建动态 Provider
                        </Button>
                        <Button size="sm" onClick={() => setProviderAddDialog('mailbox')}>
                          <Plus className="h-3.5 w-3.5 mr-1" />
                          新增 Provider
                        </Button>
                      </div>
                    </div>
                    {providerSettings.mailbox.length > 0 && (
                      <div className="flex items-center gap-2 text-xs text-[var(--color-text-secondary)]">
                        <span>已启用 <span className="font-medium text-emerald-400">{providerSettings.mailbox.filter(p => p.enabled !== false).length}</span> / {providerSettings.mailbox.length} 个邮箱来源</span>
                        <span className="text-[var(--color-text-muted)]">·</span>
                        <span className="text-[var(--color-text-muted)]">注册时从启用的来源中随机选取</span>
                      </div>
                    )}
                    {providerSettings.mailbox.length === 0 ? (
                      <div className="empty-state-panel">
                        当前没有邮箱 provider 配置，请先新增一个 provider。
                      </div>
                    ) : (
                      <div className="glass-table-wrap workspace-table-scroll rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
                        <table className="workspace-table w-full min-w-[1040px] text-sm">
                          <thead>
                            <tr className="border-b border-[var(--color-border)] bg-[var(--color-surface-hover)] text-xs text-[var(--color-text-muted)]">
                              <th className="px-4 py-3 text-left">启用</th>
                              <th className="px-4 py-3 text-left">名称</th>
                              <th className="px-4 py-3 text-left">Provider Key</th>
                              <th className="px-4 py-3 text-left">认证方式</th>
                              <th className="px-4 py-3 text-left">默认</th>
                              <th className="px-4 py-3 text-left">操作</th>
                            </tr>
                          </thead>
                          <tbody>
                            {providerSettings.mailbox.map(provider => {
                              const stateKey = `mailbox:${provider.provider_key}`
                              return (
                                <tr key={provider.provider_key} className="border-b border-[var(--color-border)]/50 hover:bg-[var(--color-surface-hover)]/60 transition-colors">
                                  <td className="px-4 py-3 whitespace-nowrap">
                                    <input
                                      type="checkbox"
                                      checked={provider.enabled !== false}
                                      onChange={() => {
                                        const toggled = { ...provider, enabled: provider.enabled === false }
                                        updateProviderSetting('mailbox', provider.provider_key, () => toggled)
                                        saveProviderSetting('mailbox', toggled)
                                      }}
                                      className="checkbox-accent"
                                    />
                                  </td>
                                  <td className={`px-4 py-3 whitespace-nowrap ${provider.enabled === false ? 'opacity-40' : ''}`}>
                                    <span className="font-medium text-[var(--color-text)]">{provider.display_name || provider.catalog_label}</span>
                                    {provider.display_name && provider.display_name !== provider.catalog_label ? (
                                      <span className="ml-2 text-[11px] text-[var(--color-text-muted)]">({provider.catalog_label})</span>
                                    ) : null}
                                  </td>
                                  <td className={`px-4 py-3 whitespace-nowrap text-[var(--color-text-secondary)] ${provider.enabled === false ? 'opacity-40' : ''}`}>{provider.provider_key}</td>
                                  <td className={`px-4 py-3 whitespace-nowrap text-[var(--color-text-secondary)] ${provider.enabled === false ? 'opacity-40' : ''}`}>{provider.auth_modes.find(mode => mode.value === provider.auth_mode)?.label || provider.auth_mode || '-'}</td>
                                  <td className="px-4 py-3 whitespace-nowrap">
                                    {provider.is_default ? <span className="inline-flex rounded-full bg-emerald-500/15 px-2 py-0.5 text-[11px] text-emerald-300">默认</span> : <span className="text-[var(--color-text-muted)]">-</span>}
                                  </td>
                                  <td className="px-4 py-3 whitespace-nowrap">
                                    <div className="flex flex-wrap items-center gap-2">
                                      <button onClick={() => openProviderDialog('mailbox', provider.provider_key, true)} className="table-action-btn">详情</button>
                                      <button onClick={() => openProviderDialog('mailbox', provider.provider_key, false)} className="table-action-btn">编辑</button>
                                      <button onClick={() => persistProviderDefault('mailbox', provider)} className="table-action-btn">
                                        {provider.is_default ? '当前默认' : '设默认'}
                                      </button>
                                      <button
                                        onClick={() => deleteProviderSetting('mailbox', provider)}
                                        disabled={providerDeleting[stateKey]}
                                        className="table-action-btn table-action-btn-danger"
                                      >
                                        {providerDeleting[stateKey] ? '删除中...' : '删除'}
                                      </button>
                                    </div>
                                  </td>
                                </tr>
                              )
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                </>
              )}
              {activeTab === 'captcha' && (
                <>
                  {optionsError && (
                    <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
                      {optionsError}
                    </div>
                  )}
                  {providerError.captcha && (
                    <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
                      {providerError.captcha}
                    </div>
                  )}
                  {providerNotice.captcha && !providerError.captcha && (
                    <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-200">
                      {providerNotice.captcha}
                    </div>
                  )}
                  <div className="rounded-lg border border-amber-500/20 bg-amber-500/10 px-4 py-3 text-sm text-[var(--color-text-secondary)]">
                    协议模式会按后端策略自动选择第一个已配置好的远程打码服务；浏览器模式固定走本地 Solver。列表行内可以直接查看详情、编辑、设默认、删除。
                  </div>
                  <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
                    <div className="mb-2">
                      <h3 className="text-sm font-semibold text-[var(--color-text)]">当前策略</h3>
                    </div>
                    <div className="text-sm text-[var(--color-text-secondary)]">{getCaptchaStrategyLabel('protocol', configOptions.captcha_policy, configOptions.captcha_providers)}</div>
                    <div className="text-sm text-[var(--color-text-secondary)] mt-2">{getCaptchaStrategyLabel('headless', configOptions.captcha_policy, configOptions.captcha_providers)}</div>
                  </div>
                  <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5 space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <h3 className="text-sm font-semibold text-[var(--color-text)]">验证 Provider 列表</h3>
                        <p className="text-xs text-[var(--color-text-muted)] mt-0.5">{providerSettings.captcha.length} 个配置，协议模式会依次读取这里的可用项。</p>
                      </div>
                      <div className="flex items-center gap-3">
                        {unusedCaptchaProviders.length === 0 ? (
                          <span className="text-xs text-[var(--color-text-muted)]">当前没有可新增的验证 provider</span>
                        ) : (
                          <span className="text-xs text-[var(--color-text-muted)]">还有 {unusedCaptchaProviders.length} 个验证 provider 可新增</span>
                        )}
                        <Button size="sm" variant="outline" onClick={() => setProviderCreateDialog('captcha')}>
                          <Plus className="h-3.5 w-3.5 mr-1" />
                          新建动态 Provider
                        </Button>
                        <Button size="sm" onClick={() => setProviderAddDialog('captcha')}>
                          <Plus className="h-3.5 w-3.5 mr-1" />
                          新增 Provider
                        </Button>
                      </div>
                    </div>
                    {providerSettings.captcha.length === 0 ? (
                      <div className="empty-state-panel">
                        当前没有验证 provider 配置，请先新增一个 provider。
                      </div>
                    ) : (
                      <div className="glass-table-wrap workspace-table-scroll rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
                        <table className="workspace-table w-full min-w-[1040px] text-sm">
                          <thead>
                            <tr className="border-b border-[var(--color-border)] bg-[var(--color-surface-hover)] text-xs text-[var(--color-text-muted)]">
                              <th className="px-4 py-3 text-left">名称</th>
                              <th className="px-4 py-3 text-left">Provider Key</th>
                              <th className="px-4 py-3 text-left">认证方式</th>
                              <th className="px-4 py-3 text-left">默认</th>
                              <th className="px-4 py-3 text-left">操作</th>
                            </tr>
                          </thead>
                          <tbody>
                            {providerSettings.captcha.map(provider => {
                              const stateKey = `captcha:${provider.provider_key}`
                              return (
                                <tr key={provider.provider_key} className="border-b border-[var(--color-border)]/50 hover:bg-[var(--color-surface-hover)]/60 transition-colors">
                                  <td className="px-4 py-3 whitespace-nowrap">
                                    <span className="font-medium text-[var(--color-text)]">{provider.display_name || provider.catalog_label}</span>
                                    {provider.display_name && provider.display_name !== provider.catalog_label ? (
                                      <span className="ml-2 text-[11px] text-[var(--color-text-muted)]">({provider.catalog_label})</span>
                                    ) : null}
                                  </td>
                                  <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-secondary)]">{provider.provider_key}</td>
                                  <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-secondary)]">{provider.auth_modes.find(mode => mode.value === provider.auth_mode)?.label || provider.auth_mode || '-'}</td>
                                  <td className="px-4 py-3 whitespace-nowrap">
                                    {provider.is_default ? <span className="inline-flex rounded-full bg-emerald-500/15 px-2 py-0.5 text-[11px] text-emerald-300">默认</span> : <span className="text-[var(--color-text-muted)]">-</span>}
                                  </td>
                                  <td className="px-4 py-3 whitespace-nowrap">
                                    <div className="flex flex-wrap items-center gap-2">
                                      <button onClick={() => openProviderDialog('captcha', provider.provider_key, true)} className="table-action-btn">详情</button>
                                      <button onClick={() => openProviderDialog('captcha', provider.provider_key, false)} className="table-action-btn">编辑</button>
                                      <button onClick={() => persistProviderDefault('captcha', provider)} className="table-action-btn">
                                        {provider.is_default ? '当前默认' : '设默认'}
                                      </button>
                                      <button
                                        onClick={() => deleteProviderSetting('captcha', provider)}
                                        disabled={providerDeleting[stateKey]}
                                        className="table-action-btn table-action-btn-danger"
                                      >
                                        {providerDeleting[stateKey] ? '删除中...' : '删除'}
                                      </button>
                                    </div>
                                  </td>
                                </tr>
                              )
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                </>
              )}
              {activeTab === 'phone' && (
                <>
                  {optionsError && (
                    <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
                      {optionsError}
                    </div>
                  )}
                  {providerError.phone && (
                    <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
                      {providerError.phone}
                    </div>
                  )}
                  {providerNotice.phone && !providerError.phone && (
                    <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-200">
                      {providerNotice.phone}
                    </div>
                  )}
                  <div className="rounded-lg border border-sky-500/20 bg-sky-500/10 px-4 py-3 text-sm text-[var(--color-text-secondary)]">
                    手机号服务用于需要短信二次验证的平台。注册任务开启“手机号验证”后，会按这里的默认来源取号并轮询短信验证码。
                  </div>
                  <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5 space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <h3 className="text-sm font-semibold text-[var(--color-text)]">手机号 Provider 列表</h3>
                        <p className="text-xs text-[var(--color-text-muted)] mt-0.5">{providerSettings.phone.length} 个配置，默认 Provider 会被注册任务使用。</p>
                      </div>
                      <div className="flex items-center gap-3">
                        {unusedPhoneProviders.length === 0 ? (
                          <span className="text-xs text-[var(--color-text-muted)]">当前没有可新增的手机号 provider</span>
                        ) : (
                          <span className="text-xs text-[var(--color-text-muted)]">还有 {unusedPhoneProviders.length} 个手机号 provider 可新增</span>
                        )}
                        <Button size="sm" variant="outline" onClick={() => setProviderCreateDialog('phone')}>
                          <Plus className="h-3.5 w-3.5 mr-1" />
                          新建动态 Provider
                        </Button>
                        <Button size="sm" onClick={() => setProviderAddDialog('phone')}>
                          <Plus className="h-3.5 w-3.5 mr-1" />
                          新增 Provider
                        </Button>
                      </div>
                    </div>
                    {providerSettings.phone.length === 0 ? (
                      <div className="empty-state-panel">
                        当前没有手机号 provider 配置，请先新增一个 provider。
                      </div>
                    ) : (
                      <div className="glass-table-wrap workspace-table-scroll rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
                        <table className="workspace-table w-full min-w-[1040px] text-sm">
                          <thead>
                            <tr className="border-b border-[var(--color-border)] bg-[var(--color-surface-hover)] text-xs text-[var(--color-text-muted)]">
                              <th className="px-4 py-3 text-left">名称</th>
                              <th className="px-4 py-3 text-left">Provider Key</th>
                              <th className="px-4 py-3 text-left">认证方式</th>
                              <th className="px-4 py-3 text-left">默认</th>
                              <th className="px-4 py-3 text-left">操作</th>
                            </tr>
                          </thead>
                          <tbody>
                            {providerSettings.phone.map(provider => {
                              const stateKey = 'phone:' + provider.provider_key
                              return (
                                <tr key={provider.provider_key} className="border-b border-[var(--color-border)]/50 hover:bg-[var(--color-surface-hover)]/60 transition-colors">
                                  <td className="px-4 py-3 whitespace-nowrap">
                                    <span className="font-medium text-[var(--color-text)]">{provider.display_name || provider.catalog_label}</span>
                                    {provider.display_name && provider.display_name !== provider.catalog_label ? (
                                      <span className="ml-2 text-[11px] text-[var(--color-text-muted)]">({provider.catalog_label})</span>
                                    ) : null}
                                  </td>
                                  <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-secondary)]">{provider.provider_key}</td>
                                  <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-secondary)]">{provider.auth_modes.find(mode => mode.value === provider.auth_mode)?.label || provider.auth_mode || '-'}</td>
                                  <td className="px-4 py-3 whitespace-nowrap">
                                    {provider.is_default ? <span className="inline-flex rounded-full bg-emerald-500/15 px-2 py-0.5 text-[11px] text-emerald-300">默认</span> : <span className="text-[var(--color-text-muted)]">-</span>}
                                  </td>
                                  <td className="px-4 py-3 whitespace-nowrap">
                                    <div className="flex flex-wrap items-center gap-2">
                                      <button onClick={() => openProviderDialog('phone', provider.provider_key, true)} className="table-action-btn">详情</button>
                                      <button onClick={() => openProviderDialog('phone', provider.provider_key, false)} className="table-action-btn">编辑</button>
                                      <button onClick={() => persistProviderDefault('phone', provider)} className="table-action-btn">
                                        {provider.is_default ? '当前默认' : '设默认'}
                                      </button>
                                      <button
                                        onClick={() => deleteProviderSetting('phone', provider)}
                                        disabled={providerDeleting[stateKey]}
                                        className="table-action-btn table-action-btn-danger"
                                      >
                                        {providerDeleting[stateKey] ? '删除中...' : '删除'}
                                      </button>
                                    </div>
                                  </td>
                                </tr>
                              )
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                </>
              )}
              {activeTab === 'proxy' && (
                <>
                  <div className="rounded-lg border border-sky-500/20 bg-sky-500/10 px-4 py-3 text-sm text-[var(--color-text-secondary)]">
                    平台映射按"任务平台=Resin Platform"逐行填写，例如 <code className="mx-1 rounded bg-black/20 px-1 py-0.5">venice=SeedancePool</code>。
                    保存后，所有留空 proxy 的注册任务会自动按任务平台挑选对应 Resin Platform。
                  </div>
                  <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5 space-y-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <h3 className="text-sm font-semibold text-[var(--color-text)]">平台映射模板</h3>
                        <p className="mt-1 text-xs text-[var(--color-text-muted)]">
                          快速生成 `resin_platform_map` 草稿，再按你的 Resin Platform 命名习惯微调即可。
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button size="sm" variant="outline" onClick={() => applyResinPlatformTemplate(RESIN_SAME_NAME_TEMPLATE)}>
                          填充同名模板
                        </Button>
                        <Button size="sm" onClick={() => applyResinPlatformTemplate(RESIN_EXAMPLE_TEMPLATE)}>
                          填充示例模板
                        </Button>
                      </div>
                    </div>
                    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_240px]">
                      <pre className="min-h-[148px] overflow-auto rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 text-xs leading-6 text-[var(--color-text-secondary)]">
                        {resinMapPreview}
                      </pre>
                      <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4 text-sm leading-6 text-[var(--color-text-secondary)]">
                        <div className="workspace-kicker">模板说明</div>
                        <div className="mt-2 space-y-1">
                          <div>• 同名模板：适合 Resin Platform 就按任务平台同名维护。</div>
                          <div>• 示例模板：适合先放业务池名，再手工改成你的实际池子。</div>
                          <div>• 会顺手补上 <code className="mx-1 rounded bg-black/20 px-1 py-0.5">Default</code> 作为兜底平台。</div>
                        </div>
                      </div>
                    </div>
                  </div>
                  <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5 space-y-4">
                    <div>
                      <h3 className="text-sm font-semibold text-[var(--color-text)]">测试 Resin 连通性</h3>
                      <p className="mt-1 text-xs text-[var(--color-text-muted)]">
                        使用当前表单里的配置即时探测，无需先保存。会走代理请求 `https://httpbin.org/ip`，并返回解析到的 Resin Platform 与出口 IP。
                      </p>
                    </div>
                    <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_auto]">
                      <input
                        value={resinCheckPlatform}
                        onChange={e => setResinCheckPlatform(e.target.value)}
                        placeholder="测试任务平台（可选），例如 venice"
                        className="control-surface"
                      />
                      <Button onClick={checkResin} disabled={resinChecking}>
                        {resinChecking ? '测试中...' : '测试 Resin 连通性'}
                      </Button>
                    </div>
                    {resinCheckError ? (
                      <div className="rounded-md border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
                        {resinCheckError}
                      </div>
                    ) : null}
                    {resinCheckResult ? (
                      <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4 text-sm text-[var(--color-text-secondary)]">
                        <div className="grid gap-2 md:grid-cols-2">
                          <div>结果：<span className={cn('font-medium', resinCheckResult.ok ? 'text-emerald-300' : 'text-red-300')}>{resinCheckResult.ok ? '成功' : '失败'}</span></div>
                          <div>来源：<span className="font-medium text-[var(--color-text)]">{resinCheckResult.source || '-'}</span></div>
                          <div>Resin Platform：<span className="font-medium text-[var(--color-text)]">{resinCheckResult.resolved_platform || '-'}</span></div>
                          <div>延迟：<span className="font-medium text-[var(--color-text)]">{resinCheckResult.latency_ms ?? '-'} ms</span></div>
                          <div className="md:col-span-2 break-all">代理 URL：<span className="font-medium text-[var(--color-text)]">{resinCheckResult.proxy_url || '-'}</span></div>
                          <div>状态码：<span className="font-medium text-[var(--color-text)]">{resinCheckResult.status_code ?? '-'}</span></div>
                          <div>出口 IP：<span className="font-medium text-[var(--color-text)]">{resinCheckResult.origin_ip || '-'}</span></div>
                        </div>
                        {resinCheckResult.error ? (
                          <div className="mt-3 rounded-[14px] border border-red-500/20 bg-red-500/10 px-3 py-2 text-red-300">
                            {resinCheckResult.error}
                          </div>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                </>
              )}
              {activeTab !== 'mailbox' && activeTab !== 'captcha' && activeTab !== 'phone' && sections.map(({ section, desc, items }) => (
                <div key={section} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
                  <div className="mb-4">
                    <h3 className="text-sm font-semibold text-[var(--color-text)]">{section}</h3>
                    {desc && <p className="text-xs text-[var(--color-text-muted)] mt-0.5">{desc}</p>}
                  </div>
                  {items.map((field: any) => (
                    <Field key={field.key} field={field} form={form} setForm={setForm}
                      showSecret={showSecret} setShowSecret={setShowSecret} />
                  ))}
                </div>
              ))}
              {activeTab !== 'mailbox' && activeTab !== 'captcha' && activeTab !== 'phone' && (
                <Button onClick={save} disabled={saving} className="w-full">
                  <Save className="h-4 w-4 mr-2" />
                  {saved ? '已保存 ✓' : saving ? '保存中...' : '保存配置'}
                </Button>
              )}
            </>
          )}
        </div>
      </div>
      {providerDialog.providerType && dialogItem && (
        <ProviderDetailModal
          title={providerDialog.providerType === 'mailbox' ? '邮箱 Provider 详情' : providerDialog.providerType === 'captcha' ? '验证 Provider 详情' : '手机号 Provider 详情'}
          item={dialogItem}
          readOnly={providerDialog.readOnly}
          saving={providerSaving[`${providerDialog.providerType}:${dialogItem.provider_key}`]}
          saved={providerSaved[`${providerDialog.providerType}:${dialogItem.provider_key}`]}
          showSecret={showSecret}
          setShowSecret={setShowSecret}
          onClose={() => setProviderDialog({ providerType: null, providerKey: '', readOnly: false })}
          onEdit={() => setProviderDialog(current => ({ ...current, readOnly: false }))}
          onChangeName={(value: string) => updateProviderSetting(providerDialog.providerType as ProviderType, dialogItem.provider_key, item => ({ ...item, display_name: value }))}
          onChangeAuthMode={(value: string) => updateProviderSetting(providerDialog.providerType as ProviderType, dialogItem.provider_key, item => ({ ...item, auth_mode: value }))}
          onChangeField={(field: any, value: string) => updateProviderSettingField(providerDialog.providerType as ProviderType, dialogItem.provider_key, field, value)}
          onSave={() => saveProviderSetting(providerDialog.providerType as ProviderType, dialogItem)}
        />
      )}
      {providerAddDialog && (
        <AddProviderModal
          title={providerAddDialog === 'mailbox' ? '新增邮箱 Provider' : providerAddDialog === 'captcha' ? '新增验证 Provider' : '新增手机号 Provider'}
          providerType={providerAddDialog}
          providers={providerAddDialog === 'mailbox' ? unusedMailboxProviders : providerAddDialog === 'captcha' ? unusedCaptchaProviders : unusedPhoneProviders}
          selectedKey={newProviderKey[providerAddDialog]}
          creating={Boolean(newProviderKey[providerAddDialog] && providerCreating[`${providerAddDialog}:${newProviderKey[providerAddDialog]}`])}
          onSelect={(value: string) => setNewProviderKey(current => ({ ...current, [providerAddDialog]: value }))}
          onClose={() => setProviderAddDialog(null)}
          onCreate={(providerKey: string) => createProviderSetting(providerAddDialog, providerKey)}
        />
      )}
      {providerCreateDialog && (
        <CreateProviderDefinitionModal
          title={providerCreateDialog === 'mailbox' ? '新建动态邮箱 Provider' : providerCreateDialog === 'captcha' ? '新建动态验证 Provider' : '新建动态手机号 Provider'}
          providerType={providerCreateDialog}
          drivers={providerCreateDialog === 'mailbox' ? mailboxDrivers : providerCreateDialog === 'captcha' ? captchaDrivers : phoneDrivers}
          form={providerDefinitionForm[providerCreateDialog]}
          creating={Boolean(providerDefinitionCreating[`${providerCreateDialog}:${providerDefinitionForm[providerCreateDialog].provider_key || 'new'}`])}
          showSecret={showSecret}
          setShowSecret={setShowSecret}
          onChange={(key: string, value: any) => updateProviderDefinitionForm(providerCreateDialog, key, value)}
          onClose={() => setProviderCreateDialog(null)}
          onCreate={() => createProviderDefinitionAndSetting(providerCreateDialog)}
        />
      )}
    </div>
  )
}
