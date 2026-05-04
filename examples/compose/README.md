# examples/compose/ — overlays for the local Docker Compose path

Three drop-in overlays for the laptop / single-server install. Each one wires
the inference gateway differently. Pick whichever matches your situation.

| Overlay | Use case | Cost | Setup |
|---|---|---|---|
| `compose.ollama.yaml.example` | Local LLM, air-gapped, no API key | Free | Pulls a model on first tick |
| `compose.openai.yaml.example` | Cloud LLM, fastest path to hosted quality | Pay-per-token | Set `OPENAI_API_KEY` in `.env` |
| `compose.litellm.yaml.example` | Multi-provider gateway, virtual keys | Depends on LiteLLM config | Point at an existing LiteLLM proxy |

## Quickstart

Copy the overlay you want into the repo root (or reference it in place via
`-f examples/compose/...`), then layer it on top of the base compose:

```bash
docker compose -f compose.yml -f examples/compose/compose.ollama.yaml.example up -d
```

For Ollama, pull a model into the container before the first tick fires:

```bash
docker compose exec ollama ollama pull llama3.2:3b
```

For OpenAI / LiteLLM, set the relevant env vars in your `.env` (or export
them in the shell) before `docker compose up`:

```bash
echo "OPENAI_API_KEY=sk-..." >> .env
# or
echo "LITELLM_URL=http://your-litellm:4000" >> .env
echo "LITELLM_KEY=sk-litellm-virtual-key"   >> .env
```

## What each overlay sets

| Service | Env vars overridden by overlay |
|---|---|
| `kb-router` | `INFERENCE_URL`, `INFERENCE_API_KEY`, `BRAIN_CLASSIFY_MODEL` |
| `tick-cron` | `INFERENCE_URL`, `INFERENCE_API_KEY`, `BRAIN_BRIEF_MODEL` |
| `mcp` | `INFERENCE_URL`, `INFERENCE_API_KEY`, `BRAIN_BRIEF_MODEL` |
| `embeddings` | `LLM_API_BASE`, `LLM_API_KEY`, `LLM_EMBED_MODEL` (Ollama overlay omits — Ollama embeddings need extra setup) |

## Without an overlay

If you skip these and run plain `docker compose up -d`, the brain still
works — just deterministic-only. Hot arcs, signals, decay, and broadcasts
all run; only the AI synthesis phase of the tick is skipped. Add an overlay
later when you want LLM-augmented ticks.

See [`docs/GATEWAY-CONTRACT.md`](../../docs/GATEWAY-CONTRACT.md) for the
full inference contract — the kernel speaks plain OpenAI chat-completions,
so any compatible upstream works (Anthropic via proxy, vLLM, LM Studio,
OpenRouter, your own gateway).
