# Gmail Analyzer — Developer Documentation

> **Stack:** Python 3.11+ · [uv](https://docs.astral.sh/uv/) · [Ollama](https://ollama.com/) `llama3.2:1b`  
> **MCP:** Google's official `gmailmcp.googleapis.com` remote MCP server (OAuth 2.0 / SSE)

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Setup Instructions](#setup-instructions)
   - [Prerequisites](#prerequisites)
   - [Google Cloud Application & OAuth](#google-cloud-application--oauth)
   - [Enabling the Gmail MCP API](#enabling-the-gmail-mcp-api)
   - [Ollama & llama3.2:1b](#ollama--llama321b)
   - [Project Installation](#project-installation)
4. [Usage](#usage)
5. [Analysis Modes](#analysis-modes)
6. [Configuration Reference](#configuration-reference)
7. [Developer Notes](#developer-notes)
8. [Running Tests](#running-tests)
9. [Potential Improvements](#potential-improvements)

---

## Overview

`gmail_analyzer` connects to Google's **official remote Gmail MCP server**
(`https://gmailmcp.googleapis.com/mcp/v1`) to read mailbox data,
then uses a locally-running **Ollama llama3.2:1b** model to summarize
conversations and generate deliverability troubleshooting advice.

Key capabilities:

- Search threads with any Gmail query string and analyze results in bulk.
- Full thread analysis: summaries, header/authentication checks, MIME/attachment
  inspection, and LLM-backed deliverability troubleshooting.
- OAuth 2.0 token management with automatic refresh and local caching.
- Pure-Python heuristic checks (DKIM/SPF/DMARC, spam flags, Reply-To mismatch,
  risky attachments) that run without any LLM round-trip.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  CLI  (gmail_analyzer.py  main / _cli_main)                          │
└─────────────────┬────────────────────────────────────────────────────┘
                  │
           GmailAnalyzer  (orchestrator)
          ┌───────┴──────────┐
          │                  │
  GmailMCPClient       OllamaClient
  (httpx JSON-RPC       (httpx /api/generate
   over HTTPS+SSE)       localhost:11434)
          │
  OAuthTokenManager
  (OAuth 2.0 PKCE-like
   desktop flow + refresh)
          │
  https://gmailmcp.googleapis.com/mcp/v1
          │
  Google's Gmail MCP Server  ←→  Gmail REST API
```

**Data flow:**

1. `OAuthTokenManager` provides a valid Bearer token (from cache, refresh, or interactive flow).
2. `GmailMCPClient` issues JSON-RPC `tools/call` requests over HTTPS (with SSE fallback)
   to `gmailmcp.googleapis.com/mcp/v1`.
3. `ThreadParser` converts the Gmail API-shaped payloads into typed `ThreadSummary`
   and `MessageSummary` dataclasses.
4. Mode-specific analyzers run:
   - `HeaderAnalyzer` — pure Python, no LLM (authentication, spam, delivery)
   - `MIMEAnalyzer` — pure Python, no LLM (attachments, risky file types)
   - `TroubleshootAnalyzer` — heuristics + Ollama LLM synthesis
   - Summarizer inside `GmailAnalyzer._summarize` — Ollama LLM
5. Results are collected into `AnalysisResult` and printed / returned.

---

## Setup Instructions

### Prerequisites

| Tool | Minimum version | Install |
|------|----------------|---------|
| Python | 3.11 | [python.org](https://www.python.org/downloads/) |
| uv | 0.4+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Ollama | 0.3+ | [ollama.com/download](https://ollama.com/download) |
| gcloud CLI | latest | [cloud.google.com/sdk](https://cloud.google.com/sdk/docs/install) |

---

### Google Cloud Application & OAuth

The Gmail MCP server authenticates to Gmail on your behalf using OAuth 2.0.
You need a Google Cloud project with the correct APIs and an OAuth client.

#### Step 1 — Create or select a Google Cloud project

```bash
gcloud projects create gmail-analyzer-dev --name="Gmail Analyzer Dev"
gcloud config set project gmail-analyzer-dev
```

Or use an existing project in [console.cloud.google.com](https://console.cloud.google.com).

#### Step 2 — Enable required APIs

```bash
# Core Gmail API
gcloud services enable gmail.googleapis.com --project=gmail-analyzer-dev

# Official Gmail MCP component (gmailmcp.googleapis.com)
gcloud services enable gmailmcp.googleapis.com --project=gmail-analyzer-dev
```

Or enable both from the Cloud Console:
- [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)
- [Gmail MCP API](https://console.cloud.google.com/apis/library/gmailmcp.googleapis.com)

#### Step 3 — Configure the OAuth Consent Screen

1. Go to **APIs & Services → OAuth consent screen** in [Cloud Console](https://console.cloud.google.com).
2. Choose **External** (personal Gmail) or **Internal** (Google Workspace org).
3. Fill in: App name, support email, developer contact.
4. On the **Scopes** page, add:
   ```
   https://www.googleapis.com/auth/gmail.readonly
   ```
5. Add your own Gmail address as a **Test user**.
6. Save and continue through all screens.

> ⚠️ Google will show an "unverified app" warning during sign-in for external apps.
> Click **Advanced → Continue** to proceed. This is normal for personal/dev use.

#### Step 4 — Create OAuth 2.0 Credentials

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Desktop app**.
3. Name: `gmail-analyzer-desktop`.
4. Click **Create**, then note your **Client ID** and **Client Secret**.

> ⚠️ Never commit these values to version control.
> Add `token.json` and `.env` to your `.gitignore`.

#### Step 5 — Provide credentials to the analyzer

Supply them as environment variables (recommended) or CLI flags:

```bash
export OAUTH_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export OAUTH_CLIENT_SECRET="your-client-secret"
```

Or use `--client-id` / `--client-secret` flags on the CLI.

On first run the analyzer will print an authorization URL. Open it in your
browser, grant access, and paste the code back into the terminal.
The token is saved to `token.json` for all future runs.

---

### Enabling the Gmail MCP API

The Gmail MCP API (`gmailmcp.googleapis.com`) is currently in
**Google Workspace Developer Preview**. It must be explicitly enabled:

```bash
gcloud services enable gmailmcp.googleapis.com --project=YOUR_PROJECT_ID
```

Verify:
```bash
gcloud services list --enabled --project=YOUR_PROJECT_ID | grep gmailmcp
# gmailmcp.googleapis.com  Gmail MCP API
```

**OAuth scopes required by the MCP server:**

```
https://www.googleapis.com/auth/gmail.readonly
```

Add `https://www.googleapis.com/auth/gmail.modify` only if you use
`create_draft`, `label_thread`, or `label_message` tools in the future.

---

### Ollama & llama3.2:1b

#### Install Ollama

```bash
# macOS / Linux
curl -fsSL https://ollama.com/install.sh | sh

# Start the server (default: http://localhost:11434)
ollama serve
```

#### Pull the model

```bash
ollama pull llama3.2:1b
```

Verify:
```bash
ollama list
# NAME            ID            SIZE   MODIFIED
# llama3.2:1b     ...           1.3 GB  ...
```

#### Configuration notes for llama3.2:1b

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `num_ctx` | 4096 | Default context window; fits most threads comfortably |
| `temperature` | 0.2 | Low for factual/structured output |
| Timeout | 120 s | CPU: ~5–30 s; GPU: ~2–5 s |
| `MAX_BODY_CHARS` | 6 000 | Truncates bodies before prompt to avoid context overflow |

**Hardware expectations:**

| Hardware | Tokens/s | Response time |
|----------|----------|---------------|
| Apple M-series (8 GB) | 50–80 | ~5 s |
| Mid-range CPU (no GPU) | 10–20 | ~15–30 s |
| NVIDIA GPU (CUDA) | 100–200 | ~2–5 s |

**Using a larger model:** Change `Config.OLLAMA_MODEL = "llama3.2:3b"` for
higher quality at ~3× the resource cost.

---

### Project Installation

```bash
# Install dependencies (including dev/test)
uv sync

# Verify
uv run python -c "import gmail_analyzer; print('OK')"
```

---

## Usage

```bash
# Summarize the 5 most recent unread threads
uv run gmail-analyzer search "is:unread" --max 5 --mode summarize

# Full analysis of threads from a specific sender
uv run gmail-analyzer search "from:boss@example.com" --max 3 --mode full

# Analyze a single thread (get the ID from Gmail's URL bar)
uv run gmail-analyzer thread 18f3a2b1c0d9e8f7 --mode full

# Just check authentication + delivery headers
uv run gmail-analyzer thread 18f3a2b1c0d9e8f7 --mode headers

# Troubleshoot a suspicious or bounced message
uv run gmail-analyzer thread 18f3a2b1c0d9e8f7 --mode troubleshoot

# Pass credentials inline (alternative to env vars)
uv run gmail-analyzer --client-id CLIENT_ID --client-secret SECRET \
    search "subject:invoice" --max 10 --mode summarize
```

**Finding a thread ID:** In Gmail's web UI, open a message.
The URL ends with the thread ID: `https://mail.google.com/mail/u/0/#inbox/18f3a2b1c0d9e8f7`

---

## Analysis Modes

| Mode | LLM? | What it does |
|------|------|--------------|
| `summarize` | ✅ | 4–6 sentence summary of the full thread, key actions and decisions |
| `headers` | ❌ | DKIM/SPF/DMARC authentication, spam flags, delivery hop estimate, date sanity, Reply-To mismatch, mailing-list headers |
| `mime` | ❌ | Attachment inventory, risky file type warnings |
| `troubleshoot` | ✅ | Heuristic warnings **+** LLM root-cause analysis and remediation suggestions |
| `full` | ✅ (×2) | All of the above |

---

## Configuration Reference

All config is in the `Config` class at the top of `gmail_analyzer.py`.

```python
class Config:
    # Google Gmail MCP — official remote server
    GMAIL_MCP_URL: str = "https://gmailmcp.googleapis.com/mcp/v1"

    # OAuth — set via env vars OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET
    OAUTH_CLIENT_ID: str = ""
    OAUTH_CLIENT_SECRET: str = ""
    OAUTH_TOKEN_FILE: str = "token.json"
    OAUTH_SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.readonly"]

    # Ollama
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.2:1b"
    OLLAMA_TIMEOUT: float = 120.0
    OLLAMA_NUM_CTX: int = 4096
    OLLAMA_TEMPERATURE: float = 0.2

    # Misc
    MAX_BODY_CHARS: int = 6_000   # truncate before LLM prompt
    MCP_TIMEOUT: float = 30.0
```

---

## Developer Notes

### Key design decisions

**Google's official remote MCP**  
This tool talks directly to `https://gmailmcp.googleapis.com/mcp/v1` — there is
no local MCP server process, no `npm install`, and no Node.js dependency.
Authentication is an OAuth 2.0 Bearer token; the transport is HTTPS with
optional SSE streaming. Google manages the server infrastructure and rate limits.

**Available MCP tools (Google Developer Preview)**  
The `gmailmcp.googleapis.com` server exposes:
`search_threads`, `get_thread`, `list_labels`, `label_thread`, `unlabel_thread`,
`label_message`, `unlabel_message`, `create_draft`, `list_drafts`, `create_label`.
The analyzer uses `search_threads` and `get_thread` for read-only analysis.

**Thread-first model**  
The MCP server's primary unit is the thread, not the individual message.
`search_threads` returns thread stubs; `get_thread` fetches all messages.
This maps naturally to email conversation analysis.

**Pure-Python heuristics before LLM**  
DKIM/SPF/DMARC checks, spam flag detection, and attachment scanning are
deterministic and run without Ollama. The LLM is called only for natural-language
summarization and synthesizing troubleshooting narratives. This keeps CPU usage
low and makes the heuristics fully unit-testable without mocking Ollama.

**OAuth token file**  
Tokens are persisted to `token.json` in the working directory. The manager
refreshes automatically 60 seconds before expiry. Delete `token.json` to force
re-authorization.

**`respx` for HTTP mocking**  
Both the MCP and Ollama clients are backed by `httpx.AsyncClient`. Tests use
`respx` to intercept at the transport layer — no network access needed,
and request bodies/headers can be fully inspected.

### Error taxonomy

| Exception | Meaning |
|-----------|---------|
| `OAuthError` | Credentials missing, token refresh failed, or interactive flow aborted |
| `GmailMCPError` | MCP HTTP error (401, 5xx), RPC error, tool-level error, or connection failure |
| `OllamaError` | Ollama not running, model not pulled, timeout, or HTTP error |

### Adding a new analysis mode

1. Add a value to `AnalysisMode`.
2. Write a new `XyzAnalyzer` class.
3. Call it in `GmailAnalyzer._run_analysis` under the new mode.
4. Add tests.

---

## Running Tests

```bash
# Full suite (fully offline — all HTTP mocked)
uv run pytest -v

# With coverage
uv run pytest --cov=gmail_analyzer --cov-report=term-missing

# Single class
uv run pytest -v tests/test_gmail_analyzer.py::TestHeaderAnalyzer

# With log output
uv run pytest -v -s
```

---

## Potential Improvements

### Short term

- **Environment-variable config** — Read all `Config` fields from env vars
  via `python-dotenv` or `pydantic-settings`.
- **JSON output** — Add `--output json` to emit machine-readable `AnalysisResult`
  objects for piping into other tools or dashboards.
- **Streaming Ollama** — Use `stream=True` and print tokens as they arrive for
  a more responsive CLI experience.
- **`--dry-run` mode** — Fetch and parse without calling Ollama (useful for
  verifying MCP connectivity and header parsing).

### Medium term

- **Async concurrency** — Use `asyncio.gather` to fetch and analyze multiple
  threads in parallel instead of sequential loops.
- **Thread conversation prompt** — Build a single chronological prompt from
  all messages in a thread so the LLM can reason about the full arc rather
  than each message in isolation.
- **Caching** — Cache parsed `ThreadSummary` objects keyed by thread ID + history
  ID to avoid re-fetching unchanged threads.
- **Label-based triage** — After analysis, automatically apply Gmail labels
  (e.g. `needs-action`, `spam-risk`) using `label_thread` / `label_message`.
- **Draft generation** — Use `create_draft` to automatically draft replies
  to flagged threads based on Ollama's suggested action items.

### Longer term

- **Embeddings search** — Embed thread bodies with a local model and store in a
  vector DB (e.g. ChromaDB) for semantic search across the mailbox.
- **Scheduled monitoring** — Run as a background service polling for new threads
  matching a query, alerting on deliverability regressions.
- **Multi-account support** — Maintain separate `OAuthTokenManager` instances
  per account, routing requests based on query context.
- **Larger model fallback** — Detect very short or repetitive Ollama responses
  (low confidence) and retry with `llama3.2:3b` or `llama3.1:8b` automatically.
- **DMARC aggregate report parsing** — Parse `rua` XML reports attached to
  messages and produce human-readable deliverability summaries.
- **Google Workspace integration** — Use the Calendar and Drive MCP servers
  alongside Gmail to correlate email threads with calendar events and shared docs.
