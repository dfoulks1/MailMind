# MailMind

A background Gmail ingestion and retrieval service with a local RAG store,
scheduled sync, and a REST API built for Ollama models.

MailMind runs as a long-lived process that automatically pulls your Gmail
inbox on a schedule, indexes message content into a local SQLite database,
and exposes an HTTP API over localhost so that any Ollama model can retrieve
relevant email context before generating a response.

```
Gmail API  ──►  IngestionScheduler  ──►  RagStore (SQLite BM25)
                                               │
Ollama models  ──►  POST /query  ─────────────┘
                    POST /analyze
                    POST /ingest
                    POST /refresh
                    GET  /status
```

---

## Documentation

| File | Contents |
|------|----------|
| [docs/getting-started.md](docs/getting-started.md) | Installation, first-run OAuth flow, and running the service |
| [docs/configuration.md](docs/configuration.md) | All environment variables and `.env` reference |
| [docs/api.md](docs/api.md) | Complete HTTP API reference with request/response examples |
| [docs/architecture.md](docs/architecture.md) | Module map, data flow, design decisions |
| [docs/scheduler.md](docs/scheduler.md) | Scheduling modes, cron syntax, and tuning |
| [docs/rag.md](docs/rag.md) | RAG store internals, BM25 scoring, and upgrade paths |
| [docs/ollama-integration.md](docs/ollama-integration.md) | How to wire Ollama models to query MailMind |
| [docs/deployment.md](docs/deployment.md) | systemd, launchd, Docker, and security hardening |
| [docs/development.md](docs/development.md) | Contributing, testing, linting, and project structure |

---

## Quick start

```bash
# 1. Install
git clone https://github.com/you/mailmind && cd mailmind
uv sync

# 2. Configure
cp .env.example .env
# Edit .env — add OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET

# 3. Authorise (one-time browser flow)
mailmind auth

# 4. Run
mailmind
```

The service binds to `http://127.0.0.1:8765` by default.
Visit `http://127.0.0.1:8765/docs` for the interactive API explorer.

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (or pip)
- [Ollama](https://ollama.com) running locally (`ollama serve`)
- A Google Cloud project with the Gmail API enabled and OAuth 2.0 Desktop credentials
