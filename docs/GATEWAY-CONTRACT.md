---
title: Gateway Contract
parent: Reference
nav_order: 5
---

# Brain LLM Gateway Contract

The brain (kb-router, kb_brief MCP tool, tick-engine) is a vanilla
OpenAI-compatible client. It speaks `POST /v1/chat/completions` with an
auth header sourced from env. Wire it at any compatible endpoint:
LiteLLM proxy, OpenAI direct, Anthropic-via-proxy, Ollama, OpenRouter,
or your own gateway.

## What the brain calls

Three call sites, two model names:

| Code | Model env | Default | Purpose |
|---|---|---|---|
| `services/kb-router/app/router.py` | `BRAIN_CLASSIFY_MODEL` | `brain-classify` | Classify ingest message into JSON |
| `services/mcp/app/tools/kb.py` (`kb_brief`) | `BRAIN_BRIEF_MODEL` | `brain-brief` | Synthesize KB brief |
| `services/tick-engine/brain_tick.py` | `BRAIN_BRIEF_MODEL` | `brain-brief` | Reason over hot arcs in the tick |

Each deployment maps these canonical names (`brain-classify`, `brain-brief`) onto whichever upstream models its LLM gateway resolves them to.

## Required env (every deployment)

Four env vars drive the contract. All have safe defaults except
`INFERENCE_URL` (which must be set for the AI phase to fire) and
`INFERENCE_API_KEY` (set when your gateway requires auth).

- `INFERENCE_URL` — base URL of an OpenAI-compatible gateway.
- `INFERENCE_API_KEY` — gateway auth token (skipped when empty).
- `BRAIN_CLASSIFY_MODEL` — name of the classifier model alias.
- `BRAIN_BRIEF_MODEL` — name of the synthesis model alias.

When `INFERENCE_URL` is empty the brain runs deterministic-only:
kb-router falls back to regex classification; kb_brief and tick-engine
return an explanatory error string instead of a brief. Empty
`INFERENCE_API_KEY` skips the auth header — fine for trusted-LAN
proxies, required to be set for OpenAI / LiteLLM.

## Wiring options

### A. LiteLLM proxy (operator pattern)

The recommended pattern when you already run LiteLLM. Register
`brain-classify` and `brain-brief` as model aliases in LiteLLM, mint a
virtual key with access to those models, and point the brain at the
LiteLLM endpoint.

`models/brain-classify.json` (litellm-state-style):

```json
{
  "model_name": "brain-classify",
  "litellm_params": {
    "api_base": "http://your-haiku-provider/v1",
    "model": "openai/your-haiku-model"
  },
  "model_info": {"mode": "chat", "supports_response_schema": true}
}
```

`models/brain-brief.json` similar, pointing at a Sonnet-class model.

Brain env: set `INFERENCE_URL` at the LiteLLM endpoint and
`INFERENCE_API_KEY` to your virtual key. Defaults `brain-classify` and
`brain-brief` work without renaming.

### B. OpenAI direct

Set `INFERENCE_URL` at `https://api.openai.com/v1`, set
`INFERENCE_API_KEY` to your OpenAI key, override
`BRAIN_CLASSIFY_MODEL` to `gpt-4o-mini` and `BRAIN_BRIEF_MODEL` to
`gpt-4o`.

OpenAI accepts the auth header just fine — the brain is a plain OpenAI
SDK consumer at the wire level.

### C. Ollama (laptop / air-gapped)

Set `INFERENCE_URL` at `http://ollama:11434/v1`, leave
`INFERENCE_API_KEY` empty, override `BRAIN_CLASSIFY_MODEL` and
`BRAIN_BRIEF_MODEL` to whatever models you have `ollama pull`'d
(typical: `qwen2.5:1.5b` and `qwen2.5:7b`).

JSON-mode support varies by Ollama version and model — kb-router falls
back to regex on malformed JSON, so degradation is graceful.

## Adding a new brain route

1. Pick a model name (e.g. `brain-extract`).
2. Add an env var in the call site (e.g. `BRAIN_EXTRACT_MODEL`).
3. Use the existing `_inference_chat` or `_call_router_llm` helper —
   each already handles the URL, body, auth header, and error fallback.
4. Document it here under "What the brain calls".
5. Add the alias on your gateway side (LiteLLM model JSON, OpenAI/Ollama mapping, etc.) so the canonical name resolves to a real upstream model.

## Wire-level reference (what the brain actually sends)

`POST` to `${INFERENCE_URL}/v1/chat/completions` with the auth header
when the token env var is set. Body is standard chat-completions:
`model` set to the configured `BRAIN_*_MODEL`, `messages` array,
`max_tokens`, `temperature`, and (classifier only) `response_format:
{"type": "json_object"}`.

Response is parsed as standard OpenAI shape
(`choices[0].message.content`). Anything that returns that shape works.

## What the brain does NOT use

- `provider` body field (LiteLLM-only).
- Streaming / SSE — synchronous calls only.
- Tool calling / function calling — the classifier uses
  `response_format: json_object` instead.
- Embedding endpoints — embeddings are a separate service
  (`agentibrain-embeddings`), not part of this contract.

## Failure modes

| Condition | Brain behavior |
|---|---|
| `INFERENCE_URL` empty | kb-router uses regex classifier; kb_brief / tick return error string |
| Network error / timeout | same as empty (fail-closed graceful) |
| 401 / 403 | logged warning, regex fallback for classify; error string for brief |
| 4xx other | error string returned to caller |
| 5xx | logged warning, regex fallback for classify; error string for brief |
| Malformed JSON in classify response | regex fallback |

The brain never blocks on the gateway. If the gateway is down, ingest
still creates artifacts (just without semantic enrichment), and the
tick still does deterministic phases.
