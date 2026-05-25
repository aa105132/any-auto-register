import type { Context } from "hono";

// OpenAI-compatible /v1/models for the direct-Anthropic proxy. The IDs here
// are what the client will pass back as `model` on chat completion requests.
export default (c: Context) => {
  const now = Math.floor(Date.now() / 1000);
  const ids = [
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-opus-4",
  ];
  return c.json({
    object: "list",
    data: ids.map((id) => ({
      id,
      object: "model",
      created: now,
      owned_by: "anthropic",
      context_length: 1_000_000,
      max_output_tokens: 64_000,
    })),
  });
};
