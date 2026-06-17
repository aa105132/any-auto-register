from __future__ import annotations

from copy import deepcopy


CAPTCHA_POLICY = {
    "protocol_mode": "auto_first_configured_remote",
    "protocol_order": ["yescaptcha", "2captcha"],
    "browser_mode": "local_solver",
}


MAILBOX_DRIVER_TEMPLATES = [
    {
        "provider_type": "mailbox",
        "driver_type": "moemail_api",
        "label": "MoeMail API",
        "description": "MoeMail / sall.cc 协议族。优先复用你手动注册好的账号；未提供现成凭据时才自动注册 provider 账号。",
        "default_auth_mode": "username_password",
        "auth_modes": [
            {"value": "endpoint_only", "label": "仅接口地址"},
            {"value": "username_password", "label": "用户名密码"},
            {"value": "session_token", "label": "Session Token"},
            {"value": "hybrid", "label": "用户名密码 + Session Token"},
        ],
        "fields": [
            {"key": "moemail_api_url", "label": "API URL", "placeholder": "https://sall.cc", "category": "connection"},
            {"key": "moemail_username", "label": "用户名（手动注册）", "placeholder": "", "category": "auth"},
            {"key": "moemail_password", "label": "密码（手动注册）", "secret": True, "category": "auth"},
            {"key": "moemail_session_token", "label": "Session Token（可选）", "secret": True, "category": "auth"},
        ],
    },
    {
        "provider_type": "mailbox",
        "driver_type": "tempmail_lol_api",
        "label": "TempMail.lol API",
        "description": "tempmail.lol 协议族，自动创建匿名邮箱。",
        "default_auth_mode": "anonymous",
        "auth_modes": [
            {"value": "anonymous", "label": "匿名访问"},
        ],
        "fields": [],
    },
    {
        "provider_type": "mailbox",
        "driver_type": "duckmail_api",
        "label": "DuckMail API",
        "description": "DuckMail 协议族，自动创建 provider 账号并登录获取 token。",
        "default_auth_mode": "bearer_token",
        "auth_modes": [
            {"value": "bearer_token", "label": "Bearer Token"},
        ],
        "fields": [
            {"key": "duckmail_api_url", "label": "Web URL", "placeholder": "https://www.duckmail.sbs", "category": "connection"},
            {"key": "duckmail_provider_url", "label": "Provider URL", "placeholder": "https://api.duckmail.sbs", "category": "connection"},
            {"key": "duckmail_bearer", "label": "Bearer Token", "placeholder": "kevin273945", "secret": True, "category": "auth"},
        ],
    },
    {
        "provider_type": "mailbox",
        "driver_type": "laoudo_api",
        "label": "Laoudo API",
        "description": "Laoudo 固定邮箱协议族。",
        "default_auth_mode": "jwt_token",
        "auth_modes": [
            {"value": "jwt_token", "label": "JWT Token"},
        ],
        "fields": [
            {"key": "laoudo_email", "label": "邮箱地址", "placeholder": "xxx@laoudo.com", "category": "identity"},
            {"key": "laoudo_account_id", "label": "Account ID", "placeholder": "563", "category": "identity"},
            {"key": "laoudo_auth", "label": "JWT Token", "placeholder": "eyJ...", "secret": True, "category": "auth"},
        ],
    },
    {
        "provider_type": "mailbox",
        "driver_type": "freemail_api",
        "label": "Freemail API",
        "description": "Freemail / Cloudflare Worker 协议族。",
        "default_auth_mode": "admin_token",
        "auth_modes": [
            {"value": "admin_token", "label": "管理员令牌"},
            {"value": "username_password", "label": "用户名密码"},
            {"value": "hybrid", "label": "令牌 + 用户名密码"},
        ],
        "fields": [
            {"key": "freemail_api_url", "label": "API URL", "placeholder": "https://mail.example.com", "category": "connection"},
            {"key": "freemail_admin_token", "label": "管理员令牌", "secret": True, "category": "auth"},
            {"key": "freemail_username", "label": "用户名（可选）", "placeholder": "", "category": "auth"},
            {"key": "freemail_password", "label": "密码（可选）", "secret": True, "category": "auth"},
        ],
    },
    {
        "provider_type": "mailbox",
        "driver_type": "cfworker_admin_api",
        "label": "CF Worker Admin API",
        "description": "Cloudflare Worker 自建邮箱协议族。",
        "default_auth_mode": "admin_token",
        "auth_modes": [
            {"value": "admin_token", "label": "管理员 Token"},
            {"value": "public_jwt", "label": "Public JWT"},
        ],
        "fields": [
            {"key": "cfworker_api_url", "label": "API URL", "placeholder": "https://apimail.example.com", "category": "connection"},
            {"key": "cfworker_admin_token", "label": "管理员 Token", "secret": True, "category": "auth"},
            {"key": "cfworker_domain", "label": "邮箱域名", "placeholder": "example.com", "category": "connection"},
            {"key": "cfworker_fingerprint", "label": "Fingerprint", "placeholder": "6703363b...", "category": "auth"},
        ],
    },
    {
        "provider_type": "mailbox",
        "driver_type": "luckmail_token_query",
        "label": "LuckMail Token Query",
        "description": "LuckMail 已购邮箱查询模式。通过邮箱 + purchase token 直接轮询收件箱，不需要邮箱密码。",
        "default_auth_mode": "purchase_token",
        "auth_modes": [
            {"value": "purchase_token", "label": "Purchase Token"},
        ],
        "fields": [
            {"key": "luckmail_api_base_url", "label": "API URL", "placeholder": "https://mails.luckyous.com", "category": "connection"},
            {"key": "luckmail_email", "label": "邮箱地址", "placeholder": "example@hotmail.com", "category": "identity"},
            {"key": "luckmail_purchase_token", "label": "Purchase Token", "placeholder": "tok_xxx", "secret": True, "category": "auth"},
        ],
    },
    {
        "provider_type": "mailbox",
        "driver_type": "outlook_token_imap",
        "label": "Outlook Token IMAP",
        "description": "导入 Outlook 邮箱 + refresh token，通过 Microsoft OAuth 刷新令牌后走 IMAP 收件。支持跨站重复使用同一个邮箱。",
        "default_auth_mode": "refresh_token",
        "auth_modes": [
            {"value": "refresh_token", "label": "Refresh Token"},
        ],
        "fields": [
            {"key": "outlook_email", "label": "Outlook 邮箱", "placeholder": "demo@outlook.com", "category": "identity"},
            {"key": "outlook_password", "label": "邮箱密码（可选）", "secret": True, "category": "auth"},
            {"key": "outlook_client_id", "label": "Client ID", "placeholder": "000000004C12AE6F", "category": "auth"},
            {"key": "outlook_refresh_token", "label": "Refresh Token", "placeholder": "0.A...", "secret": True, "category": "auth"},
            {"key": "outlook_alias_max_count", "label": "父邮箱别名上限", "placeholder": "0 表示不限", "category": "config", "type": "number"},
        ],
    },
    {
        "provider_type": "mailbox",
        "driver_type": "yyds_mail_api",
        "label": "YYDS Mail API",
        "description": "215.im / YYDS Mail 动态邮箱，使用 API Key 创建邮箱并轮询收件箱。",
        "default_auth_mode": "api_key",
        "auth_modes": [
            {"value": "api_key", "label": "API Key"},
        ],
        "fields": [
            {"key": "yyds_mail_api_base_url", "label": "API URL", "placeholder": "https://maliapi.215.im", "category": "connection"},
            {"key": "yyds_mail_api_key", "label": "API Key", "secret": True, "category": "auth"},
            {"key": "yyds_mail_prefix", "label": "邮箱前缀", "placeholder": "可留空自动生成", "category": "identity"},
            {"key": "yyds_mail_domain", "label": "邮箱域名", "placeholder": "可留空使用服务默认值", "category": "identity"},
        ],
    },
    {
        "provider_type": "mailbox",
        "driver_type": "hstockplus_google_account",
        "label": "HStockPlus Google Account",
        "description": "HStockPlus Google/Gmail 成品账号，用于 Google OAuth 登录注册。",
        "default_auth_mode": "api_key",
        "auth_modes": [
            {"value": "api_key", "label": "API Key"},
        ],
        "fields": [
            {"key": "hstockplus_api_url", "label": "API URL", "placeholder": "https://hstockplus.com/api/v2", "category": "connection"},
            {"key": "hstockplus_api_key", "label": "API Key", "secret": True, "category": "auth"},
            {"key": "hstockplus_google_service_id", "label": "Google 商品/服务 ID", "placeholder": "可手动填写 HStockPlus service id", "category": "identity", "type": "hstockplus_product_select"},
            {"key": "hstockplus_quantity", "label": "购买数量", "placeholder": "1", "category": "identity"},
            {"key": "hstockplus_link", "label": "备注/链接", "placeholder": "可留空或填写订单备注", "category": "identity"},
            {"key": "hstockplus_delivery_timeout", "label": "交付超时秒数", "placeholder": "300", "category": "connection"},
            {"key": "hstockplus_request_timeout", "label": "API 请求超时秒数", "placeholder": "90", "category": "connection"},
            {"key": "hstockplus_enterprise_contract_required", "label": "需要企业/合约确认", "type": "checkbox", "placeholder": "false", "category": "config"},
            {"key": "hstockplus_enterprise_contract_accepted", "label": "已确认企业/合约", "type": "checkbox", "placeholder": "false", "category": "config"},
            {"key": "hstockplus_poll_interval", "label": "轮询间隔秒数", "placeholder": "5", "category": "connection"},
        ],
    },
    {
        "provider_type": "mailbox",
        "driver_type": "gptmail_api",
        "label": "GPTMail API",
        "description": "mail.chatgpt.org.uk 动态邮箱，使用 API Key 生成邮箱并轮询收件箱。",
        "default_auth_mode": "api_key",
        "auth_modes": [
            {"value": "api_key", "label": "API Key"},
        ],
        "fields": [
            {"key": "gptmail_api_base_url", "label": "API URL", "placeholder": "https://mail.chatgpt.org.uk", "category": "connection"},
            {"key": "gptmail_api_key", "label": "API Key", "secret": True, "category": "auth"},
            {"key": "gptmail_prefix", "label": "邮箱前缀", "placeholder": "可留空自动生成", "category": "identity"},
            {"key": "gptmail_domain", "label": "邮箱域名", "placeholder": "可留空使用服务默认值", "category": "identity"},
        ],
    },
]


PHONE_FILTER_FIELDS = [
    {"key": "phone_number", "label": "指定手机号（可选）", "placeholder": "完整手机号；留空随机取号", "category": "task"},
    {"key": "phone_segment", "label": "指定号段（可选）", "placeholder": "如 162 或 162,165；留空不过滤", "category": "task"},
    {"key": "phone_filter_attempts", "label": "号段/指定手机号重试次数", "placeholder": "5", "category": "task"},
]


PHONE_DRIVER_TEMPLATES = [
    {
        "provider_type": "phone",
        "driver_type": "haozhu_sms_api",
        "label": "豪猪 SMS API",
        "description": "豪猪接码平台。先登录获取固定 token，再按项目 ID 取手机号并轮询短信验证码。",
        "default_auth_mode": "username_password",
        "auth_modes": [
            {"value": "username_password", "label": "用户名密码"},
            {"value": "token", "label": "固定 Token"},
            {"value": "hybrid", "label": "用户名密码 + Token"},
        ],
        "fields": [
            {"key": "haozhu_api_base_url", "label": "API URL", "placeholder": "https://api.haozhuma.com", "category": "connection"},
            {"key": "haozhu_username", "label": "API 账号", "placeholder": "网页后台 API 账号", "category": "auth"},
            {"key": "haozhu_password", "label": "API 密码", "secret": True, "category": "auth"},
            {"key": "haozhu_token", "label": "固定 Token（可选）", "secret": True, "category": "auth"},
            {"key": "haozhu_project_id", "label": "项目 ID / sid", "placeholder": "1000", "category": "task"},
            {"key": "haozhu_uid", "label": "对接码 UID（可选）", "placeholder": "只取指定对接码", "category": "task"},
            {"key": "haozhu_author", "label": "开发者账号（可选）", "placeholder": "用于消费分成", "category": "task"},
            {"key": "haozhu_isp", "label": "运营商过滤（可选）", "placeholder": "isp，如 1=中国移动", "category": "task"},
            {"key": "haozhu_province", "label": "号码省份（可选）", "placeholder": "Province，如 44=广东", "category": "task"},
            {"key": "haozhu_ascription", "label": "号码类型（可选）", "placeholder": "1=虚拟，2=实卡", "category": "task"},
            {"key": "haozhu_paragraph", "label": "豪猪只取号段（可选）", "placeholder": "paragraph，如 162", "category": "task"},
            {"key": "haozhu_exclude", "label": "排除号段（可选）", "placeholder": "exclude", "category": "task"},
            *PHONE_FILTER_FIELDS,
            {"key": "haozhu_poll_interval", "label": "短信轮询间隔秒数", "placeholder": "15", "category": "connection"},
            {"key": "haozhu_phone_timeout", "label": "短信等待超时秒数", "placeholder": "180", "category": "connection"},
        ],
    },
    {
        "provider_type": "phone",
        "driver_type": "qianchuan_sms_api",
        "label": "千川 SMS API",
        "description": "千川接码平台。支持 API 凭证登录获取永久 token，按通道 ID 取手机号、轮询共享数据、释放和拉黑号码。",
        "default_auth_mode": "username_password",
        "auth_modes": [
            {"value": "username_password", "label": "用户名密码"},
            {"value": "token", "label": "固定 Token"},
            {"value": "hybrid", "label": "用户名密码 + Token"},
        ],
        "fields": [
            {"key": "qianchuan_api_base_url", "label": "API URL", "placeholder": "https://api.qc86.shop/api", "category": "connection"},
            {"key": "qianchuan_username", "label": "API Username", "placeholder": "网站 API 认证凭证用户名", "category": "auth"},
            {"key": "qianchuan_password", "label": "API Password", "secret": True, "category": "auth"},
            {"key": "qianchuan_token", "label": "固定 Token（可选）", "secret": True, "category": "auth"},
            {"key": "qianchuan_channel_id", "label": "通道 ID / channelId", "placeholder": "1237436366606831616", "category": "task"},
            {"key": "qianchuan_phone_num", "label": "指定手机号（可选）", "placeholder": "phoneNum，可留空随机取号", "category": "task"},
            {"key": "qianchuan_operator", "label": "运营商过滤", "placeholder": "0=全部，5=虚拟，4=非虚拟", "category": "task"},
            {"key": "qianchuan_scope", "label": "地区范围（可选）", "category": "task", "placeholder": "scope"},
            *PHONE_FILTER_FIELDS,
            {"key": "qianchuan_poll_interval", "label": "短信轮询间隔秒数", "placeholder": "5", "category": "connection"},
            {"key": "qianchuan_phone_timeout", "label": "短信等待超时秒数", "placeholder": "180", "category": "connection"},
        ],
    },
    {
        "provider_type": "phone",
        "driver_type": "5sim_api",
        "label": "5sim API",
        "description": "5sim 手机号接码平台。使用 API Token 购买号码，按订单 ID 轮询短信，支持释放和拉黑订单。",
        "default_auth_mode": "api_key",
        "auth_modes": [
            {"value": "api_key", "label": "API Token"},
        ],
        "fields": [
            {"key": "5sim_api_base_url", "label": "API URL", "placeholder": "https://5sim.net", "category": "connection"},
            {"key": "5sim_api_token", "label": "API Token", "secret": True, "category": "auth"},
            {"key": "5sim_country", "label": "国家 / country", "placeholder": "any / china / russia", "category": "task"},
            {"key": "5sim_operator", "label": "运营商 / operator", "placeholder": "any", "category": "task"},
            {"key": "5sim_product", "label": "产品 / product", "placeholder": "openai / telegram / google", "category": "task"},
            {"key": "5sim_max_price", "label": "最高价格（可选）", "placeholder": "maxPrice", "category": "task"},
            *PHONE_FILTER_FIELDS,
            {"key": "5sim_poll_interval", "label": "短信轮询间隔秒数", "placeholder": "5", "category": "connection"},
            {"key": "5sim_phone_timeout", "label": "短信等待超时秒数", "placeholder": "180", "category": "connection"},
        ],
    },
    {
        "provider_type": "phone",
        "driver_type": "apicc_sms_api",
        "label": "api.cc 免费接码",
        "description": "api.cc 公共免费号池（https://api.cc/home/index/free.html）。号码为共享公共号，短信全局可见、可重复用于无限次注册。填入要用的号码即可；可选按发送方过滤短信（如 Vellum/WorkOS 发送方 732873）。",
        "default_auth_mode": "public_free",
        "auth_modes": [
            {"value": "public_free", "label": "免登录公共号池"},
        ],
        "fields": [
            {"key": "apicc_api_base_url", "label": "API URL", "placeholder": "https://api.cc", "category": "connection"},
            {"key": "apicc_phone_number", "label": "手机号（公共号池中的号码）", "placeholder": "如 18194816943（可配置，不固定）", "category": "task"},
            {"key": "apicc_country_code", "label": "国家码", "placeholder": "+1", "category": "task"},
            {"key": "apicc_sender", "label": "发送方过滤（可选）", "placeholder": "如 732873；多个用逗号分隔，留空不过滤", "category": "task"},
            {"key": "apicc_poll_interval", "label": "短信轮询间隔秒数", "placeholder": "5", "category": "connection"},
            {"key": "apicc_phone_timeout", "label": "短信等待超时秒数", "placeholder": "180", "category": "connection"},
        ],
    },
]


CAPTCHA_DRIVER_TEMPLATES = [
    {
        "provider_type": "captcha",
        "driver_type": "local_solver",
        "label": "本地 Solver (Camoufox)",
        "description": "本地 Turnstile Solver。",
        "default_auth_mode": "endpoint_only",
        "auth_modes": [
            {"value": "endpoint_only", "label": "仅接口地址"},
        ],
        "fields": [
            {"key": "solver_url", "label": "Solver URL", "placeholder": "http://localhost:8889", "category": "connection"},
        ],
    },
    {
        "provider_type": "captcha",
        "driver_type": "yescaptcha_api",
        "label": "YesCaptcha API",
        "description": "YesCaptcha / OhMyCaptcha 兼容协议族。",
        "default_auth_mode": "api_key",
        "auth_modes": [
            {"value": "api_key", "label": "API Key"},
        ],
        "fields": [
            {"key": "yescaptcha_api_url", "label": "API Base URL", "placeholder": "https://api.yescaptcha.com", "category": "connection"},
            {"key": "yescaptcha_key", "label": "Client Key / API Key", "secret": True, "category": "auth"},
        ],
    },
    {
        "provider_type": "captcha",
        "driver_type": "twocaptcha_api",
        "label": "2Captcha API",
        "description": "2Captcha 协议族。",
        "default_auth_mode": "api_key",
        "auth_modes": [
            {"value": "api_key", "label": "API Key"},
        ],
        "fields": [
            {"key": "twocaptcha_key", "label": "2Captcha Key", "secret": True, "category": "auth"},
        ],
    },
    {
        "provider_type": "captcha",
        "driver_type": "patchright_harvester",
        "label": "Patchright Harvester (本地)",
        "description": "使用 patchright Chromium 本地过 Turnstile，无需第三方打码。默认 headed 模式（headless 无法过 Turnstile）。",
        "default_auth_mode": "endpoint_only",
        "auth_modes": [
            {"value": "endpoint_only", "label": "默认配置"},
        ],
        "fields": [
            {"key": "harvester_headless", "label": "无头模式 (仅调试)", "placeholder": "false", "category": "connection"},
            {"key": "harvester_max_contexts", "label": "最大并发上下文", "placeholder": "3", "category": "connection"},
            {"key": "harvester_proxy", "label": "代理", "placeholder": "http://user:pass@host:port", "category": "connection"},
        ],
    },
    {
        "provider_type": "captcha",
        "driver_type": "tulingcloud_api",
        "label": "图灵云 / fdyscloud API",
        "description": "调用 tulingcloud/fdyscloud 图片识别接口，当前用于辅助腾讯滑块自动拖动。",
        "default_auth_mode": "username_password",
        "auth_modes": [
            {"value": "username_password", "label": "用户名密码"},
            {"value": "token", "label": "UserToken"},
        ],
        "fields": [
            {"key": "tuling_api_base", "label": "API Base URL", "placeholder": "http://www.tulingcloud.com", "category": "connection"},
            {"key": "tuling_username", "label": "用户名", "category": "auth"},
            {"key": "tuling_password", "label": "密码", "secret": True, "category": "auth"},
            {"key": "tuling_usertoken", "label": "UserToken", "secret": True, "category": "auth"},
            {"key": "tuling_slider_model_id", "label": "滑块模型 ID", "placeholder": "48956156", "category": "task"},
            {"key": "tuling_developer", "label": "开发者账号（可选）", "category": "task"},
        ],
    },
]


BUILTIN_PROVIDER_DEFINITIONS = [
    {
        "provider_type": "mailbox",
        "provider_key": "moemail",
        "label": "MoeMail (sall.cc)",
        "description": "优先复用你手动注册好的 MoeMail 账号；未提供凭据时退回自动注册。",
        "driver_type": "moemail_api",
    },
    {
        "provider_type": "mailbox",
        "provider_key": "tempmail_lol",
        "label": "TempMail.lol（自动生成）",
        "description": "自动生成邮箱，通常无需额外配置；如果所在网络受限，请为任务配置可用代理。",
        "driver_type": "tempmail_lol_api",
    },
    {
        "provider_type": "mailbox",
        "provider_key": "duckmail",
        "label": "DuckMail（自动生成）",
        "description": "自动生成邮箱，支持自定义 Web 地址、Provider 地址和 Bearer Token。",
        "driver_type": "duckmail_api",
    },
    {
        "provider_type": "mailbox",
        "provider_key": "laoudo",
        "label": "Laoudo（固定邮箱）",
        "description": "固定邮箱模式，需要你自己提供已有邮箱和授权信息。",
        "driver_type": "laoudo_api",
    },
    {
        "provider_type": "mailbox",
        "provider_key": "freemail",
        "label": "Freemail（自建 CF Worker）",
        "description": "基于 Cloudflare Worker 的自建邮箱，支持管理员令牌或账号密码认证。",
        "driver_type": "freemail_api",
    },
    {
        "provider_type": "mailbox",
        "provider_key": "cfworker",
        "label": "CF Worker（自建域名）",
        "description": "使用你自己的域名和 Worker 邮件服务。",
        "driver_type": "cfworker_admin_api",
    },
    {
        "provider_type": "mailbox",
        "provider_key": "luckmail",
        "label": "LuckMail（已购邮箱）",
        "description": "适配 LuckMail 已购邮箱的 token 查询模式，可直接导入 `email----token` 文本作为邮箱来源。",
        "driver_type": "luckmail_token_query",
    },
    {
        "provider_type": "mailbox",
        "provider_key": "outlook_token",
        "label": "Outlook 令牌邮箱（可复用）",
        "description": "导入 `email----password----client_id----refresh_token` 后可直接收 Outlook 邮件。注册成功后邮箱会自动回池，支持跨站复用；可配置每个父邮箱最多创建多少个别名子邮箱。",
        "driver_type": "outlook_token_imap",
    },
    {
        "provider_type": "mailbox",
        "provider_key": "yyds_mail",
        "label": "YYDS Mail（215.im）",
        "description": "215.im / YYDS Mail 动态邮箱，使用 API Key 自动创建邮箱并轮询收件箱。",
        "driver_type": "yyds_mail_api",
    },
    {
        "provider_type": "mailbox",
        "provider_key": "hstockplus_google",
        "label": "HStockPlus Google 账号",
        "description": "通过 HStockPlus API 购买 Google/Gmail 成品账号，用作 Google OAuth 注册身份来源。",
        "driver_type": "hstockplus_google_account",
    },
    {
        "provider_type": "mailbox",
        "provider_key": "gptmail",
        "label": "GPTMail（动态邮箱）",
        "description": "mail.chatgpt.org.uk 动态邮箱，使用 API Key 生成邮箱并拉取验证码或验证链接。",
        "driver_type": "gptmail_api",
    },
    {
        "provider_type": "phone",
        "provider_key": "haozhu",
        "label": "豪猪",
        "description": "豪猪接码平台手机号来源，支持取号、轮询短信验证码、释放和拉黑号码。",
        "driver_type": "haozhu_sms_api",
    },
    {
        "provider_type": "phone",
        "provider_key": "qianchuan",
        "label": "千川",
        "description": "千川接码平台手机号来源，支持取号、轮询共享数据、释放和拉黑号码。",
        "driver_type": "qianchuan_sms_api",
    },
    {
        "provider_type": "phone",
        "provider_key": "5sim",
        "label": "5sim",
        "description": "5sim 手机号来源，支持按国家、运营商和产品购买号码并轮询短信验证码。",
        "driver_type": "5sim_api",
    },
    {
        "provider_type": "phone",
        "provider_key": "apicc",
        "label": "api.cc 免费接码",
        "description": "api.cc 公共免费号池手机号来源，填入公共号即可重复接码（一个号可注册无限账号），可选按发送方过滤短信。",
        "driver_type": "apicc_sms_api",
    },
    {
        "provider_type": "captcha",
        "provider_key": "local_solver",
        "label": "本地 Solver (Camoufox)",
        "description": "浏览器自动注册默认走本地 Solver。",
        "driver_type": "local_solver",
    },
    {
        "provider_type": "captcha",
        "provider_key": "yescaptcha",
        "label": "YesCaptcha",
        "description": "协议模式下优先尝试的远程打码服务。",
        "driver_type": "yescaptcha_api",
    },
    {
        "provider_type": "captcha",
        "provider_key": "ohmycaptcha",
        "label": "OhMyCaptcha",
        "description": "自建 YesCaptcha 兼容验证码服务。",
        "driver_type": "yescaptcha_api",
    },
    {
        "provider_type": "captcha",
        "provider_key": "2captcha",
        "label": "2Captcha",
        "description": "当 YesCaptcha 未配置时，协议模式会继续尝试 2Captcha。",
        "driver_type": "twocaptcha_api",
    },
    {
        "provider_type": "captcha",
        "provider_key": "patchright_harvester",
        "label": "Patchright 本地 Harvester",
        "description": "本地无头 Chromium 过 Turnstile，不依赖打码平台。Venice 协议模式首选。",
        "driver_type": "patchright_harvester",
    },
    {
        "provider_type": "captcha",
        "provider_key": "tulingcloud",
        "label": "图灵云 / fdyscloud",
        "description": "图灵验证码识别服务，辅助本地浏览器通过腾讯滑块。",
        "driver_type": "tulingcloud_api",
    },
]


def _clone(items: list[dict]) -> list[dict]:
    return deepcopy(items)


def list_driver_templates(provider_type: str) -> list[dict]:
    if provider_type == "mailbox":
        return _clone(MAILBOX_DRIVER_TEMPLATES)
    if provider_type == "captcha":
        return _clone(CAPTCHA_DRIVER_TEMPLATES)
    if provider_type == "phone":
        return _clone(PHONE_DRIVER_TEMPLATES)
    return []


def get_driver_template(provider_type: str, driver_type: str) -> dict | None:
    for item in list_driver_templates(provider_type):
        if item.get("driver_type") == driver_type:
            return item
    return None


def list_builtin_provider_definitions(provider_type: str | None = None) -> list[dict]:
    items = []
    for item in BUILTIN_PROVIDER_DEFINITIONS:
        if provider_type and item.get("provider_type") != provider_type:
            continue
        template = get_driver_template(str(item.get("provider_type") or ""), str(item.get("driver_type") or "")) or {}
        items.append({
            "provider_type": item.get("provider_type", ""),
            "provider_key": item.get("provider_key", ""),
            "label": item.get("label", ""),
            "description": item.get("description", ""),
            "driver_type": item.get("driver_type", ""),
            "default_auth_mode": template.get("default_auth_mode", ""),
            "auth_modes": template.get("auth_modes", []),
            "fields": template.get("fields", []),
            "enabled": True,
            "is_builtin": True,
            "metadata": {},
        })
    return items
