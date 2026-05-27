from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.deploy_zo_openai_proxy import (
    build_route_subset,
    load_result_context,
    patch_zo_chat_route,
    sync_routes,
)


class ZoProxyDeployTests(unittest.TestCase):
    def test_patch_zo_chat_route_defines_and_uses_max_tokens(self):
        source = 'import type { Context } from "hono";\n\n// OpenAI-compatible chat completions endpoint, with the API token embedded\n// in the URL path. The Zo platform\'s edge proxy consumes the standard\n// `Authorization` header for its own auth, so we can\'t rely on clients\n// sending it through. Putting the token in the path sidesteps that.\n//\n// Setup in an OpenAI-compatible client (e.g. RikkaHub):\n//   Base URL: https://<your-handle>.zo.space/v1/<your-zo-access-token>\n//   API Key:  anything (unused; the path token is what authenticates)\n//   Model:    "zo" for your default, or "anthropic:claude-opus-4-7" etc.\n//\n// ⚠️ IMPORTANT: Replace PERSONA_ID_PLACEHOLDER below with the persona_id\n// you got from create_persona() when installing this on your own Zo.\n\nconst PERSONA_ID = "PERSONA_ID_PLACEHOLDER";\n\ntype ChatMessage = {\n  role: "system" | "user" | "assistant" | "tool";\n  content: string | Array<{ type: string; text?: string }>;\n};\n\ntype ChatRequest = {\n  model?: string;\n  messages: ChatMessage[];\n  stream?: boolean;\n  temperature?: number;\n  max_tokens?: number;\n  max_completion_tokens?: number;\n  top_p?: number;\n  frequency_penalty?: number;\n  presence_penalty?: number;\n  stop?: string | string[];\n  n?: number;\n  user?: string;\n  logit_bias?: Record<string, number>;\n  response_format?: unknown;\n  seed?: number;\n  tools?: unknown;\n  tool_choice?: unknown;\n  functions?: unknown;\n  function_call?: unknown;\n};\n\nfunction extractText(content: ChatMessage["content"]): string {\n  if (typeof content === "string") return content;\n  if (!Array.isArray(content)) return "";\n  return content\n    .map((part) => {\n      if (typeof part === "string") return part;\n      if (part && typeof part === "object") {\n        if ((part as any).type === "text" && typeof (part as any).text === "string") {\n          return (part as any).text;\n        }\n        if ((part as any).type === "image_url") return "";\n      }\n      return "";\n    })\n    .filter(Boolean)\n    .join("\\n");\n}\n\nfunction messagesToPrompt(messages: ChatMessage[]): string {\n  const systemParts: string[] = [];\n  const convo: string[] = [];\n\n  for (const msg of messages) {\n    const text = extractText(msg.content).trim();\n    if (!text) continue;\n    if (msg.role === "system") {\n      systemParts.push(text);\n    } else if (msg.role === "assistant") {\n      convo.push(`Assistant: ${text}`);\n    } else if (msg.role === "tool") {\n      convo.push(`Tool result: ${text}`);\n    } else {\n      convo.push(`User: ${text}`);\n    }\n  }\n\n  const systemBlock =\n    systemParts.length > 0\n      ? `System instructions:\\n${systemParts.join("\\n\\n")}\\n\\n---\\n\\n`\n      : "";\n  return systemBlock + convo.join("\\n\\n");\n}\n\nfunction isValidZoModel(model?: string): string | undefined {\n  if (!model) return undefined;\n  if (/^[a-z0-9_-]+:[a-zA-Z0-9._\\/-]+$/.test(model)) return model;\n  return undefined;\n}\n\nfunction makeId() {\n  return "chatcmpl-" + Math.random().toString(36).slice(2, 12);\n}\n\nfunction nonStreamingResponse(modelName: string, content: string) {\n  return {\n    id: makeId(),\n    object: "chat.completion",\n    created: Math.floor(Date.now() / 1000),\n    model: modelName,\n    choices: [\n      {\n        index: 0,\n        message: { role: "assistant", content },\n        finish_reason: "stop",\n      },\n    ],\n    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },\n  };\n}\n\nfunction streamChunk(id: string, modelName: string, delta: object, finish: string | null = null) {\n  return (\n    "data: " +\n    JSON.stringify({\n      id,\n      object: "chat.completion.chunk",\n      created: Math.floor(Date.now() / 1000),\n      model: modelName,\n      choices: [{ index: 0, delta, finish_reason: finish }],\n    }) +\n    "\\n\\n"\n  );\n}\n\nexport default async (c: Context) => {\n  const token = (c.req.param("token") || "").trim();\n  if (!token) {\n    return c.json(\n      { error: { message: "Missing token in URL path", type: "auth_error" } },\n      401,\n    );\n  }\n\n  // === Coding-agent denylist ===\n  // This endpoint is for AIRP (chat / roleplay) only. Block well-known coding\n  // assistants and agent tools by User-Agent so they can\'t burn the operator\'s\n  // Zo balance running coding workflows.\n  //\n  // EXPLICITLY ALLOWED (these UAs do NOT match any pattern below, verified by test):\n  //   • RikkaHub                — Android chat client\n  //   • SillyTavern             — official web/desktop\n  //   • TauriTavern             — SillyTavern rewritten in Tauri/Rust (Darkatse/TauriTavern)\n  //   • ChatBox, LobeChat       — general OpenAI-compatible chat UIs\n  //   • Mozilla/Chrome/Safari   — direct browser usage\n  //   • reqwest/curl/python     — generic HTTP libraries used by user scripts\n  const ua = (c.req.header("user-agent") || "").toLowerCase();\n  const BLOCKED_UA_PATTERNS = [\n    /claude[-_ ]?code/,       // Anthropic Claude Code CLI\n    /codex[-_ ]?cli/,         // OpenAI Codex CLI\n    /openai[-_ ]?codex/,\n    /\\bcursor\\b/,             // Cursor IDE\n    /windsurf/,               // Codeium Windsurf\n    /\\bcline\\b/,              // Cline VSCode extension\n    /\\baider\\b/,              // Aider\n    /continue[-_ ]?(dev|cli)/,// Continue.dev\n    /gpt-engineer/,\n    /lobster/, /long[-_ ]?xia/, // "龙虾" English/pinyin variants\n    /\\btrae\\b/,               // ByteDance Trae IDE\n    /void[-_ ]?editor/,       // Void Editor\n    /devin/,                  // Cognition Devin\n  ];\n  if (BLOCKED_UA_PATTERNS.some((p) => p.test(ua))) {\n    return c.json(\n      {\n        error: {\n          message:\n            "本渠道仅支持聊天/角色扮演用途,不支持代码助手、Agent 或自动化工具。/ This channel is for chat & roleplay only — coding agents are not supported.",\n          type: "channel_restriction",\n          code: "coding_agent_blocked",\n        },\n      },\n      403,\n    );\n  }\n\n  let body: ChatRequest;\n  try {\n    body = await c.req.json();\n  } catch {\n    return c.json(\n      { error: { message: "Invalid JSON body", type: "invalid_request_error" } },\n      400,\n    );\n  }\n\n  if (!body?.messages || !Array.isArray(body.messages) || body.messages.length === 0) {\n    return c.json(\n      { error: { message: "`messages` is required", type: "invalid_request_error" } },\n      400,\n    );\n  }\n\n  const prompt = messagesToPrompt(body.messages);\n  const requestedModel = body.model || "zo";\n  const zoModel = isValidZoModel(body.model);\n  const wantStream = body.stream === true;\n\n  const upstreamPayload: Record<string, unknown> = {\n    input: prompt,\n    persona_id: "PERSONA_ID_PLACEHOLDER",\n  };\n  if (zoModel) upstreamPayload.model_name = zoModel;\n  if (wantStream) upstreamPayload.stream = true;\n  // Pass max_tokens through (default 64000 for AIRP use)\n  upstreamPayload.max_tokens = maxTokens;\n\n  // Kick off the upstream fetch immediately, but DON\'T await it here when\n  // streaming. The ReadableStream below will own it. This lets us flush the\n  // response headers + the initial role chunk to the client before the\n  // upstream model has even started responding.\n  const upstreamPromise = fetch("https://api.zo.computer/zo/ask", {\n    method: "POST",\n    headers: {\n      Authorization: `Bearer ${token}`,\n      "Content-Type": "application/json",\n      Accept: wantStream ? "text/event-stream" : "application/json",\n    },\n    body: JSON.stringify(upstreamPayload),\n  });\n\n  if (!wantStream) {\n    const upstream = await upstreamPromise;\n    if (!upstream.ok) {\n      const errText = await upstream.text();\n      return c.json(\n        {\n          error: {\n            message: `Zo API error (${upstream.status}): ${errText.slice(0, 500)}`,\n            type: "upstream_error",\n            code: upstream.status,\n          },\n        },\n        upstream.status as 400 | 401 | 403 | 404 | 429 | 500,\n      );\n    }\n    const data = (await upstream.json()) as { output?: unknown };\n    const out =\n      typeof data?.output === "string"\n        ? data.output\n        : JSON.stringify(data?.output ?? data);\n    return c.json(nonStreamingResponse(requestedModel, out));\n  }\n\n  const id = makeId();\n  const encoder = new TextEncoder();\n  const decoder = new TextDecoder();\n\n  const stream = new ReadableStream({\n    async start(controller) {\n      const upstream = await upstreamPromise;\n      if (!upstream.ok) {\n        const errText = await upstream.text().catch(() => "");\n        // Emit role chunk first so OpenAI-compatible clients show the message bubble.\n        controller.enqueue(\n          encoder.encode(streamChunk(id, requestedModel, { role: "assistant" })),\n        );\n        controller.enqueue(\n          encoder.encode(\n            streamChunk(\n              id,\n              requestedModel,\n              { content: `\\n[upstream error ${upstream.status}: ${errText.slice(0, 300)}]` },\n              "stop",\n            ),\n          ),\n        );\n        controller.enqueue(encoder.encode("data: [DONE]\\n\\n"));\n        controller.close();\n        return;\n      }\n\n      // Emit the role frame only after upstream is ready — sending tiny\n      // packets before upstream produces data caused intermediaries\n      // (CDN / cellular networks) to buffer the entire stream.\n      controller.enqueue(\n        encoder.encode(streamChunk(id, requestedModel, { role: "assistant" })),\n      );\n\n      const reader = upstream.body?.getReader();\n      if (!reader) {\n        controller.enqueue(encoder.encode(streamChunk(id, requestedModel, {}, "stop")));\n        controller.enqueue(encoder.encode("data: [DONE]\\n\\n"));\n        controller.close();\n        return;\n      }\n\n      let buffer = "";\n      let finished = false;\n      // Once we\'ve emitted the terminating chunks, skip emitting them again\n      // from the post-loop tail (e.g. if the streaming loop threw).\n      let sentFinish = false;\n      let textPreambleHandled = false;\n      let textPreambleBuf = "";\n      try {\n        while (true) {\n          const { value, done } = await reader.read();\n          if (done) break;\n          buffer += decoder.decode(value, { stream: true });\n\n          const events = buffer.split(/\\r?\\n\\r?\\n/);\n          buffer = events.pop() ?? "";\n\n          for (const evt of events) {\n            const lines = evt.split(/\\r?\\n/);\n            let eventType = "";\n            const dataLines: string[] = [];\n            for (const l of lines) {\n              if (l.startsWith("event:")) {\n                eventType = l.slice(6).trim();\n              } else if (l.startsWith("data:")) {\n                dataLines.push(l.slice(5).trim());\n              }\n            }\n            if (dataLines.length === 0) continue;\n            const data = dataLines.join("\\n");\n            if (data === "[DONE]") {\n              finished = true;\n              continue;\n            }\n\n            let parsed: any;\n            try {\n              parsed = JSON.parse(data);\n            } catch {\n              continue;\n            }\n\n            let textDelta = "";\n            let thinkingDelta = "";\n            if (eventType === "PartStartEvent") {\n              const kind = parsed?.part?.part_kind;\n              const content = parsed?.part?.content;\n              if (typeof content === "string" && content.length > 0) {\n                if (kind === "thinking") thinkingDelta = content;\n                else if (kind === "text") textDelta = content;\n              }\n            } else if (eventType === "PartDeltaEvent") {\n              const kind = parsed?.delta?.part_delta_kind;\n              const content = parsed?.delta?.content_delta;\n              if (typeof content === "string" && content.length > 0) {\n                if (kind === "thinking") thinkingDelta = content;\n                else if (kind === "text") textDelta = content;\n              }\n            } else if (eventType === "End") {\n              finished = true;\n            }\n\n            if (thinkingDelta.length > 0) {\n              controller.enqueue(\n                encoder.encode(\n                  streamChunk(id, requestedModel, { reasoning_content: thinkingDelta }),\n                ),\n              );\n            }\n            if (textDelta.length > 0) {\n              if (textPreambleHandled) {\n                controller.enqueue(\n                  encoder.encode(streamChunk(id, requestedModel, { content: textDelta })),\n                );\n              } else {\n                textPreambleBuf += textDelta;\n                const m = textPreambleBuf.match(/^\\s*\\[[^\\]\\n]{0,60}\\]\\s*/);\n                if (m) {\n                  controller.enqueue(\n                    encoder.encode(\n                      streamChunk(id, requestedModel, { reasoning_content: m[0] }),\n                    ),\n                  );\n                  const rest = textPreambleBuf.slice(m[0].length);\n                  if (rest.length > 0) {\n                    controller.enqueue(\n                      encoder.encode(streamChunk(id, requestedModel, { content: rest })),\n                    );\n                  }\n                  textPreambleBuf = "";\n                  textPreambleHandled = true;\n                } else if (\n                  textPreambleBuf.length > 80 ||\n                  (textPreambleBuf.length > 1 && !/^\\s*\\[/.test(textPreambleBuf))\n                ) {\n                  controller.enqueue(\n                    encoder.encode(streamChunk(id, requestedModel, { content: textPreambleBuf })),\n                  );\n                  textPreambleBuf = "";\n                  textPreambleHandled = true;\n                }\n              }\n            }\n          }\n          if (finished) break;\n        }\n      } catch (err) {\n        controller.enqueue(\n          encoder.encode(\n            streamChunk(\n              id,\n              requestedModel,\n              { content: `\\n[proxy error: ${(err as Error).message}]` },\n              "stop",\n            ),\n          ),\n        );\n        controller.enqueue(encoder.encode("data: [DONE]\\n\\n"));\n        controller.close();\n        sentFinish = true;\n      }\n\n      if (sentFinish) return;\n\n      if (!textPreambleHandled && textPreambleBuf.length > 0) {\n        controller.enqueue(\n          encoder.encode(streamChunk(id, requestedModel, { content: textPreambleBuf })),\n        );\n      }\n\n      controller.enqueue(encoder.encode(streamChunk(id, requestedModel, {}, "stop")));\n      controller.enqueue(encoder.encode("data: [DONE]\\n\\n"));\n      controller.close();\n    },\n  });\n\n  return new Response(stream, {\n    headers: {\n      "Content-Type": "text/event-stream",\n      "Cache-Control": "no-cache",\n      Connection: "keep-alive",\n    },\n  });\n};\n'
        patched = patch_zo_chat_route(source, "persona-123")
        self.assertNotIn("PERSONA_ID_PLACEHOLDER", patched)
        self.assertIn('const PERSONA_ID = "persona-123";', patched)
        self.assertIn('persona_id: PERSONA_ID', patched)
        self.assertIn('const maxTokens =', patched)
        self.assertIn('upstreamPayload.max_tokens = maxTokens;', patched)

    def test_build_route_subset_contains_four_public_api_routes(self):
        source_dir = Path("artifacts") / "zo_openai_proxy_source"
        routes = build_route_subset(source_dir, "persona-xyz")
        self.assertEqual([item["path"] for item in routes], [
            "/v1/:token/chat/completions",
            "/v1/:token/models",
            "/anthropic/:apikey/v1/chat/completions",
            "/anthropic/:apikey/v1/models",
        ])
        self.assertTrue(all(item["type"] == "api" and item["public"] is True for item in routes))
        self.assertTrue(any("persona-xyz" in item["code"] for item in routes))

    def test_load_result_context_derives_handle_from_login_state_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "zo_e2e_result.json"
            result_path.write_text(json.dumps({
                "api_key": "zo_sk_demo",
                "cookies": {"access_token": "jwt", "refresh_token": "rt"},
                "workspace_result": {
                    "workspace": {
                        "handle": "demo123",
                        "origin": "https://demo123.zo.computer",
                    },
                },
            }), encoding="utf-8")
            ctx = load_result_context(result_path)
        self.assertEqual(ctx.handle, "demo123")
        self.assertEqual(ctx.workspace_origin, "https://demo123.zo.computer")
        self.assertEqual(ctx.api_key, "zo_sk_demo")

    def test_patch_zo_chat_route_uses_current_direct_ask_protocol(self):
        source = (Path("artifacts") / "zo_openai_proxy_source" / "routes" / "zo-chat-completions.ts").read_text(encoding="utf-8")
        patched = patch_zo_chat_route(
            source,
            "persona-123",
            access_token="access-demo",
            workspace_origin="https://demo.zo.computer",
            workspace_handle="demo",
        )

        self.assertIn('const EMBEDDED_ACCESS_TOKEN = "access-demo";', patched)
        self.assertIn('const WORKSPACE_ORIGIN = "https://demo.zo.computer";', patched)
        self.assertIn('const WORKSPACE_HANDLE = "demo";', patched)
        self.assertIn('fetch("https://api.zo.computer/ask"', patched)
        self.assertNotIn('https://api.zo.computer/zo/ask', patched)
        self.assertIn('q: prompt', patched)
        self.assertIn('mode: "chat"', patched)
        self.assertIn('context_paths: []', patched)
        self.assertIn('command_paths: []', patched)
        self.assertIn('expanded_paths: []', patched)
        self.assertIn('"x-zo-streaming-version": "2"', patched)
        self.assertIn('"X-Zo-Workspace-Origin": WORKSPACE_ORIGIN', patched)
        self.assertIn('"x-zo-host-key": WORKSPACE_HANDLE', patched)
        self.assertIn('extractZoSseOutput', patched)

    def test_build_route_subset_can_embed_direct_ask_context_without_writing_source(self):
        source_dir = Path("artifacts") / "zo_openai_proxy_source"
        ctx = load_result_context(Path("tests") / "fixtures" / "zo_proxy_context.json") if False else None
        routes = build_route_subset(
            source_dir,
            "persona-xyz",
            access_token="access-demo",
            workspace_origin="https://demo.zo.computer",
            workspace_handle="demo",
        )
        chat_route = next(item for item in routes if item["path"] == "/v1/:token/chat/completions")
        self.assertIn('const EMBEDDED_ACCESS_TOKEN = "access-demo";', chat_route["code"])
        source = (source_dir / "routes" / "zo-chat-completions.ts").read_text(encoding="utf-8")
        self.assertNotIn("access-demo", source)

    def test_sync_routes_uses_mcp_write_space_route_when_api_key_available(self):
        class FakeResponse:
            status_code = 200
            ok = True
            text = '{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"ok"}],"isError":false}}'

            def json(self):
                return json.loads(self.text)

        class FakeSession:
            def __init__(self):
                self.calls = []

            def post(self, url, headers=None, json=None, timeout=None):
                self.calls.append({"url": url, "headers": headers or {}, "json": json or {}, "timeout": timeout})
                return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "zo_e2e_result.json"
            result_path.write_text(json.dumps({
                "api_key": "zo_sk_demo",
                "cookies": {"access_token": "jwt"},
                "workspace_result": {"workspace": {"handle": "demo", "origin": "https://demo.zo.computer"}},
            }), encoding="utf-8")
            ctx = load_result_context(result_path)
        session = FakeSession()
        result = sync_routes(ctx, [{"path": "/v1/:token/models", "type": "api", "public": True, "code": "export default () => new Response('ok')"}], session=session)

        self.assertTrue(result["ok"])
        self.assertEqual(session.calls[0]["url"], "https://api.zo.computer/mcp")
        payload = session.calls[0]["json"]
        self.assertEqual(payload["method"], "tools/call")
        self.assertEqual(payload["params"]["name"], "write_space_route")
        self.assertEqual(payload["params"]["arguments"]["route_type"], "api")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer zo_sk_demo")
        self.assertNotIn("/exec", repr(session.calls))


if __name__ == "__main__":
    unittest.main()
