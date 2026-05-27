# Configuration Reference

All runtime settings are controlled through environment variables.
MailMind reads them from a `.env` file on startup (via `python-dotenv`)
and from the process environment, with process environment variables always
taking precedence over the file.

Copy `.env.example` to `.env` and fill in the values for your setup.

---

## How settings are applied

Settings are loaded in this priority order (highest wins):

1. **Process environment** — variables exported in your shell or set by
   a service manager like systemd.
2. **`.env` file** — loaded at startup; does not override process env vars.
3. **Dataclass defaults** — hardcoded defaults in `mailmind/config.py`.

CLI flags (`--host`, `--port`, `--log-level`, `--no-scheduler`) override
the corresponding settings after `.env` is loaded, so they always win.

---

## OAuth 2.0

These two variables are **required**. The service will not start the ingest
scheduler or accept Gmail-dependent requests without them.

| Variable | Default | Description |
|----------|---------|-------------|
| `OAUTH_CLIENT_ID` | *(none)* | Desktop-app OAuth 2.0 client ID from Google Cloud Console. |
| `OAUTH_CLIENT_SECRET` | *(none)* | Matching client secret. |
| `OAUTH_TOKEN_FILE` | `token.json` | Path where the access/refresh token JSON is cached after the first `mailmind auth` run. Delete this file to force re-authorisation. |

---

## Gmail API

| Variable | Default | Description |
|----------|---------|-------------|
| `GMAIL_API_URL` | `https://gmail.googleapis.com/gmail/v1` | Gmail REST API base URL. Only change this if Google updates the endpoint. |
| `GMAIL_DEFAULT_QUERY` | `category:inbox -category:trash` | Gmail search query used by the scheduled ingest job. Accepts any syntax supported by the Gmail search box (e.g. `from:boss@corp.com after:2024-01-01`). |
| `GMAIL_MAX_RESULTS` | `200` | Maximum number of threads fetched per ingest run. The Gmail API hard cap is 500 per page. |
| `GMAIL_MAX_BODY_CHARS` | `6000` | Maximum characters of email body forwarded to Ollama per message. Increase for richer summaries; decrease to keep LLM prompts short. |
| `GMAIL_API_TIMEOUT` | `30.0` | HTTP timeout (seconds) for Gmail API calls. |

### Writing a good `GMAIL_DEFAULT_QUERY`

The query uses [Gmail search operators](https://support.google.com/mail/answer/7190):

```bash
# Inbox only, no trash, last 90 days
GMAIL_DEFAULT_QUERY=category:inbox -category:trash after:2024-10-01

# Unread messages from your team domain
GMAIL_DEFAULT_QUERY=from:@yourcompany.com is:unread

# Everything — use with a low MAX_RESULTS to avoid long ingest runs
GMAIL_DEFAULT_QUERY=in:anywhere
```

---

## Ollama

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Base URL of your local Ollama server. |
| `OLLAMA_MODEL` | `llama3.2:1b` | Model tag to use for analysis and summarisation. Must be pulled with `ollama pull <model>` before use. |
| `OLLAMA_TIMEOUT` | `120.0` | HTTP timeout (seconds) for Ollama generation calls. Increase for large models or slow hardware. |
| `OLLAMA_NUM_CTX` | `4096` | Context window size in tokens. Match this to your model's actual context limit. |
| `OLLAMA_TEMPERATURE` | `0.2` | Sampling temperature. Lower values (0.1–0.3) produce more factual, deterministic output. |

### Choosing a model

Any model available on Ollama works. Smaller models are faster for routine
queries; larger models produce better summaries and troubleshooting analysis.

| Model | Size | Good for |
|-------|------|----------|
| `llama3.2:1b` | ~900 MB | Quick summaries on low-resource hardware |
| `llama3.2:3b` | ~2 GB | Balanced quality and speed |
| `llama3.1:8b` | ~5 GB | High-quality analysis |
| `mistral:7b` | ~4 GB | Strong instruction following |

---

## RAG Store

| Variable | Default | Description |
|----------|---------|-------------|
| `RAG_DB_PATH` | `mailmind.db` | Path to the SQLite database file. Use an absolute path to avoid ambiguity when running as a system service. |
| `RAG_CHUNK_SIZE` | `400` | Target word count per text chunk. Smaller chunks improve precision for narrow queries; larger chunks improve recall for broad queries. |
| `RAG_CHUNK_OVERLAP` | `40` | Words shared between consecutive chunks. Overlap prevents sentences at boundaries from being missed. Must be less than `RAG_CHUNK_SIZE`. |

---

## Scheduler

| Variable | Default | Description |
|----------|---------|-------------|
| `INGEST_CRON` | *(empty)* | Standard five-field cron expression. When set, takes precedence over `INGEST_INTERVAL_MINUTES`. See [scheduler.md](scheduler.md) for examples. |
| `INGEST_INTERVAL_MINUTES` | `360` | Fallback interval in minutes (default: every 6 hours). Used when `INGEST_CRON` is empty. |
| `SCHEDULER_ENABLED` | `true` | Set to `false` to disable background scheduling entirely. Useful when running MailMind as a pure query service or during testing. |

---

## HTTP Service

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_HOST` | `127.0.0.1` | Interface to bind the HTTP server. Use `0.0.0.0` to accept connections from other machines (only do this on a trusted network). |
| `SERVICE_PORT` | `8765` | Port to listen on. |
| `SERVICE_LOG_LEVEL` | `info` | Uvicorn log verbosity. One of: `debug`, `info`, `warning`, `error`. |

---

## Complete `.env` example

```bash
# OAuth 2.0 — required
OAUTH_CLIENT_ID=123456789-abc.apps.googleusercontent.com
OAUTH_CLIENT_SECRET=GOCSPX-your-secret-here
OAUTH_TOKEN_FILE=/home/alice/.mailmind/token.json

# Gmail
GMAIL_DEFAULT_QUERY=category:inbox -category:trash after:2024-01-01
GMAIL_MAX_RESULTS=200
GMAIL_MAX_BODY_CHARS=6000
GMAIL_API_TIMEOUT=30.0

# Ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
OLLAMA_TIMEOUT=180.0
OLLAMA_NUM_CTX=8192
OLLAMA_TEMPERATURE=0.2

# RAG store
RAG_DB_PATH=/home/alice/.mailmind/mailmind.db
RAG_CHUNK_SIZE=400
RAG_CHUNK_OVERLAP=40

# Scheduler — every day at 07:00
INGEST_CRON=0 7 * * *
SCHEDULER_ENABLED=true

# HTTP service
SERVICE_HOST=127.0.0.1
SERVICE_PORT=8765
SERVICE_LOG_LEVEL=info
```

---

## Security notes

- **Never commit `.env` to version control.** It contains your OAuth client
  secret. Add `.env` and `token.json` to `.gitignore`.
- `token.json` contains an access token that grants read access to your
  Gmail. Store it with restricted permissions: `chmod 600 token.json`.
- `SERVICE_HOST=127.0.0.1` (the default) means the API is only reachable
  from the same machine. Do not change this to `0.0.0.0` on a shared server
  without adding authentication middleware.
