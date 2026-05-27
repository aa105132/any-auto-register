# Google 账号池 — 多平台 OAuth 复用指南

## 概述

`output/google_accounts_pool.json` 是通用 Google 账号池，多 OAuth 平台共享。HStockPlus 购买后自动写入，手动也可添加。

核心模块：`core/google_account_pool.py` — `GoogleAccountPool` 类负责池读写。

## 账号池 JSON 结构

```json
{
  "version": 1,
  "accounts": [
    {
      "email": "xxx@antttool.us",
      "password": "GooglePassword",
      "added_at": "2026-05-19T00:00:00Z",
      "expires_at": "2027-05-19T00:00:00Z",
      "source": "hstockplus",
      "source_order_id": "78256",
      "registered_platforms": ["gettoken", "cursor"],
      "notes": ""
    }
  ]
}
```

字段说明：
- `email` / `password`: Google 账号凭据
- `expires_at`: 过期时间，过期后不应再使用（空字符串表示未知）
- `registered_platforms`: 已注册的平台列表，取号时会跳过

## 批量购买账号并导入

通过 HStockPlus 购买 Google Workspace for Education 账号（约 $0.012/个）：

1. 在 `frontend` 全局配置中设置 `hstockplus_api_key` 和 `hstockplus_google_service_id=18972`
2. 直接调用 HStockPlus API 批量购买：POST `https://hstockplus.com/api/v2` action=add, service=18972, quantity=N
3. 购买完成后账号自动写入 `output/google_accounts_pool.json`
4. 可以手动编辑 JSON 添加更多账号

## 在新 OAuth 平台复用账号池

### 方案 A — 通过 HStockPlus provider + reuse_mode（推荐）

配置参数：
```json
{
  "identity_provider": "oauth_browser",
  "mail_provider": "hstockplus_google_account",
  "oauth_account_source": "mailbox",
  "oauth_provider": "google",
  "mailbox_proxy_mode": "direct",
  "hstockplus_api_key": "your_api_key",
  "hstockplus_reuse_mode": true
}
```

自动流程：
1. `BrowserOAuthIdentityProvider.resolve()` → `mailbox.get_email()` → `HStockPlusGoogleAccountProvider.get_email()`
2. reuse_mode=true → 从 `output/google_accounts_pool.json` 取一个未注册当前平台的账号
3. 返回的 `MailboxAccount.extra.provider_account.credentials.password` 包含 Google 密码
4. `_resolve_oauth_password()` 自动读取密码传给 browser_oauth

### 方案 B — 直接用 GoogleAccountPool

```python
from core.google_account_pool import GoogleAccountPool

pool = GoogleAccountPool()
acct = pool.acquire(exclude_platforms=["my_platform"])
if not acct:
    raise RuntimeError("池中无可用账号")

# OAuth 注册完成后
pool.mark_registered(acct.email, "my_platform")
```

### 在 register_with_browser_oauth 中集成

```python
def register_with_browser_oauth(email_hint, google_password, ...):
    # email_hint 和 google_password 由调用方传入
    # 从 MailboxAccount 拿到的就是池中账号
    ...
```

## 完整 OAuth 测试流程（已验证 2026-05-19）

新购 Google Workspace for Education 账号含 ToS 页面。全流程：

1. gettoken.dev → Login → 弹出 Portal Login iframe（pay.imgto.link 跨域）
2. iframe 内点 "Continue with Google" → **必须用 Playwright Locator API**（page.evaluate 跨不过跨域 iframe）
3. Google signin → 填 email → 填 password
4. **Google speedbump/gaplustos** → "Welcome to your new account" → 点 "I understand"
5. OAuth consent "Sign in to yinziai.com" → 点 "Continue"
6. 浏览器崩溃后重启 → cookies 持久化 → gettoken 已登录
7. /console/api-keys → Create API Key → 保存

**已知问题**：OAuth consent 点 Continue 后浏览器崩溃（Playwright/CDP crash），但回调已完成，重启后 cookies 恢复登录态。代码需加崩溃恢复重试。

## OAuth 开发注意事项

### 跨域 iframe 点击

GetToken 等平台 OAuth 按钮在 Portal Login iframe（pay.imgto.link 跨域）中。
**不能用 page.evaluate() 搜索 iframe 内按钮**，必须用 Playwright Locator API：

```python
# 错误 — page.evaluate 穿不过跨域 iframe
page.evaluate("() => { document.querySelector('iframe')... }")

# 正确 — Playwright Locator 通过 CDP 跨域
for frame_el in page.locator("iframe").all():
    content = frame_el.content_frame()
    btn = content.get_by_role("button", name=re.compile("google", re.IGNORECASE))
    if btn.count() > 0:
        btn.first.click()
```

见 `platforms/gettoken/browser_oauth.py:_click_oauth_in_iframes` 的实现。

### 密码解析

`_resolve_oauth_password()` 从以下路径依次查找密码：
1. ctx.extra: `oauth_password`, `google_password`, `hstockplus_google_password`
2. ctx.identity.mailbox_account.extra.provider_account.credentials.password

### 浏览器模式路径

`base_platform.py register()` 路由：
- executor_type=headless/headed → BrowserRegistrationFlow → browser_oauth
- executor_type=protocol + identity_provider=oauth_browser → ProtocolOAuthFlow → protocol_oauth

协议模式需要预先获取 portal_login_token，否则降级到浏览器。

## 账号池 API 参考

```python
pool = GoogleAccountPool()                     # 使用默认路径 output/google_accounts_pool.json
pool = GoogleAccountPool(pool_path="custom")    # 自定义路径

acct = pool.acquire(exclude_platforms=["p1"])   # 取号，跳过已注册 p1 的
pool.mark_registered(email, "platform_name")     # 标记已注册
pool.add_account(email, password, source="h")    # 手动添加
pool.add_from_hstockplus_line("e----p", "id")   # HStockPlus 原始格式导入
pool.stats()                                     # {"total": N, "unused": M, "by_platform": {...}}
pool.list_all()                                  # 列出全部账号
```
