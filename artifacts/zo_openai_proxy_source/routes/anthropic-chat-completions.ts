import type { Context } from "hono";

// TRUE raw proxy: OpenAI-compatible chat completions that forwards directly
// to Anthropic's Messages API. ZERO system prompt is injected by this proxy
// or by Zo — only what the client sends ends up in front of the model.
// Use this when your client (SillyTavern, RikkaHub, etc.) already has its
// own preset / character card and you do not want Zo's identity bleeding in.
//
// Setup:
//   Base URL: https://<handle>.zo.space/anthropic/<sk-ant-...>/v1
//   API Key:  anything (unused; the Anthropic key in the URL path authenticates)
//   Model:    claude-opus-4-7  (or any model name Anthropic accepts)

type ChatMessage = {
  role: "system" | "user" | "assistant" | "tool" | "developer";
  content: string | Array<{ type: string; text?: string; [k: string]: unknown }>;
};

function extractText(content: ChatMessage["content"]): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((p) => {
      if (typeof p === "string") return p;
      if (p && typeof p === "object" && (p as any).type === "text" && typeof (p as any).text === "string") {
        return (p as any).text;
      }
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

function makeId() {
  return "chatcmpl-" + Math.random().toString(36).slice(2, 12);
}

function streamChunk(id: string, model: string, delta: object, finish: string | null = null) {
  return (
    "data: " +
    JSON.stringify({
      id,
      object: "chat.completion.chunk",
      created: Math.floor(Date.now() / 1000),
      model,
      choices: [{ index: 0, delta, finish_reason: finish }],
    }) +
    "\n\n"
  );
}

function normalizeModel(input?: string): string {
  if (!input) return "claude-opus-4-7";
  let m = input.trim();
  // Strip a leading "anthropic:" prefix if a client uses Zo-style naming.
  if (m.toLowerCase().startsWith("anthropic:")) m = m.slice("anthropic:".length);
  return m;
}

export default async (c: Context) => {
  const apikey = (c.req.param("apikey") || "").trim();
  if (!apikey) {
    return c.json(
      { error: { message: "Missing Anthropic API key in URL path", type: "auth_error" } },
      401,
    );
  }

  // === Coding-agent denylist === (same as the Zo route)
  // Allowed chat clients (verified, do NOT match any pattern): RikkaHub,
  // SillyTavern, TauriTavern (Darkatse/TauriTavern), ChatBox, LobeChat,
  // Mozilla/Chrome/Safari, reqwest, curl, python scripts.
  const ua = (c.req.header("user-agent") || "").toLowerCase();
  const BLOCKED_UA_PATTERNS = [
    /claude[-_ ]?code/, /codex[-_ ]?cli/, /openai[-_ ]?codex/,
    /\bcursor\b/, /windsurf/, /\bcline\b/, /\baider\b/,
    /continue[-_ ]?(dev|cli)/, /gpt-engineer/,
    /lobster/, /long[-_ ]?xia/, /\btrae\b/, /void[-_ ]?editor/, /devin/,
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

  let body: any;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: { message: "Invalid JSON body", type: "invalid_request_error" } }, 400);
  }

  if (!Array.isArray(body?.messages) || body.messages.length === 0) {
    return c.json({ error: { message: "`messages` is required", type: "invalid_request_error" } }, 400);
  }

  // Split system messages out and merge consecutive same-role messages
  // (Anthropic requires strict user/assistant alternation).
  const systemParts: string[] = [];
  type AntMsg = { role: "user" | "assistant"; content: string };
  const msgs: AntMsg[] = [];
  for (const m of body.messages as ChatMessage[]) {
    const text = extractText(m.content);
    if (!text) continue;
    if (m.role === "system" || m.role === "developer") {
      systemParts.push(text);
      continue;
    }
    const role: "user" | "assistant" = m.role === "assistant" ? "assistant" : "user";
    const last = msgs[msgs.length - 1];
    if (last && last.role === role) {
      last.content += "\n\n" + text;
    } else {
      msgs.push({ role, content: text });
    }
  }

  // Anthropic requires the first message to be from the user.
  if (msgs.length === 0 || msgs[0].role !== "user") {
    msgs.unshift({ role: "user", content: "(start)" });
  }

  const wantStream = body.stream === true;
  const model = normalizeModel(body.model);
  const requestedModel = body.model || model;

  const anthropicPayload: Record<string, unknown> = {
    model,
    messages: msgs,
    max_tokens:
      typeof body.max_completion_tokens === "number"
        ? body.max_completion_tokens
        : typeof body.max_tokens === "number"
          ? body.max_tokens
          : 64_000,
    stream: wantStream,
  };
  if (systemParts.length > 0) anthropicPayload.system = systemParts.join("\n\n");
  if (typeof body.temperature === "number") anthropicPayload.temperature = body.temperature;
  if (typeof body.top_p === "number") anthropicPayload.top_p = body.top_p;
  if (typeof body.top_k === "number") anthropicPayload.top_k = body.top_k;
  if (body.stop_sequences || body.stop) {
    const stop = body.stop_sequences ?? body.stop;
    anthropicPayload.stop_sequences = Array.isArray(stop) ? stop : [stop];
  }

  // Fire the upstream request immediately; only await inside the stream
  // controller so our response headers + first SSE frame go out instantly.
  const upstreamPromise = fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "x-api-key": apikey,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
      accept: wantStream ? "text/event-stream" : "application/json",
    },
    body: JSON.stringify(anthropicPayload),
  });

  if (!wantStream) {
    const upstream = await upstreamPromise;
    if (!upstream.ok) {
      const errText = await upstream.text();
      return c.json(
        {
          error: {
            message: `Anthropic error (${upstream.status}): ${errText.slice(0, 800)}`,
            type: "upstream_error",
            code: upstream.status,
          },
        },
        upstream.status as 400 | 401 | 403 | 404 | 429 | 500,
      );
    }
    const data: any = await upstream.json();
    const content =
      Array.isArray(data?.content)
        ? data.content.filter((b: any) => b?.type === "text").map((b: any) => b.text).join("")
        : "";
    return c.json({
      id: makeId(),
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model: requestedModel,
      choices: [
        {
          index: 0,
          message: { role: "assistant", content },
          finish_reason: data?.stop_reason === "end_turn" ? "stop" : (data?.stop_reason ?? "stop"),
        },
      ],
      usage: {
        prompt_tokens: data?.usage?.input_tokens ?? 0,
        completion_tokens: data?.usage?.output_tokens ?? 0,
        total_tokens: (data?.usage?.input_tokens ?? 0) + (data?.usage?.output_tokens ?? 0),
      },
    });
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

      controller.enqueue(encoder.encode(streamChunk(id, requestedModel, { role: "assistant" })));

      const reader = upstream.body?.getReader();
      if (!reader) {
        controller.enqueue(encoder.encode(streamChunk(id, requestedModel, {}, "stop")));
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
        return;
      }

      let buffer = "";
      let stopReason: string | null = null;
      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const events = buffer.split(/\r?\n\r?\n/);
          buffer = events.pop() ?? "";

          for (const evt of events) {
            let eventType = "";
            const dataLines: string[] = [];
            for (const line of evt.split(/\r?\n/)) {
              if (line.startsWith("event:")) eventType = line.slice(6).trim();
              else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
            }
            if (dataLines.length === 0) continue;

            let parsed: any;
            try {
              parsed = JSON.parse(dataLines.join("\n"));
            } catch {
              continue;
            }

            if (eventType === "content_block_delta" || parsed?.type === "content_block_delta") {
              const d = parsed?.delta;
              if (d?.type === "text_delta" && typeof d.text === "string" && d.text.length > 0) {
                controller.enqueue(encoder.encode(streamChunk(id, requestedModel, { content: d.text })));
              }
            } else if (eventType === "message_delta" || parsed?.type === "message_delta") {
              if (parsed?.delta?.stop_reason) stopReason = parsed.delta.stop_reason;
            } else if (eventType === "error" || parsed?.type === "error") {
              const msg = parsed?.error?.message || "anthropic stream error";
              controller.enqueue(
                encoder.encode(streamChunk(id, requestedModel, { content: `\n[upstream error: ${msg}]` })),
              );
            }
          }
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
        return;
      }

      controller.enqueue(
        encoder.encode(streamChunk(id, requestedModel, {}, stopReason === "end_turn" ? "stop" : (stopReason ?? "stop"))),
      );
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
