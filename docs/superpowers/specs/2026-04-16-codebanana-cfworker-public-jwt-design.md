# CodeBanana 纯协议注册 + cfworker public_jwt 扩展设计

## 1. 背景

当前需求是为项目新增 `https://www.codebanana.com/` 的注册能力，并在注册成功后提取并保存平台登录凭证。用户已明确选择：

- 注册方式：纯协议
- 验证邮箱：复用现有 mailbox/provider 架构
- mailbox 方向：不新增独立 `bufanmail` provider，扩展现有 `cfworker`
- mailbox 扩展方案：支持 `public_jwt`

本设计基于两类已验证的运行时事实：

### 1.1 CodeBanana 已验证事实

已确认可用接口：

- `POST /api/auth/check-username`
- `POST /api/auth/send-verification-code`
- `POST /api/auth/verify-and-register`
- `GET /api/auth/csrf`
- `POST /api/auth/callback/credentials`
- `GET /api/auth/session`

已确认行为：

- 注册验证码为 **4 位**
- `verify-and-register` 成功后 **不会自动登录**
- 需要额外走 next-auth credentials 登录流程
- 登录成功后可获取：
  - Cookie：`__Secure-next-auth.session-token`
  - Session JSON 中的 `jwtToken`

### 1.2 cloudflare_temp_email 部署已验证事实

用户说明当前邮箱部署基于：

- `https://github.com/dreamhunter2333/cloudflare_temp_email`

已确认当前部署既存在管理员接口，也存在公共 JWT 接口：

#### 公共 JWT 模式

- `GET /open_api/settings`
- `POST /api/new_address`
- `GET /api/mails?limit=20&offset=0`
- `GET /api/mail/{id}`

行为：

- 匿名创建邮箱后返回：
  - `jwt`
  - `address`
  - `password`
  - `address_id`
- 后续通过 `Authorization: Bearer <mailbox_jwt>` 查询邮件

#### 管理员模式

- `POST /admin/new_address`

行为：

- 不带管理员口令时返回 `401`
- 说明管理员接口真实存在，当前部署属于 `cfworker` 家族，不是 `freemail`

## 2. 设计目标

### 2.1 本次要完成的目标

1. 新增 `CodeBanana` 平台插件
2. 保持 **纯协议注册**
3. 扩展现有 `cfworker` mailbox，使其同时支持：
   - `admin_token`
   - `public_jwt`
4. 注册成功后保存以下平台凭证：
   - `session_token`
   - `jwtToken`
   - `cookies`
   - `session_json`
   - `csrf_token`
5. 同时保留用于收码回放的 mailbox 侧凭证：
   - `mailbox_jwt`
   - `mailbox_email`
   - `mailbox_password`
   - `address_id`

### 2.2 明确不做

- 不新增独立 `bufanmail` provider key
- 不改浏览器注册链路
- 不改无关平台
- 不做 UI 层额外改版

## 3. 与现有架构的对齐方式

项目已有成熟平台注册架构：

- `core/base_platform.py`
- `core/registration/flows.py`
- `core/registration/models.py`
- `core/base_mailbox.py`
- `core/account_graph.py`

已有可复用模式：

- 协议注册平台：`platforms/trae/plugin.py`
- mailbox provider 工厂：`core/base_mailbox.py`
- provider 定义：`core/provider_drivers.py`
- 凭证落库与图谱同步：`core/account_graph.py`

因此本次设计遵循现有模式，不引入新总线或额外抽象层。

## 4. 模块设计

## 4.1 新增 CodeBanana 平台插件

新增目录：

```text
platforms/codebanana/
├── __init__.py
├── plugin.py
├── core.py
└── protocol_mailbox.py
```

职责划分如下：

### `plugin.py`

负责：

- 注册平台元信息
- 声明支持的 executor 与 identity mode
- 构建 `ProtocolMailboxAdapter`
- 将 worker 输出映射为 `RegistrationResult`

### `core.py`

负责封装 CodeBanana 纯 HTTP 协议调用，包括：

1. 检查用户名可用性
2. 发送验证码
3. 提交注册
4. 获取 CSRF
5. credentials 登录
6. 拉取 session
7. 提取 cookie / jwt / session

### `protocol_mailbox.py`

负责完整编排：

1. 使用 mailbox 地址参与注册
2. 调用发码接口
3. 通过 mailbox 轮询验证码
4. 完成注册
5. 完成登录
6. 组装统一结果

## 4.2 扩展现有 cfworker mailbox

不新建 `BufanMailMailbox`，直接扩展：

- `core/base_mailbox.py` 中的 `CFWorkerMailbox`
- `core/provider_drivers.py` 中的 `cfworker` provider 定义

新增认证模式字段：

- `cfworker_auth_mode`
  - `admin_token`
  - `public_jwt`

### admin_token 模式

保持现有实现兼容：

- `POST /admin/new_address`
- `GET /admin/mails`

### public_jwt 模式

新增能力：

- `GET /open_api/settings`
- `POST /api/new_address`
- `GET /api/mails`
- `GET /api/mail/{id}`

该模式不要求管理员 token，而是在创建邮箱后保存 mailbox 自身返回的 jwt。

## 5. 配置设计

`cfworker` 的运行时配置扩展为：

- `cfworker_api_url`
- `cfworker_auth_mode`
- `cfworker_admin_token`
- `cfworker_domain`
- `cfworker_fingerprint`

解释如下：

### `cfworker_api_url`

- `admin_token` 模式下通常指向管理员 API 根地址
- `public_jwt` 模式下应指向当前部署的 API 根，例如 `https://apimail.example.com`

### `cfworker_auth_mode`

- 缺省值保持兼容，推荐默认为 `admin_token`
- 当用户明确选择 `public_jwt` 时走公共匿名建箱流程

### `cfworker_admin_token`

- 仅 `admin_token` 模式必需

### `cfworker_domain`

- 可选域名
- 用于指定创建邮箱时优先选择的邮箱域

### `cfworker_fingerprint`

- `public_jwt` 模式下可选
- 若未提供，可运行时自动生成稳定值

## 6. 注册流程设计

## 6.1 mailbox 获取阶段

平台执行注册前，通过现有 identity/mailbox 流程拿到邮箱账号：

- `email`
- `account_id`
- `extra`

在 `cfworker public_jwt` 模式下，`MailboxAccount.extra` 需要补齐：

- `provider_account`
  - `provider_name = "cfworker"`
  - `credentials.mailbox_jwt`
  - `credentials.address_password`
- `provider_resource`
  - `resource_type = "mailbox"`
  - `metadata.address_id`
  - `metadata.api_url`

这样后续平台注册完成后，既能收码，也能回放 mailbox 凭证。

## 6.2 CodeBanana 协议注册阶段

建议顺序如下：

1. 生成用户名与密码
2. `POST /api/auth/check-username`
3. `POST /api/auth/send-verification-code`
4. mailbox 轮询最近新邮件
5. 从邮件中提取 **4 位验证码**
6. `POST /api/auth/verify-and-register`

其中验证码提取逻辑需要支持：

- 直接从 raw MIME / HTML 中匹配 4 位数字
- 优先过滤时间戳、message-id 等无关数字

推荐的默认正则：

```text
(?<!\d)(\d{4})(?!\d)
```

必要时可再结合关键字 `Verification Code` 提高命中稳定性。

## 6.3 CodeBanana 协议登录阶段

注册成功后立即执行纯协议登录：

1. `GET /api/auth/csrf`
2. `POST /api/auth/callback/credentials`
3. 从响应 cookie jar 中提取：
   - `__Secure-next-auth.session-token`
4. `GET /api/auth/session`
5. 从响应 JSON 中提取：
   - `jwtToken`
   - `user`
   - 其他 session 元数据

说明：

- `verify-and-register` 成功不代表 session 已建立
- 必须单独走 credentials 登录流程

## 7. 返回结果与落库设计

## 7.1 `RegistrationResult` 映射

`CodeBanana` 的 `RegistrationResult` 建议如下：

- `email`
- `password`
- `user_id`
- `token`
  - 写入主 token，值为 `session_token`
- `status`
  - `registered`
- `extra`
  - `jwtToken`
  - `session_token`
  - `cookies`
  - `session_json`
  - `csrf_token`
  - `username`
  - `verification_mailbox`

## 7.2 主凭证规则

在 `core/account_graph.py` 中新增：

```python
PRIMARY_TOKEN_WRITE_KEYS["codebanana"] = "session_token"
```

原因：

- 当前可稳定复用的是 next-auth session cookie
- `jwtToken` 应作为补充平台 JWT 保存
- 主 token 用 `session_token` 更符合项目现有习惯

## 7.3 建议保存的 extra 字段

建议结构：

```python
{
    "username": "...",
    "session_token": "...",
    "jwtToken": "...",
    "cookies": {
        "__Secure-next-auth.session-token": "...",
        "__Host-next-auth.csrf-token": "...",
        "__Secure-next-auth.callback-url": "..."
    },
    "session_json": {...},
    "csrf_token": "...",
    "verification_mailbox": {
        "provider": "cfworker",
        "auth_mode": "public_jwt",
        "email": "...",
        "mailbox_jwt": "...",
        "address_password": "...",
        "address_id": "...",
        "api_url": "..."
    }
}
```

## 8. mailbox 扩展细节

## 8.1 `CFWorkerMailbox.get_email()`

需要根据 `cfworker_auth_mode` 分支：

### admin_token

沿用现有逻辑：

- 请求 `/admin/new_address`
- 保存返回的 email 与 token

### public_jwt

新增逻辑：

1. 调 `POST /api/new_address`
2. 请求体包含：
   - `name`
   - `domain`
   - `cf_token`
3. 从响应中提取：
   - `jwt`
   - `address`
   - `password`
   - `address_id`

## 8.2 `CFWorkerMailbox.get_current_ids()`

### admin_token

继续使用管理员邮件列表接口。

### public_jwt

改用：

- `GET /api/mails?limit=20&offset=0`
- `Authorization: Bearer <mailbox_jwt>`

返回 `results[].id`

## 8.3 `CFWorkerMailbox.wait_for_code()`

### public_jwt

改用：

- `GET /api/mails`
- 对新邮件再调 `GET /api/mail/{id}`
- 从 `raw` 内容或详情内容中提取验证码

这样可避免只依赖摘要列表，提升兼容性。

## 9. 错误处理设计

## 9.1 mailbox 阶段错误

- `/open_api/settings` 不可达
  - 报错：邮箱 API 不可访问
- `/api/new_address` 返回失败
  - 报错：邮箱创建失败
- 邮件轮询超时
  - 报错：未在指定时间内收到验证码

## 9.2 CodeBanana 阶段错误

- 用户名不可用
  - 重新生成用户名后重试有限次数
- 发码失败
  - 直接抛出接口错误
- 验证码错误
  - 明确透出 `VERIFICATION_CODE_INCORRECT`
- 注册成功但登录失败
  - 将其视为注册链路失败，因为需求要求保存 token
- 登录成功但 session 拉取失败
  - 报错并保留已获得的 cookie 作为调试证据

## 10. 测试与验证设计

至少覆盖以下层次：

### 10.1 单元/轻量验证

- `cfworker public_jwt` 创建邮箱
- `cfworker public_jwt` 轮询邮件 ID
- 4 位验证码提取函数
- CodeBanana 登录 cookie 解析

### 10.2 集成验证

端到端跑通：

1. 创建临时邮箱
2. CodeBanana 发码
3. 收到验证码
4. 注册成功
5. 登录成功
6. `/api/auth/session` 返回 `jwtToken`
7. 账号落库后凭证可见

### 10.3 回归验证

确保以下不被破坏：

- 原有 `cfworker admin_token` 模式
- 其他 mailbox provider
- 原有协议注册平台

## 11. 最终实施范围

预计影响文件：

- `core/base_mailbox.py`
- `core/provider_drivers.py`
- `core/account_graph.py`
- `platforms/codebanana/__init__.py`
- `platforms/codebanana/plugin.py`
- `platforms/codebanana/core.py`
- `platforms/codebanana/protocol_mailbox.py`

必要时可能补充：

- `platforms/codebanana` 目录下测试或调试脚本

## 12. 设计决策总结

本设计的最终决策为：

1. `mail.bufan.de5.net` 归类为 **cfworker 家族**
2. 不新增 `bufanmail` provider key
3. 扩展 `cfworker` 支持 `public_jwt`
4. 新增 `CodeBanana` 纯协议注册插件
5. 主平台凭证保存为 `session_token`
6. 同时保存 `jwtToken`、cookies、session_json 与 mailbox 凭证

该设计遵循现有项目结构，变更范围明确，兼容已有 `cfworker` 管理员模式，且能直接覆盖用户当前实际部署形态。
