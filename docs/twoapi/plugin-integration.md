# 2API 插件接入指南

本文档给后续 agent 接入新的 2API 平台使用。目标是让每个插件都能复用统一能力：

- OpenAI 兼容 `/models` 与 `/chat/completions` 路由。
- 账号池加载、外部账号导入、远端推送、自动补号。
- 流式响应透传与 SSE 空包心跳。
- 密钥脱敏与测试约束。

当前示例插件：`services/twoapi/plugins/swarms.py`。


## 1. 文件和路由结构

核心文件：

```text
services/twoapi/models.py          # 通用 settings/account dataclass 与脱敏
services/twoapi/importer.py        # 通用外部账号导入框架
services/twoapi/manager.py         # 插件注册、key 管理、补号/导入调度
api/twoapi.py                      # 管理 API 与 OpenAI 兼容代理路由
scripts/run_twoapi_server.py       # 独立 2API server
main.py                            # 主 FastAPI 服务挂载路由
```

新增插件建议放在：

```text
services/twoapi/plugins/<plugin>.py
```

管理路由统一挂载在：

```text
/api/2api/...
```

OpenAI 兼容代理路由建议为：

```text
/<plugin>/v1/models
/<plugin>/v1/chat/completions
/<plugin>/v1/{path_token}/models
/<plugin>/v1/{path_token}/chat/completions
```

`{path_token}` 用于客户端不方便写 `Authorization` 头的场景。


## 2. 插件最小接口

一个 2API 插件至少应提供：

```python
class ExampleTwoAPIPlugin:
    name = "example"
    display_name = "Example OpenAI 兼容代理"

    def load_accounts(self) -> list[TwoAPIAccount]: ...
    def status(self) -> dict[str, Any]: ...
    def recent_logs(self, *, limit: int = 200) -> list[str]: ...
    def forward_models(self) -> Any: ...
    def forward_chat(self, payload: dict[str, Any], *, stream: bool = False) -> Any: ...
    def refresh_credits(self) -> list[TwoAPIAccount]: ...
```

推荐额外支持：

```python
@property
def import_schema(self) -> TwoAPIImportSchema: ...

def import_accounts(self, *, records=None, lines=None, source="external") -> dict: ...

def refill_accounts(self, *, count=1, concurrency=1, executor_type="protocol", extra=None) -> dict: ...
```


## 3. 账号池加载规范

账号应统一转换成 `TwoAPIAccount`：

```python
TwoAPIAccount(
    plugin="example",
    email=email,
    base_url="https://api.example.com/v1",
    api_key=api_key,
    credit_amount=100.0,
    credit_ok=True,
    enabled=True,
    metadata={"source": "account_database"},
)
```

推荐加载来源优先级：

1. `account_manager.db` 中对应 `platform` 的账号图谱凭据。
2. `output/<plugin>_credentials.json`。
3. `output/<plugin>_keys.txt`。
4. 兼容旧脚本产生的结果文件。

注意：

- 不要把完整 key 写入 `status()` 返回。
- 日志必须调用 `mask_secret_in_text()`。
- `to_public()` 会自动脱敏 `api_key` 和敏感 metadata。


## 4. 外部账号导入通用框架

通用导入器在：

```python
from services.twoapi.importer import TwoAPIImportSchema, import_twoapi_accounts
```

插件声明 schema：

```python
EXAMPLE_IMPORT_SCHEMA = TwoAPIImportSchema(
    plugin="example",
    platform="example",
    token_fields=("api_key", "token", "key"),
    email_fields=("email", "account", "username"),
    base_url_fields=("base_url", "openai_base_url"),
    default_base_url="https://api.example.com/v1",
    token_prefixes=("sk-",),
    credential_aliases=("api_key", "token"),
    primary_token_field="api_key",
)
```

插件导入方法：

```python
def import_accounts(self, *, records=None, lines=None, source="external") -> dict:
    result = import_twoapi_accounts(
        self.import_schema,
        records=records,
        lines=lines,
        source=source,
    )
    self.accounts = []
    return result
```

管理 API：

```http
POST /api/2api/plugins/{plugin}/import
```

请求示例：

```json
{
  "source": "manual",
  "lines": [
    "demo@example.com|sk-xxxx|base_url=https://api.example.com/v1"
  ],
  "records": [
    {
      "email": "demo2@example.com",
      "api_key": "sk-yyyy",
      "base_url": "https://api.example.com/v1"
    }
  ]
}
```

返回示例：

```json
{
  "plugin": "example",
  "platform": "example",
  "created": 2,
  "accepted": 2,
  "skipped": 0,
  "errors": []
}
```


## 5. 远端推送规范

如果插件需要把本地账号池推到另一台后端，实现：

```python
def push_accounts(self, *, target_url: str, source="external-push", emails=None, latest_only=False, timeout=30.0) -> dict:
    ...
```

管理 API：

```http
POST /api/2api/plugins/{plugin}/push
```

请求示例：

```json
{
  "target_url": "http://1.2.3.4:8000",
  "source": "frontend-remote-push",
  "emails": [],
  "latest_only": true,
  "timeout": 30
}
```

目标端应暴露同一个导入接口：

```text
/api/2api/plugins/{plugin}/import
```

Zo 与 Swarms 当前已实现该能力；其他插件按需实现。


### Swarms Windows 注册机推送到 Linux 2API

适用场景：Swarms 注册必须在本地 Windows 可视化浏览器完成，但 2API 代理服务部署在 Linux。

流程：

```text
Windows 本机注册 Swarms
  → 写入 output/swarms_credentials.json / output/swarms_keys.txt
  → Windows 后端调用 /api/2api/plugins/swarms/push
  → Linux 后端接收 /api/2api/plugins/swarms/import
  → Linux 2API 使用 /swarms/v1/chat/completions 对外服务
```

Windows 本机发起推送：

```http
POST http://windows-host:8000/api/2api/plugins/swarms/push
Content-Type: application/json
```

```json
{
  "target_url": "http://linux-server:8000",
  "source": "windows-register",
  "latest_only": true,
  "timeout": 30
}
```

如果只推指定账号：

```json
{
  "target_url": "http://linux-server:8000",
  "source": "windows-register",
  "emails": ["account@swarms.test"],
  "latest_only": false
}
```

`target_url` 可以传 Linux 后端根地址，也可以直接传导入接口：

```text
http://linux-server:8000
http://linux-server:8000/api
http://linux-server:8000/api/2api/plugins/swarms/import
```

Linux 端实际接收接口：

```http
POST http://linux-server:8000/api/2api/plugins/swarms/import
```

接收后 Linux 端可以创建 Swarms 专用 2API Key，然后调用：

```http
POST http://linux-server:6543/swarms/v1/chat/completions
Authorization: Bearer <swarms-twoapi-key>
```


## 6. 自动补号规范

如果平台已有注册插件，2API 插件不要重写注册逻辑，应调用现有任务框架：

```python
from application.tasks import create_register_task
from services.task_runtime import task_runtime


def refill_accounts(self, *, count=1, concurrency=1, executor_type="protocol", extra=None) -> dict:
    payload = {
        "platform": self.name,
        "count": count,
        "concurrency": concurrency,
        "executor_type": executor_type,
        "extra": {"twoapi_auto_refill": True, **dict(extra or {})},
    }
    task = create_register_task(payload)
    task_runtime.wake_up()
    return {"ok": True, "task": task, "payload": payload}
```

管理 API：

```http
POST /api/2api/plugins/{plugin}/refill
```

请求示例：

```json
{
  "count": 3,
  "concurrency": 1,
  "executor_type": "protocol",
  "extra": {
    "mail_provider": "luckmail"
  }
}
```

插件选择账号时可以在账号池为空或全部不可用时触发自动补号：

```python
if not self.accounts and self.settings.auto_refill:
    self.refill_accounts(count=1, concurrency=1)
```

注意：补号只创建注册任务，不应阻塞当前 OpenAI 请求等待注册完成。


## 7. `/models` 适配规范

优先返回 OpenAI 模型目录格式：

```json
{
  "object": "list",
  "data": [
    {"id": "model-id", "object": "model", "created": 0, "owned_by": "plugin"}
  ]
}
```

如果上游不是 OpenAI 原生 `/models`，在插件内转换。

Swarms 示例：

- `GET https://api.swarms.world/v1/models` 返回 404。
- 实际模型目录是 `GET https://api.swarms.world/v1/models/available`。
- 插件把 `{ "success": true, "models": [...] }` 转成 OpenAI catalog。


## 8. `/chat/completions` 与流式透传

插件的 `forward_chat()` 应尊重 `stream`：

```python
response = self.transport.post(
    f"{account.base_url}/chat/completions",
    headers=headers,
    json=payload,
    timeout=self.settings.request_timeout,
    stream=stream,
)
return response
```

API 层 `_response_from_upstream(..., stream=True)` 会：

- 原样透传上游 bytes。
- 自动关闭 upstream response。
- 当上游静默超过心跳间隔时发送 SSE comment：

```text
: ping

```

这能避免部分客户端或代理因为长时间无数据而断开。


## 9. 路由接入清单

新增插件后必须修改：

1. `services/twoapi/manager.py`
   - import 插件类。
   - 注册到 `self.plugins`。
   - `listen_urls` 添加插件地址。

2. `api/twoapi.py`
   - 创建 `APIRouter(prefix="/<plugin>/v1")`。
   - 添加 `/models` 和 `/chat/completions` 路由。
   - `_require_key(..., plugin="<plugin>")` 必须使用插件作用域。

3. `main.py`
   - include 新插件 proxy router。

4. `scripts/run_twoapi_server.py`
   - 独立 server 也 include 新插件 proxy router。

5. `tests/`
   - 插件加载账号测试。
   - models 转换测试。
   - chat 转发 endpoint/header 测试。
   - API 路由 key scope 测试。
   - 流式透传测试。


## 10. 验证命令

最小验证：

```powershell
python -m py_compile services\twoapi\plugins\<plugin>.py api\twoapi.py services\twoapi\manager.py scripts\run_twoapi_server.py main.py
```

插件定向测试：

```powershell
python -m pytest tests\test_twoapi_framework.py -k <plugin> -q
python -m pytest tests\test_twoapi_api.py -k <plugin> -q
```

如果平台支持真实 key，建议额外实测：

```text
GET  <upstream>/models 或平台真实模型目录
POST <upstream>/chat/completions
```

实测输出禁止打印完整 key，只打印 preview。


## 11. Swarms 当前事实

Swarms 已接入：

```text
/swarms/v1/models
/swarms/v1/chat/completions
/swarms/v1/{path_token}/models
/swarms/v1/{path_token}/chat/completions
```

上游事实：

```text
POST https://api.swarms.world/v1/chat/completions     # OpenAI 兼容，可用
GET  https://api.swarms.world/v1/models               # 404
GET  https://api.swarms.world/v1/models/available     # 可用，返回 models 数组
```

实测模型：

```text
claude-opus-4-6
```

返回过：

```json
{
  "object": "chat.completion",
  "model": "claude-opus-4-6",
  "choices": [
    {"message": {"content": "OK"}}
  ]
}
```


### Swarms 自动补号落点

Swarms 注册插件在注册成功后会自动导出：

```text
output/swarms_credentials.json
output/swarms_keys.txt
```

因此 `SwarmsTwoAPIPlugin.load_accounts()` 可以在下一次刷新时直接加载新账号。

Swarms 账号池还会过滤明显不合法的短 `sk-*` 测试值，避免把单测/假 key 混入真实轮询。
