# ATXP + Clowdbot 全量接入设计

## 1. 背景

当前需求是在 `any-auto-register` 中新增 `ATXP` 平台接入能力，并且不是只做基础账号注册，而是一次性覆盖完整交付面：

1. `ATXP` 主账号注册
2. `Clowdbot` 任务流接入
3. 注册成功后的自动入库
4. 账号详情页展示核心 ATXP / Clowdbot 字段
5. 导出支持（`json` / `csv` / `txt`，支持勾选字段）
6. 复用当前项目已有的多邮箱 provider 抽象，而不是把邮箱实现写死到平台代码里

用户已经明确确认以下边界：

- 平台范围：`ATXP + Clowdbot` 一起接入
- 交付范围：全量接入（注册、入库、详情、导出）
- 邮箱策略：复用当前项目 mailbox 抽象，支持多邮箱源切换
- 成功判定：只要 `ATXP` 主账号和核心凭证拿到，就允许入库；`Clowdbot` 失败单独记状态并支持后续补跑

## 2. 已确认的运行时事实

### 2.1 当前项目的接入模式

`any-auto-register` 已经具备稳定的平台接入形态：

- `core/base_platform.py`
- `core/registration/adapters.py`
- `core/registration/flows.py`
- `core/base_mailbox.py`
- `core/account_graph.py`
- `application/account_exports.py`
- `frontend/src/pages/Accounts.tsx`

新增协议注册平台的 canonical pattern 已明确为：

```text
platforms/<platform>/
├── __init__.py
├── plugin.py
├── core.py
└── protocol_mailbox.py
```

并且平台注册结果最终会被归一到现有的 account graph 模型中：

- `overview`
- `credentials`
- `provider_accounts`
- `provider_resources`

因此本次设计不新增新的账号表结构，只沿用现有模型。

### 2.2 参考实现 `atxp` 项目的已验证流程

外部参考实现位于：

- `D:\Desktop\cat\atxp\server.js`
- `D:\Desktop\cat\atxp\ATXP_FULL_FLOW.md`

已确认的主流程为：

1. 创建临时邮箱
2. `Privy` 发送邮箱 OTP
3. 收码并完成 `Privy` 登录
4. 调用 `accounts.atxp.ai`
   - `GET /me`
   - `POST /wallets/ensure`
   - `GET /connection-token`
5. 组装 `ATXP_CONNECTION`
6. 调用 `llm.atxp.ai/v1/models` 做 gateway 探活
7. 继续登录 `clowdbot.atxp.ai`
8. 完成任务：
   - `create_clowdbot`
   - `claim_email`

已确认的关键凭证与字段包括：

- `privy token`
- `refresh token`
- `accountId`
- `connectionToken`
- `connectionString`
- `walletInfo`
- `clowdbotInstanceId`
- `claimedAgentEmail`
- `rewardProgress`

## 3. 设计目标

### 3.1 本次要完成

1. 新增 `ATXP` 平台插件，平台 key 为 `atxp`
2. 复用现有 mailbox provider 体系完成 ATXP 邮箱注册链路
3. 完成 `ATXP` 主账号注册与 `Clowdbot` 任务流编排
4. 按现有 account graph 规范落库
5. 在账号详情页展示核心字段和任务状态
6. 在导出菜单中新增 `ATXP` 字段配置，并支持 `txt/json/csv`
7. 新增平台 action：`retry_clowdbot_tasks`

### 3.2 明确不做

1. 不保留对外部 `D:\Desktop\cat\atxp` Node 项目的运行时依赖
2. 不新增独立 `clowdbot` 平台
3. 不修改现有邮箱 provider 架构的总体设计
4. 不新增数据库表，仅复用现有 account graph 模型
5. 不把某个具体邮箱实现（如 bufan / cfworker）写死在 `ATXP` 平台代码里

## 4. 总体方案

采用单平台方案：

- 新平台名：`atxp`
- `ATXP` 主账号与 `Clowdbot` 任务流同属一个平台
- `Clowdbot` 不是第二条账号，而是 `ATXP` 账号上的扩展任务状态

整体结构如下：

```text
mailbox provider
  → Privy OTP 登录
  → accounts.atxp.ai 初始化账号 / 钱包 / connection token
  → llm.atxp.ai 探活
  → clowdbot.atxp.ai 完成任务
  → RegistrationResult
  → account graph 落库
  → 前端详情 / 导出 / 平台动作
```

这样设计的原因：

1. 与当前项目的插件模式完全对齐
2. 可以直接复用 mailbox identity、OTP callback、account graph、导出菜单、平台 action
3. 便于后续对 `Clowdbot` 失败账号做单独补跑

## 5. 模块设计

新增目录：

```text
platforms/atxp/
├── __init__.py
├── plugin.py
├── core.py
└── protocol_mailbox.py
```

### 5.1 `platforms/atxp/plugin.py`

职责：

1. 通过 `@register` 注册平台
2. 声明：
   - `name = "atxp"`
   - `display_name = "ATXP"`
   - `supported_executors = ["protocol"]`
   - `supported_identity_modes = ["mailbox"]`
3. 构建 `ProtocolMailboxAdapter`
4. 把 worker 返回的原始结果映射成 `RegistrationResult`
5. 暴露平台动作：
   - `retry_clowdbot_tasks`

### 5.2 `platforms/atxp/core.py`

职责：封装纯协议 HTTP 客户端，不处理 mailbox provider 抽象。

建议拆分的方法：

- `send_privy_code(...)`
- `authenticate_privy(...)`
- `fetch_atxp_bundle(...)`
- `probe_gateway_connection(...)`
- `login_clowdbot_oidc(...)`
- `complete_clowdbot_tasks(...)`

其中：

- `send_privy_code` / `authenticate_privy` 负责 `auth.privy.io`
- `fetch_atxp_bundle` 负责 `accounts.atxp.ai`
- `probe_gateway_connection` 负责 `llm.atxp.ai`
- `login_clowdbot_oidc` 与 `complete_clowdbot_tasks` 负责 `clowdbot.atxp.ai`

### 5.3 `platforms/atxp/protocol_mailbox.py`

职责：只负责流程编排。

主流程顺序固定为：

1. 读取由现有 identity/mailbox 流程解析出的邮箱地址
2. 请求 `Privy` 发码
3. 通过 `otp_callback()` 收取 OTP
4. 完成 `Privy` 登录
5. 拉取 `ATXP` 主账号信息、钱包信息、connection token
6. 组装 `connection_string`
7. 探活 gateway
8. 继续执行 `Clowdbot` 登录与任务
9. 汇总结果

### 5.4 需要联动的现有模块

除新增平台目录外，还需要联动以下模块：

- `core/account_graph.py`
- `application/account_exports.py`
- `frontend/src/pages/Accounts.tsx`
- `tests/test_account_exports.py`
- 新增 `ATXP` 平台相关测试文件

## 6. 注册与任务流程设计

### 6.1 邮箱获取阶段

`ATXP` 平台不自己创建邮箱 provider，而是直接复用当前项目已经存在的 mailbox 解析链路：

- `application/tasks.py` 中 `_build_platform_instance(...)`
- `core/base_mailbox.py` 中 `create_mailbox(...)`
- `core/base_platform.py` 中 identity 解析和 mailbox snapshot 注入

因此平台侧只依赖两个输入：

1. `ctx.identity.email`
2. `artifacts.otp_callback`

邮箱 provider 可以来自现有任何可用 mailbox setting，例如：

- `freemail`
- `cfworker`
- `tempmail_lol`
- 其他后续新增 provider

`ATXP` 平台代码不假设某个具体 provider。

### 6.2 Privy 登录阶段

协议流程：

1. `POST https://auth.privy.io/api/v1/passwordless/init`
2. mailbox 收取 OTP
3. `POST https://auth.privy.io/api/v1/passwordless/authenticate`

需要保存：

- `privy_token`
- `refresh_token`

说明：

- `refresh_token` 优先从响应体提取
- 若响应体中缺失，则允许从 cookie jar 中提取

### 6.3 ATXP 主账号初始化阶段

完成 `Privy` 登录后，继续请求：

1. `GET https://accounts.atxp.ai/me`
2. `POST https://accounts.atxp.ai/wallets/ensure`
3. `GET https://accounts.atxp.ai/connection-token`

从中提取：

- `account_id`
- `wallet_address`
- `connection_token`

并组装：

```text
https://accounts.atxp.ai?connection_token=<token>&account_id=<account_id>
```

该值记为：

- `connection_string`

### 6.4 Gateway 探活阶段

使用 `connection_string` 请求：

- `GET https://llm.atxp.ai/v1/models`

记录：

- `gateway_health_alive`
- `gateway_health_model`
- `gateway_health_checked_at`
- `gateway_health`

### 6.5 Clowdbot 任务阶段

在主账号初始化成功后，继续执行：

1. `login_clowdbot_oidc`
2. `create_clowdbot`
3. `claim_email`
4. 汇总 `reward_progress`

保存结果：

- `clowdbot_status`
- `clowdbot_instance_id`
- `claimed_agent_email`
- `create_clowdbot_completed`
- `claim_email_completed`
- `reward_progress`
- `task_error`

## 7. 成功判定与失败恢复

### 7.1 主账号成功条件

只有在以下关键字段全部可得时，才视为 `ATXP` 主账号注册成功：

- `privy_token`
- `account_id`
- `connection_token`
- `connection_string`

若在此之前失败：

- 整次注册失败
- 不入库

### 7.2 主账号成功、Clowdbot 失败

若 `ATXP` 主账号成功，但 `Clowdbot` 任务失败：

- 账号仍然入库
- 不回滚账号
- 失败信息单独写入 `overview`

此时的账号仍是可用账号，只是任务状态不完整。

### 7.3 重试策略

新增平台 action：

```text
retry_clowdbot_tasks
```

重试逻辑：

1. 从已入库账号读取：
   - `privy_token`
   - `account_id`
   - `connection_string`
   - 注册邮箱
2. 只补跑 `Clowdbot` 部分
3. 用最新结果覆盖旧的 `Clowdbot` 状态字段

## 8. 数据落库设计

### 8.1 顶层账号字段

建议映射：

- `platform = "atxp"`
- `email = 注册邮箱`
- `password = mailbox provider 返回的地址密码；若当前 provider 不返回地址密码，则写空字符串`
- `user_id = account_id`
- `primary_token = connection_string`
- `lifecycle_status = "registered"`

说明：

- `ATXP` 主流程本身是邮箱 OTP 登录，不存在平台侧固定密码
- 因此顶层 `password` 字段不再伪造平台密码，只承载 mailbox 侧可回放凭证

### 8.2 平台凭证 `credentials`

建议标准化保存以下 key：

- `privy_token`
- `refresh_token`
- `account_id`
- `connection_token`
- `connection_string`
- `wallet_address`
- `clowdbot_instance_id`
- `claimed_agent_email`

并约定：

- `connection_string` 作为 `atxp` 平台的主凭证

因此需要在 `core/account_graph.py` 中补齐：

1. `PLATFORM_CREDENTIAL_TYPES` 对上述字段的类型映射
2. `PRIMARY_TOKEN_WRITE_KEYS["atxp"] = "connection_string"`

### 8.3 概览 `overview`

建议写入以下摘要字段：

- `gateway_health`
- `gateway_health_alive`
- `gateway_health_model`
- `gateway_health_checked_at`
- `clowdbot_status`
- `create_clowdbot_completed`
- `claim_email_completed`
- `reward_progress`
- `task_error`
- `task_checked_at`
- `atxp_me`
- `wallet_info`
- `clowdbot_result`

原则：

- `credentials` 用于可直接复用的凭证
- `overview` 用于状态摘要和结构化原始返回

### 8.4 mailbox 相关

不新增新的 mailbox 落库结构，继续复用现有：

- `provider_accounts`
- `provider_resources`

也就是说，邮箱 provider 的：

- `mailbox_jwt`
- `address_password`
- `address_id`
- `api_url`
- `auth_mode`

仍然按当前统一模型保存。

## 9. 导出设计

### 9.1 支持格式

`ATXP` 平台支持：

- `json`
- `csv`
- `txt`

不支持：

- `cpa`
- `sub2api`

### 9.2 可选导出字段

建议支持如下字段：

- `email`
- `password`
- `account_id`
- `privy_token`
- `refresh_token`
- `connection_token`
- `connection_string`
- `wallet_address`
- `gateway_health_alive`
- `gateway_health_model`
- `clowdbot_status`
- `clowdbot_instance_id`
- `claimed_agent_email`
- `create_clowdbot_completed`
- `claim_email_completed`
- `reward_progress`
- `task_error`
- `mailbox_jwt`
- `address_password`
- `address_id`
- `api_url`
- `auth_mode`

### 9.3 `txt` 格式

继续沿用当前项目已实现的 `txt` 导出规则：

- 字段之间用 `----` 连接
- 输出顺序与前端勾选顺序一致

## 10. 前端展示设计

### 10.1 详情页

`frontend/src/pages/Accounts.tsx` 中的详情页已经支持：

- `Platform Credentials`
- `Provider Accounts`
- `Provider Resources`

本次在此基础上补充更易读的摘要展示项：

- `Account ID`
- `Wallet Address`
- `Gateway Alive`
- `Gateway Model`
- `Clowdbot Status`
- `Clowdbot Instance ID`
- `Claimed Agent Email`
- `Reward Progress`
- `Task Error`

### 10.2 导出菜单

在现有 `EXPORT_PLATFORM_CONFIGS` 中新增 `atxp` 配置：

- `enabledFormats`
- `fieldGroups`
- `defaultFields`
- `commonPreset`

### 10.3 平台动作

复用现有 `/actions/{platform}` 机制，在 `ATXP` 平台挂出：

- `retry_clowdbot_tasks`

前端无需新建独立动作框架。

## 11. 测试设计

至少补以下测试：

1. `ATXP` result mapper
   - 原始 worker 结果正确映射成 `RegistrationResult`
2. account graph
   - `connection_string` 被识别为主凭证
   - `account_id / wallet_address / claimed_agent_email` 被正确归类
3. 导出
   - `ATXP` 的 `json/csv/txt` 导出字段正确
   - `txt` 按勾选字段顺序输出并使用 `----` 分隔
4. 部分成功场景
   - 主账号成功、`Clowdbot` 失败时仍能入库并保留任务状态
5. 平台动作
   - `retry_clowdbot_tasks` 能读取已有凭证并补跑任务

## 12. 风险与控制

### 12.1 风险

1. `Privy` 与 `Clowdbot` 的协议头部可能有时间敏感性
2. `Clowdbot` 任务流比主注册链路更脆弱
3. 不同 mailbox provider 的 OTP 到达时间差异较大

### 12.2 控制策略

1. `core.py` 中把请求头、cookie jar、OIDC 跳转解析集中封装，避免散落在多个文件
2. 主账号成功后立即返回可落库结果，不把 `Clowdbot` 失败升级为整号失败
3. 通过平台 action 提供任务补跑入口
4. 继续复用现有 OTP callback，不为单平台重复实现 mailbox 轮询

## 13. 实施结果要求

最终交付后，`any-auto-register` 应满足：

1. 用户可在现有系统中选择 `ATXP` 平台发起注册
2. 平台可复用当前 mailbox provider 设置完成收码注册
3. 注册后自动保存：
   - 主账号凭证
   - mailbox 凭证
   - gateway 探活结果
   - Clowdbot 任务状态
4. 账号详情页可查看并复制核心字段
5. 导出菜单可勾选导出 `ATXP` / `Clowdbot` 字段
6. 已入库但任务失败的账号可通过平台动作补跑 `Clowdbot`
