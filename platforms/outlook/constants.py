"""Outlook/Hotmail 注册平台常量。

集中所有 URL、内置公开 client_id、OAuth scope 和页面选择器，便于在微软前端
改版或 client_id 被限流时单点维护。选择器参考自公开注册流程实测，作者也明确
警告"选择器经常更新"，故全部抽到这里。
"""
from __future__ import annotations

# ---- 注册入口 ----
OUTLOOK_SIGNUP_URL = "https://outlook.live.com/mail/0/?prompt=create_account"
# 注册完成标志：Outlook Web 邮箱主界面左侧"新邮件"按钮
OUTLOOK_INBOX_URL_HINT = "outlook.live.com/mail"

# ---- Microsoft OAuth2 (PKCE) ----
OUTLOOK_OAUTH_AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
OUTLOOK_OAUTH_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
# 个人账号消费者端点（备用，部分公开 client_id 走 consumers 租户更稳）
OUTLOOK_OAUTH_AUTHORIZE_URL_CONSUMERS = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
OUTLOOK_OAUTH_TOKEN_URL_CONSUMERS = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

# 内置默认 client_id：用户自注册的 Azure 公共客户端应用（Native Client），
# 支持个人 + 工作/学校账号，IMAP 读写 + Graph Mail + 日历全权限。
# 公共 client 不需要 client_secret，适合 PKCE 流程；用户可在任务 extra 用
# outlook_client_id 覆盖。
DEFAULT_CLIENT_ID = "81e460af-5884-4fee-bd10-4c40dec6d32b"
# redirect_url 必须与 client_id 注册时一致；该应用注册了 nativeclient。
DEFAULT_REDIRECT_URL = "https://login.microsoftonline.com/common/oauth2/nativeclient"

# IMAP 收件 + Graph 邮件/日历 + 登录基础 scope。
# IMAP.AccessAsUser.All 走 XOAUTH2 直连 outlook.live.com:993；
# offline_access 拿 refresh_token；其余覆盖读信、发信、日历、个人资料。
DEFAULT_SCOPES = (
    "offline_access",
    "openid",
    "email",
    "profile",
    "User.Read",
    "Mail.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.Read",
    "Calendars.ReadWrite",
    "IMAP.AccessAsUser.All",
)

# ---- IMAP ----
OUTLOOK_IMAP_SERVER = "outlook.live.com"
OUTLOOK_IMAP_PORT = 993

# ---- 邮箱后缀 ----
DEFAULT_EMAIL_SUFFIX = "@outlook.com"
SUPPORTED_EMAIL_SUFFIXES = ("@outlook.com", "@hotmail.com")

# ---- 注册表单选择器（参考公开实现，微软改版时集中更新） ----
# 多语言：中文 + 英文 + 其它常见语言。aria-label / 文本会随 IP 地区语言变化，
# 但 data-testid 跨语言一致，优先用 data-testid。
SEL_AGREE_CONTINUE_TEXTS = ("同意并继续", "Agree and continue", "Agree & continue", "I agree", "Agree")
SEL_EMAIL_INPUT = '[aria-label="新建电子邮件"]'
SEL_EMAIL_INPUT_EN = '[aria-label="Create a new email address"]'
SEL_EMAIL_INPUT_FALLBACK = 'input[type="email"][name="email"]'
SEL_PRIMARY_BUTTON = '[data-testid="primaryButton"]'
SEL_PASSWORD_INPUT = '[type="password"]'
SEL_BIRTH_YEAR = '[name="BirthYear"]'
SEL_BIRTH_MONTH = '[name="BirthMonth"]'
SEL_BIRTH_DAY = '[name="BirthDay"]'
SEL_LAST_NAME = '#lastNameInput'
SEL_FIRST_NAME = '#firstNameInput'
SEL_OUTLOOK_SUFFIX_TEXT = "@outlook.com"
SEL_HOTMAIL_SUFFIX_OPTION = '[role="option"]:text-is("@hotmail.com")'
# 月份/日期选项文案随语言变化：中文"5月"/"15日"，英文"May"/"15"
SEL_MONTH_OPTION_TEMPLATE_CN = '[role="option"]:text-is("{month}月")'
SEL_DAY_OPTION_TEMPLATE_CN = '[role="option"]:text-is("{day}日")'
SEL_MONTH_NAMES_EN = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
SEL_DAY_OPTION_TEMPLATE_EN = '[role="option"]:text-is("{day}")'
# 注册完成等待标志：中文"新邮件"，英文"New mail"
SEL_NEW_MAIL_BUTTON = '[aria-label="新邮件"]'
SEL_NEW_MAIL_BUTTON_EN = '[aria-label="New mail"]'
# 防机器人等待消失标志（跨语言一致：URL 匹配）
SEL_BOT_PROTECTION_LINK = 'span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]'
# IP 频率限制 / 帐户被阻止文案（多语言）
RATE_LIMIT_TEXTS = ("一些异常活动", "此站点正在维护，暂时无法使用，请稍后重试。")
ACCOUNT_BLOCKED_TEXTS = ("帐户创建已被阻止", "Account creation has been blocked", "We detected some unusual activity")
# 非按压类 FunCaptcha 检测（无法过）
SEL_ENFORCEMENT_FRAME = 'iframe#enforcementFrame'

# ---- Arkose 长按压验证选择器（多语言） ----
SEL_ARKOSE_OUTER_IFRAME = 'iframe[title="验证质询"]'
SEL_ARKOSE_OUTER_IFRAME_EN = 'iframe[title="Verification challenge"]'
SEL_ARKOSE_INNER_IFRAME = 'iframe[style*="display: block"]'
SEL_ARKOSE_FIRST_PRESS = '[aria-label="可访问性挑战"]'
SEL_ARKOSE_FIRST_PRESS_EN = '[aria-label="Accessibility challenge"]'
SEL_ARKOSE_SECOND_PRESS = '[aria-label="再次按下"]'
SEL_ARKOSE_SECOND_PRESS_EN = '[aria-label="Press again"]'
SEL_ARKOSE_DRAW = '.draw'
SEL_ARKOSE_LOADING_STATUS = '[role="status"][aria-label="正在加载..."]'
SEL_ARKOSE_LOADING_STATUS_EN = '[role="status"][aria-label="Loading..."]'
# 成功/重试文案（多语言）
ARKOSE_SUCCESS_TEXTS = ("取消", "Cancel", "取消", "Cancel")
ARKOSE_RETRY_TEXTS = ("请再试一次", "Please try again", "Try again")

# ---- OAuth 同意页选择器 ----
SEL_OAUTH_LOGINFMT = '[name="loginfmt"]'
SEL_OAUTH_SIGNIN_BUTTON = '#idSIButton9'
SEL_OAUTH_CONSENT_BUTTON = '[data-testid="appConsentPrimaryButton"]'

# ---- 配置键（任务 extra / provider field） ----
EXTRA_CLIENT_ID = "outlook_client_id"
EXTRA_REDIRECT_URL = "outlook_redirect_url"
EXTRA_SCOPES = "outlook_scopes"
EXTRA_EMAIL_SUFFIX = "outlook_email_suffix"
EXTRA_BOT_PROTECTION_WAIT = "outlook_bot_protection_wait"
EXTRA_MAX_CAPTCHA_RETRIES = "outlook_max_captcha_retries"
EXTRA_USE_CAMOUFOX = "outlook_use_camoufox"
EXTRA_USE_PROTOCOL_PROOF = "outlook_use_protocol_proof"
EXTRA_REGISTER_TIMEOUT = "outlook_register_timeout"
EXTRA_OAUTH_TIMEOUT = "outlook_oauth_timeout"
EXTRA_USE_CONSUMERS_TENANT = "outlook_use_consumers_tenant"

# 默认行为参数
DEFAULT_BOT_PROTECTION_WAIT_SECONDS = 11
DEFAULT_MAX_CAPTCHA_RETRIES = 2
DEFAULT_REGISTER_TIMEOUT = 240
DEFAULT_OAUTH_TIMEOUT = 90
