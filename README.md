# Hermes Plugins

Personal [Hermes Agent](https://hermes-agent.nousresearch.com) Python plugins
(`~/.hermes/plugins/`), released as standalone drop-ins.

| Plugin | What it does |
|--------|--------------|
| [**alibaba-quota-retry**](alibaba-quota-retry/) | Alibaba Cloud (Model Studio / token-plan) returns `429 insufficient_quota` for transient short-window TPM throttling — minutes later it recovers. Hermes' built-in classifier reads that as *billing* (out of money) and fails the turn immediately. This plugin reclassifies exactly that combination back to `rate_limit`, so the standard backoff-and-retry chain applies. Only for Alibaba providers; real 402/billing failures are untouched. |
| [**vec-memory-inject**](vec-memory-inject/) | Per-turn automatic RAG: before each LLM call it searches a local vector memory (bge-m3 embeddings + SQLite via `vec_memory.py`) and injects the top adaptive-filtered hits as context. Also captures turns and runs background knowledge extraction (`extract_turn.py`) so new facts are searchable on the next turn. Failures are silent — the conversation never breaks because of it. |

## Install

```bash
git clone https://github.com/get-together-cc/hermes-plugins
cp -r hermes-plugins/<plugin-name> ~/.hermes/plugins/
hermes plugins enable <plugin-name>
```

Restart Hermes afterwards so the running gateway process picks up the plugin
(plugins are scanned at process start).

## Requirements

- Hermes Agent with the Python plugin system (`~/.hermes/plugins/`).
- `vec-memory-inject` additionally needs `vec_memory.py` (search CLI) and
  optionally `extract_turn.py` (extraction). Paths are overridable via
  `HERMES_VEC_MEMORY_SCRIPT` / `HERMES_EXTRACT_SCRIPT` env vars.
  See the [vec-memory-system](https://github.com/get-together-cc/vec-memory-system)
  repo for the backend.

## License

MIT © 2026 Joe
