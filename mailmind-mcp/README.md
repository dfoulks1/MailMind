# Mailmind MCP Server

A production-ready Python MCP (Model Context Protocol) server that exposes your Gmail inbox and Mailmind RAG system to AI assistants such as Claude.

This is a drop-in Python replacement for the Node.js [`dfoulks1/GmailMCP`](https://github.com/dfoulks1/GmailMCP) server, maintaining full feature parity while integrating natively with the [`dfoulks1/Mailmind`](https://github.com/dfoulks1/mailmind) RAG background service.

---

## Architecture

```
Claude Desktop / MCP client
        │  stdio (default) or SSE
        ▼
  mcp/server.py          ← MCP server (this repo)
  ├─ tools/gmail.py      ← Gmail API actions
  └─ tools/search.py     ← RAG search + LLM summarization
        │
        ├─► Google Gmail API     (google-api-python-client)
        ├─► MongoDB email cache  (pymongo)
        ├─► Celery task queue    (redis broker)
        └─► Ollama HTTP API      (httpx → llama3.2:1b)
```

The **RAG background service** (`dfoulks1/Mailmind`) runs separately and continuously ingests emails into MongoDB. The MCP server reads from that cache and dispatches Celery tasks to trigger ingest/refresh jobs.

---

## Prerequisites

| Service   | Minimum version | Notes                                |
|-----------|-----------------|--------------------------------------|
| Python    | 3.11+           |                                      |
| MongoDB   | 6.0+            | For the email cache                  |
| Redis     | 7.0+            | Celery broker for background tasks   |
| Ollama    | latest          | Local LLM — pull `llama3.2:1b`       |
| Gmail API | v1              | OAuth2 credentials required          |

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/dfoulks1/mailmind
cd mailmind

# Using uv (recommended)
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Or plain pip
pip install -r requirements.txt
```

### 2. Set up Gmail OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials.
2. Create an **OAuth 2.0 Client ID** (Desktop app).
3. Download `credentials.json` and place it in the project root (or set `GMAIL_CREDENTIALS_FILE`).
4. On first run the server will open a browser for the OAuth consent flow and save `token.json`.

### 3. Configure

Copy the example config and edit as needed:

```bash
cp config/config.yaml config/config.local.yaml
```

All values support `${ENV_VAR:default}` interpolation.  Key variables:

| Variable                | Default                        | Description                       |
|-------------------------|--------------------------------|-----------------------------------|
| `GMAIL_CREDENTIALS_FILE`| `credentials.json`             | Path to OAuth2 credentials        |
| `GMAIL_TOKEN_FILE`      | `token.json`                   | Saved OAuth2 token                |
| `MONGO_URI`             | `mongodb://localhost:27017`    | MongoDB connection string         |
| `REDIS_URL`             | `redis://localhost:6379/0`     | Celery / Redis URL                |
| `OLLAMA_URL`            | `http://localhost:11434`       | Ollama API base URL               |
| `OLLAMA_MODEL`          | `llama3.2:1b`                  | Model to use for summarization    |
| `MCP_TRANSPORT`         | `stdio`                        | `stdio` or `sse`                  |
| `LOG_LEVEL`             | `INFO`                         | Python log level                  |

### 4. Add to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your OS:

```json
{
  "mcpServers": {
    "mailmind": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "mailmind_mcp.server"],
      "cwd": "/path/to/mailmind-mcp-server",
      "env": {
        "GMAIL_CREDENTIALS_FILE": "/path/to/credentials.json",
        "MONGO_URI": "mongodb://localhost:27017",
        "REDIS_URL": "redis://localhost:6379/0",
        "OLLAMA_URL": "http://localhost:11434"
      }
    }
  }
}
```

### 5. SSE / HTTP mode (optional)

```bash
MCP_TRANSPORT=sse python -m mailmind_mcp.server
# Server listens on http://0.0.0.0:8000/sse
```

---

## Available Tools

### Gmail tools

| Tool                | Description                                               |
|---------------------|-----------------------------------------------------------|
| `search_gmail`      | Search Gmail using a query string (real-time)             |
| `get_email`         | Fetch full content of a single email                      |
| `get_email_headers` | Fetch headers only (fast, no body decode)                 |
| `list_labels`       | List all Gmail labels                                     |
| `create_label`      | Create a new label                                        |
| `add_label`         | Add a label to a message                                  |
| `remove_label`      | Remove a label from a message                             |
| `mark_read`         | Mark a message as read                                    |
| `mark_unread`       | Mark a message as unread                                  |
| `trash_email`       | Move a message to trash                                   |
| `delete_email`      | Permanently delete a message (`confirm=True` required)    |
| `ingest_emails`     | Trigger background ingest of new emails into the cache    |

### RAG / Search tools

| Tool             | Description                                                  |
|------------------|--------------------------------------------------------------|
| `search_emails`  | Keyword search over the local MongoDB email cache            |
| `summarize_email`| Summarize an email via the local Ollama LLM                  |
| `ask_emails`     | Answer a question using cached emails as RAG context         |
| `refresh_rag`    | Trigger a background RAG index rebuild                       |
| `cache_stats`    | Return cache size and system health status                   |

### Example prompts

```
"Search my email for messages from my bank in the last week"
→ search_gmail(query="from:bank is:unread newer_than:7d")

"Summarize the last email from my boss"
→ search_gmail + get_email + summarize_email

"What did the team decide about the Q4 roadmap?"
→ ask_emails(question="Q4 roadmap decisions")

"Label all newsletters as 'Newsletter'"
→ search_gmail + create_label + add_label (loop)
```

---

## Testing

```bash
# Run unit tests
pytest tests/test_tools.py -v

# Run integration tests (no external services required)
pytest tests/test_integration.py -v

# Full suite with coverage
pytest --cov=mcp --cov-report=term-missing
```

---

## Architectural Notes vs. Node.js Version

| Aspect               | Node.js (GmailMCP)             | Python (this server)                      |
|----------------------|--------------------------------|-------------------------------------------|
| RAG communication    | `execa` subprocess + NDJSON    | Direct library calls + Celery tasks       |
| Transport            | stdio only                     | stdio **and** SSE/HTTP                    |
| Auth refresh         | Manual token file handling     | `google-auth` library (auto-refresh)      |
| Config               | `.env` file                    | `config.yaml` with env interpolation      |
| Retry logic          | Manual                         | `tenacity` decorators                     |
| Logging              | `console.error`                | `structlog` (structured JSON logs)        |

**Key assumption**: The Mailmind RAG service is running and has already populated MongoDB.  Tools that hit the cache (`search_emails`, `summarize_email`, `ask_emails`) will return empty results until at least one `ingest_emails` run has completed.

---

## Project Structure

```
mailmind-mcp-server/
├── mcp/
│   ├── __init__.py
│   ├── server.py          # MCP server entry point & tool registry
│   ├── config.py          # YAML config loader with env interpolation
│   ├── gmail_client.py    # Authenticated Gmail API wrapper
│   ├── rag_client.py      # MongoDB cache + Ollama + Celery client
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── gmail.py       # Gmail tool implementations
│   │   └── search.py      # RAG search tool implementations
│   └── handlers/
│       └── __init__.py
├── config/
│   ├── config.yaml        # Shared config (edit or override via env)
│   └── config.schema.json # JSON Schema for config validation
├── tests/
│   ├── __init__.py
│   ├── test_tools.py      # Unit tests (mocked dependencies)
│   └── test_integration.py # Integration tests (no external services)
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

## License

MIT
