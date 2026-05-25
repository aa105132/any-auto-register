# Zo OpenAI-Compatible Proxy

把你的 Zo Computer 变成一个 OpenAI 兼容的 API 端点,可以在 **RikkaHub / 酒馆 (SillyTavern) / Chatbox** 等任何支持 "Custom OpenAI" 的客户端里直接调用 Zo 的所有模型。

## 功能
- ✅ **走 Zo 余额** 调用 Claude Opus / GPT-5 / Gemini / DeepSeek 等所有 Zo 支持的模型
- ✅ **思维链流式输出**(reasoning_content,DeepSeek-R1 风格,客户端会渲染成可折叠块)
- ✅ **完美兼容角色卡 / 预设**(passthrough persona 压制 Zo 自带身份)
- ✅ **自动剥离 `[xxx accept]` 类预设确认标记** 到思维链
- ✅ **动态拉取模型列表**(包含 11 个官方 + 4 个隐藏可用的模型)
- ✅ **可选直连 Anthropic**(用你自己的 Anthropic key,零提示词污染)

---

## 🤖 给 Zo 看的安装说明

**如果你是另一个 Zo 用户,把这个 README 发给你的 Zo,说"按这个 README 在我的 zo.space 上部署 OpenAI 代理"。** 你的 Zo 会自动执行下面这些步骤。

### Step 1: 创建 persona

调用 `create_persona`:
- name: `API Passthrough`
- prompt: 见本仓库的 `persona/api-passthrough.md`(或下文 PERSONA PROMPT 段)

然后调用 `set_persona_scopes(persona_id, [])` 把工具权限清空(这个 persona 只用于代理,不需要任何工具)。

**记下返回的 `persona_id`**,后面 Step 2 的代码里要替换。

### Step 2: 创建 4 个 zo.space 路由

依次调用 `write_space_route`,代码在本仓库的 `routes/` 目录下:

| 路由路径 | 类型 | 源文件 |
|---|---|---|
| `/v1/:token/chat/completions` | api | `routes/zo-chat-completions.ts` |
| `/v1/:token/models` | api | `routes/zo-models.ts` |
| `/anthropic/:apikey/v1/chat/completions` | api | `routes/anthropic-chat-completions.ts` |
| `/anthropic/:apikey/v1/models` | api | `routes/anthropic-models.ts` |

⚠️ **关键步骤**:在 `zo-chat-completions.ts` 里把这一行的 persona_id 替换成 Step 1 拿到的 ID:
```ts
persona_id: "dad35669-fe0e-4d29-9201-e197f631ebbc",  // 改成你刚创建的 persona_id
```

### Step 3: 告诉用户怎么配置客户端

完成后,给用户报告以下信息让他配置 RikkaHub / 酒馆。

---

## 👤 用户配置(部署完成后看这里)

### 拿一个 Zo Access Token
[Settings > Access Tokens](/?t=settings&s=advanced) → 新建一个 token,复制完整字符串(类似 `zo_sk_xxxxx`)。

### RikkaHub / 酒馆 / Chatbox 配置

| 字段 | 值 |
|---|---|
| **API 类型** | OpenAI 兼容 (Custom OpenAI) |
| **Base URL** | `https://<你的handle>.zo.space/v1/<你的zo-token>` |
| **API Key** | 随便填(例如 `unused`,不会用到) |
| **Model** | `zo:anthropic/claude-opus-4-7` 等(列表见 `/v1/<token>/models`) |
| **Streaming** | 开启 |
| **Max Output Tokens** | 64000 |
| **Context Size** | 1000000 |

### 直连 Anthropic 模式(可选)
如果你有自己的 Anthropic API key 想完全跳过 Zo 提示词:

| 字段 | 值 |
|---|---|
| **Base URL** | `https://<你的handle>.zo.space/anthropic/<sk-ant-xxx>/v1` |
| **Model** | `claude-opus-4-7` / `claude-opus-4-6` etc. |

---

## 📂 文件清单
```
zo-openai-proxy/
├── README.md                                   ← 你正在看
├── persona/
│   └── api-passthrough.md                      ← persona prompt
└── routes/
    ├── zo-chat-completions.ts                  ← 主代理(走 Zo)
    ├── zo-models.ts                            ← 模型列表
    ├── anthropic-chat-completions.ts           ← 直连 Anthropic
    └── anthropic-models.ts                     ← Anthropic 模型列表
```
