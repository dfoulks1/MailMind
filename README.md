# gmail-analyzer

Analyze, summarize, and troubleshoot Gmail conversations using a local LLM.

**Quick start:**

```bash
uv sync
ollama pull llama3.2:1b
mcp-server-gmail &         # start Gmail MCP (needs credentials.json)
ollama serve &             # start Ollama
uv run gmail-analyzer search "is:unread" --mode full
```

See [`docs/README.md`](docs/README.md) for full setup instructions.
