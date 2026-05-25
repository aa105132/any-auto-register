import type { Context } from "hono";

// OpenAI-compatible /v1/models — dynamically pulls the user's available model
// list from Zo's /models/available endpoint using the access token in the URL.
// Also merges in "hidden" models that the official endpoint omits but the
// /zo/ask backend still accepts (verified via probing).

// Hidden-but-callable models the official endpoint doesn't list.
const HIDDEN_MODELS = [
  { model_name: "zo:anthropic/claude-opus-4-6",      label: "Opus 4.6",                vendor: "Anthropic", context_window: 1_000_000 },
  { model_name: "zo:anthropic/claude-sonnet-4-6",    label: "Sonnet 4.6",              vendor: "Anthropic", context_window: 1_000_000 },
  { model_name: "zo:anthropic/claude-sonnet-4-5",    label: "Sonnet 4.5",              vendor: "Anthropic", context_window: 1_000_000 },
  { model_name: "zo:openai/gpt-5.2",                 label: "GPT-5.2",                 vendor: "OpenAI",    context_window: 400_000   },
  { model_name: "zo:google/gemini-3-flash-preview",  label: "Gemini 3 Flash (preview)", vendor: "Google",    context_window: 1_000_000 },
];

const FALLBACK_MODELS = [
  { model_name: "zo:anthropic/claude-opus-4-7",      label: "Opus 4.7",                 vendor: "Anthropic", context_window: 1_000_000 },
  { model_name: "zo:anthropic/claude-sonnet-4-6",    label: "Sonnet 4.6",               vendor: "Anthropic", context_window: 1_000_000 },
  { model_name: "zo:openai/gpt-5.5",                 label: "GPT-5.5",                  vendor: "OpenAI",    context_window: 1_050_000 },
  { model_name: "zo:openai/gpt-5.4",                 label: "GPT-5.4",                  vendor: "OpenAI",    context_window: 1_000_000 },
  { model_name: "zo:openai/gpt-5.4-mini",            label: "GPT-5.4 Mini",             vendor: "OpenAI",    context_window: 400_000   },
  { model_name: "zo:openai/gpt-5.3-codex",           label: "GPT-5.3 Codex",            vendor: "OpenAI",    context_window: 400_000   },
  { model_name: "zo:google/gemini-3.1-pro-preview",  label: "Gemini 3.1 Pro (preview)", vendor: "Google",    context_window: 1_000_000 },
  { model_name: "zo:deepseek/deepseek-v4-pro",       label: "DeepSeek V4 Pro",          vendor: "DeepSeek",  context_window: 1_000_000 },
  { model_name: "zo:minimax/minimax-m2.7",           label: "MiniMax M2.7",             vendor: "MiniMax",   context_window: 205_000   },
  { model_name: "zo:minimax/minimax-m2.5",           label: "MiniMax M2.5",             vendor: "MiniMax",   context_window: 196_608   },
  { model_name: "zo:zai/glm-5",                      label: "GLM 5",                    vendor: "Z.AI",      context_window: 202_752   },
  ...HIDDEN_MODELS,
];

function vendorToOwnedBy(vendor: string | null | undefined): string {
  if (!vendor) return "zo";
  return vendor.toLowerCase().replace(/\s+/g, "-");
}

function toOpenAiModel(m: any, now: number) {
  return {
    id: m.model_name,
    object: "model",
    created: now,
    owned_by: vendorToOwnedBy(m.vendor),
    display_name: m.label ?? m.model_name,
    description: m.description ?? undefined,
    context_length: m.context_window ?? 200_000,
    max_output_tokens: 64_000,
    is_byok: m.is_byok ?? false,
  };
}

export default async (c: Context) => {
  const token = (c.req.param("token") || "").trim();
  const now = Math.floor(Date.now() / 1000);

  try {
    const res = await fetch("https://api.zo.computer/models/available", {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
    });
    if (!res.ok) throw new Error(`upstream ${res.status}`);
    const data = (await res.json()) as { models?: any[] };
    const dynamicModels = data.models ?? [];

    // Merge dynamic + hidden, de-dupe by model_name.
    const seen = new Set(dynamicModels.map((m: any) => m.model_name));
    const merged = [
      ...dynamicModels,
      ...HIDDEN_MODELS.filter((m) => !seen.has(m.model_name)),
    ];

    return c.json({
      object: "list",
      data: merged.map((m) => toOpenAiModel(m, now)),
    });
  } catch {
    return c.json({
      object: "list",
      data: FALLBACK_MODELS.map((m) => toOpenAiModel(m, now)),
    });
  }
};
