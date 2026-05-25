import type { Context } from "hono";

// OpenAI-compatible chat completions endpoint, with the API token embedded
// in the URL path. The Zo platform's edge proxy consumes the standard
// `Authorization` header for its own auth, so we can't rely on clients
// sending it through. Putting the token in the path sidesteps that.
//
// Setup in an OpenAI-compatible client (e.g. RikkaHub):
//   Base URL: https://<your-handle>.zo.space/v1/<your-zo-access-token>
//   API Key:  anything (unused; the path token is what authenticates)
//   Model:    "zo" for your default, or "anthropic:claude-opus-4-7" etc.
//
// ⚠️ IMPORTANT: Replace PERSONA_ID_PLACEHOLDER below with the persona_id
// you got from create_persona() when installing this on your own Zo.

const PERSONA_ID = "PERSONA_ID_PLACEHOLDER";

type ChatMessage = {
  role: "system" | "user" | "assistant" | "tool";
  content: string | Array<{ type: string; text?: string }>;
};

type ChatRequest = {
  model?: string;
  messages: ChatMessage[];
  stream?: boolean;
  temperature?: number;
  max_tokens?: number;
  max_completion_tokens?: number;
  top_p?: number;
  frequency_penalty?: number;
  presence_penalty?: number;
  stop?: string | string[];
  n?: number;
  user?: string;
  logit_bias?: Record<string, number>;
  response_format?: unknown;
  seed?: number;
  tools?: unknown;
  tool_choice?: unknown;
  functions?: unknown;
  function_call?: unknown;
};

function extractText(content: ChatMessage["content"]): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      if (typeof part === "string") return part;
      if (part && typeof part === "object") {
        if ((part as any).type === "text" && typeof (part as any).text === "string") {
          return (part as any).text;
        }
        if ((part as any).type === "image_url") return "";
      }
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

function messagesToPrompt(messages: ChatMessage[]): string {
  const systemParts: string[] = [];
  const convo: string[] = [];

  for (const msg of messages) {
    const text = extractText(msg.content).trim();
    if (!text) continue;
    if (msg.role === "system") {
      systemParts.push(text);
    } else if (msg.role === "assistant") {
      convo.push(`Assistant: ${text}`);
    } else if (msg.role === "tool") {
      convo.push(`Tool result: ${text}`);
    } else {
      convo.push(`User: ${text}`);
    }
  }

  const systemBlock =
    systemParts.length > 0
      ? `System instructions:\n${systemParts.join("\n\n")}\n\n---\n\n`
      : "";
  return systemBlock + convo.join("\n\n");
}

function isValidZoModel(model?: string): string | undefined {
  if (!model) return undefined;
  if (/^[a-z0-9_-]+:[a-zA-Z0-9._\/-]+$/.test(model)) return model;
  return undefined;
}

function makeId() {
  return "chatcmpl-" + Math.random().toString(36).slice(2, 12);
}

function nonStreamingResponse(modelName: string, content: string) {
  return {
    id: makeId(),
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: modelName,
    choices: [
      {
        index: 0,
        message: { role: "assistant", content },
        finish_reason: "stop",
      },
    ],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

function streamChunk(id: string, modelName: string, delta: object, finish: string | null = null) {
  return (
    "data: " +
    JSON.stringify({
      id,
      object: "chat.completion.chunk",
      created: Math.floor(Date.now() / 1000),
      model: modelName,
      choices: [{ index: 0, delta, finish_reason: finish }],
    }) +
    "\n\n"
  );
}

export default async (c: Context) => {
  const token = (c.req.param("token") || "").trim();
  if (!token) {
    return c.json(
      { error: { message: "Missing token in URL path", type: "auth_error" } },
      401,
    );
  }

  // === Coding-agent denylist ===
  // This endpoint is for AIRP (chat / roleplay) only. Block well-known coding
  // assistants and agent tools by User-Agent so they can't burn the operator's
  // Zo balance running coding workflows.
  //
  // EXPLICITLY ALLOWED (these UAs do NOT match any pattern below, verified by test):
  //   • RikkaHub                — Android chat client
  //   • SillyTavern             — official web/desktop
  //   • TauriTavern             — SillyTavern rewritten in Tauri/Rust (Darkatse/TauriTavern)
  //   • ChatBox, LobeChat       — general OpenAI-compatible chat UIs
  //   • Mozilla/Chrome/Safari   — direct browser usage
  //   • reqwest/curl/python     — generic HTTP libraries used by user scripts
  const ua = (c.req.header("user-agent") || "").toLowerCase();
  const BLOCKED_UA_PATTERNS = [
    /claude[-_ ]?code/,       // Anthropic Claude Code CLI
    /codex[-_ ]?cli/,         // OpenAI Codex CLI
    /openai[-_ ]?codex/,
    /\bcursor\b/,             // Cursor IDE
    /windsurf/,               // Codeium Windsurf
    /\bcline\b/,              // Cline VSCode extension
    /\baider\b/,              // Aider
    /continue[-_ ]?(dev|cli)/,// Continue.dev
    /gpt-engineer/,
    /lobster/, /long[-_ ]?xia/, // "龙虾" English/pinyin variants
    /\btrae\b/,               // ByteDance Trae IDE
    /void[-_ ]?editor/,       // Void Editor
    /devin/,                  // Cognition Devin
  ];
  if (BLOCKED_UA_PATTERNS.some((p) => p.test(ua))) {
    return c.json(
      {
        error: {
          message:
            "本渠道仅支持聊天/角色扮演用途,不支持代码助手、Agent 或自动化工具。/ This channel is for chat & roleplay only — coding agents are not supported.",
          type: "channel_restriction",
          code: "coding_agent_blocked",
        },
      },
      403,
    );
  }

  let body: ChatRequest;
  try {
    body = await c.req.json();
  } catch {
    return c.json(
      { error: { message: "Invalid JSON body", type: "invalid_request_error" } },
      400,
    );
  }

  if (!body?.messages || !Array.isArray(body.messages) || body.messages.length === 0) {
    return c.json(
      { error: { message: "`messages` is required", type: "invalid_request_error" } },
      400,
    );
  }

  const prompt = messagesToPrompt(body.messages);
  const requestedModel = body.model || "zo";
  const zoModel = isValidZoModel(body.model);
  const wantStream = body.stream === true;

  const upstreamPayload: Record<string, unknown> = {
    input: prompt,
    persona_id: "PERSONA_ID_PLACEHOLDER",
  };
  if (zoModel) upstreamPayload.model_name = zoModel;
  if (wantStream) upstreamPayload.stream = true;
  // Pass max_tokens through (default 64000 for AIRP use)
  upstreamPayload.max_tokens = maxTokens;

  // Kick off the upstream fetch immediately, but DON'T await it here when
  // streaming. The ReadableStream below will own it. This lets us flush the
  // response headers + the initial role chunk to the client before the
  // upstream model has even started responding.
  const upstreamPromise = fetch("https://api.zo.computer/zo/ask", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: wantStream ? "text/event-stream" : "application/json",
    },
    body: JSON.stringify(upstreamPayload),
  });

  if (!wantStream) {
    const upstream = await upstreamPromise;
    if (!upstream.ok) {
      const errText = await upstream.text();
      return c.json(
        {
          error: {
            message: `Zo API error (${upstream.status}): ${errText.slice(0, 500)}`,
            type: "upstream_error",
            code: upstream.status,
          },
        },
        upstream.status as 400 | 401 | 403 | 404 | 429 | 500,
      );
    }
    const data = (await upstream.json()) as { output?: unknown };
    const out =
      typeof data?.output === "string"
        ? data.output
        : JSON.stringify(data?.output ?? data);
    return c.json(nonStreamingResponse(requestedModel, out));
  }

  const id = makeId();
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  const stream = new ReadableStream({
    async start(controller) {
      const upstream = await upstreamPromise;
      if (!upstream.ok) {
        const errText = await upstream.text().catch(() => "");
        // Emit role chunk first so OpenAI-compatible clients show the message bubble.
        controller.enqueue(
          encoder.encode(streamChunk(id, requestedModel, { role: "assistant" })),
        );
        controller.enqueue(
          encoder.encode(
            streamChunk(
              id,
              requestedModel,
              { content: `\n[upstream error ${upstream.status}: ${errText.slice(0, 300)}]` },
              "stop",
            ),
          ),
        );
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
        return;
      }

      // Emit the role frame only after upstream is ready — sending tiny
      // packets before upstream produces data caused intermediaries
      // (CDN / cellular networks) to buffer the entire stream.
      controller.enqueue(
        encoder.encode(streamChunk(id, requestedModel, { role: "assistant" })),
      );

      const reader = upstream.body?.getReader();
      if (!reader) {
        controller.enqueue(encoder.encode(streamChunk(id, requestedModel, {}, "stop")));
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
        return;
      }

      let buffer = "";
      let finished = false;
      // Once we've emitted the terminating chunks, skip emitting them again
      // from the post-loop tail (e.g. if the streaming loop threw).
      let sentFinish = false;
      let textPreambleHandled = false;
      let textPreambleBuf = "";
      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const events = buffer.split(/\r?\n\r?\n/);
          buffer = events.pop() ?? "";

          for (const evt of events) {
            const lines = evt.split(/\r?\n/);
            let eventType = "";
            const dataLines: string[] = [];
            for (const l of lines) {
              if (l.startsWith("event:")) {
                eventType = l.slice(6).trim();
              } else if (l.startsWith("data:")) {
                dataLines.push(l.slice(5).trim());
              }
            }
            if (dataLines.length === 0) continue;
            const data = dataLines.join("\n");
            if (data === "[DONE]") {
              finished = true;
              continue;
            }

            let parsed: any;
            try {
              parsed = JSON.parse(data);
            } catch {
              continue;
            }

            let textDelta = "";
            let thinkingDelta = "";
            if (eventType === "PartStartEvent") {
              const kind = parsed?.part?.part_kind;
              const content = parsed?.part?.content;
              if (typeof content === "string" && content.length > 0) {
                if (kind === "thinking") thinkingDelta = content;
                else if (kind === "text") textDelta = content;
              }
            } else if (eventType === "PartDeltaEvent") {
              const kind = parsed?.delta?.part_delta_kind;
              const content = parsed?.delta?.content_delta;
              if (typeof content === "string" && content.length > 0) {
                if (kind === "thinking") thinkingDelta = content;
                else if (kind === "text") textDelta = content;
              }
            } else if (eventType === "End") {
              finished = true;
            }

            if (thinkingDelta.length > 0) {
              controller.enqueue(
                encoder.encode(
                  streamChunk(id, requestedModel, { reasoning_content: thinkingDelta }),
                ),
              );
            }
            if (textDelta.length > 0) {
              if (textPreambleHandled) {
                controller.enqueue(
                  encoder.encode(streamChunk(id, requestedModel, { content: textDelta })),
                );
              } else {
                textPreambleBuf += textDelta;
                const m = textPreambleBuf.match(/^\s*\[[^\]\n]{0,60}\]\s*/);
                if (m) {
                  controller.enqueue(
                    encoder.encode(
                      streamChunk(id, requestedModel, { reasoning_content: m[0] }),
                    ),
                  );
                  const rest = textPreambleBuf.slice(m[0].length);
                  if (rest.length > 0) {
                    controller.enqueue(
                      encoder.encode(streamChunk(id, requestedModel, { content: rest })),
                    );
                  }
                  textPreambleBuf = "";
                  textPreambleHandled = true;
                } else if (
                  textPreambleBuf.length > 80 ||
                  (textPreambleBuf.length > 1 && !/^\s*\[/.test(textPreambleBuf))
                ) {
                  controller.enqueue(
                    encoder.encode(streamChunk(id, requestedModel, { content: textPreambleBuf })),
                  );
                  textPreambleBuf = "";
                  textPreambleHandled = true;
                }
              }
            }
          }
          if (finished) break;
        }
      } catch (err) {
        controller.enqueue(
          encoder.encode(
            streamChunk(
              id,
              requestedModel,
              { content: `\n[proxy error: ${(err as Error).message}]` },
              "stop",
            ),
          ),
        );
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
        sentFinish = true;
      }

      if (sentFinish) return;

      if (!textPreambleHandled && textPreambleBuf.length > 0) {
        controller.enqueue(
          encoder.encode(streamChunk(id, requestedModel, { content: textPreambleBuf })),
        );
      }

      controller.enqueue(encoder.encode(streamChunk(id, requestedModel, {}, "stop")));
      controller.enqueue(encoder.encode("data: [DONE]\n\n"));
      controller.close();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
};
