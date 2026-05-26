# gmail-analyzer

Analyze, summarize, and troubleshoot Gmail conversations using a local LLM.

Uses Google's **official remote Gmail MCP server** (`gmailmcp.googleapis.com`) —
no local MCP process or `npm install` required.

**Quick start:**

```bash
# 1. Install Python dependencies
uv sync

# 2. Pull the model
ollama pull llama3.2:1b && ollama serve &

# 3. Set OAuth credentials (one-time browser flow on first run)
export OAUTH_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export OAUTH_CLIENT_SECRET="your-client-secret"

# 4. Analyze
uv run gmail-analyzer search "is:unread" --mode full
```

See [`docs/README.md`](docs/README.md) for full setup instructions, including
how to create the Google Cloud project and enable `gmailmcp.googleapis.com`.
