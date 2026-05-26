# Gmail Analyzer — Developer Documentation

> **Stack:** Python 3.11+ · [uv](https://docs.astral.sh/uv/) · [Ollama](https://ollama.com/) `llama3.2:1b` · Gmail MCP Server · httpx · pytest

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Setup Instructions](#setup-instructions)
   - [Prerequisites](#prerequisites)
   - [Google Cloud Application & OAuth](#google-cloud-application--oauth)
   - [Gmail MCP Server](#gmail-mcp-server)
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

`gmail_analyzer` is a Python CLI tool and importable library that:

- Connects to a **Gmail mailbox** via the [Gmail MCP Server](https://github.com/modelcontextprotocol/servers) (JSON-RPC over HTTP).
- Fetches messages, threads, or search results and **parses RFC-822 structure** including MIME parts, raw headers, and bodies.
- Runs **local LLM inference** via [Ollama](https://ollama.com/) using `llama3.2:1b` to summarize conversations and generate troubleshooting advice.
- Performs **pure-Python heuristic analysis** for authentication (DKIM/SPF/DMARC), spam headers, delivery hops, Reply-To mismatches, and risky MIME types — no LLM round-trip needed for these checks.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  CLI  (gmail_analyzer.py main / _cli_main)                       │
└──────────────┬───────────────────────────────────────────────────┘
               │
        GmailAnalyzer (orchestrator)
       ┌───────┴─────────┐
       │                 │
GmailMCPClient     OllamaClient
(httpx JSON-RPC)   (httpx /api/generate)
       │
 Gmail MCP Server (localhost:3000)
       │
 Gmail REST API (via OAuth2)
       │
   Google Cloud
```

**Data flow:**

1. `GmailMCPClient` issues a JSON-RPC call to the local MCP server → receives raw Gmail API payloads.
2. `EmailParser` converts each payload into a typed `EmailMessage` dataclass (headers normalized to lowercase, bodies base64-decoded, MIME tree mapped).
3. Mode-specific analyzers run:
   - **`HeaderAnalyzer`** — pure Python, no LLM.
   - **`MIMEAnalyzer`** — pure Python, no LLM.
   - **`TroubleshootAnalyzer`** — builds a focused prompt from heuristic findings, then calls Ollama.
   - **Summarizer** (inside `GmailAnalyzer._summarize`) — calls Ollama directly.
4. Results are collected into `AnalysisResult` dataclasses and printed / returned.

---

## Setup Instructions

### Prerequisites

| Tool | Minimum version | Install |
|------|----------------|---------|
| Python | 3.11 | [python.org](https://www.python.org/downloads/) |
| uv | 0.4+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Ollama | 0.3+ | [ollama.com/download](https://ollama.com/download) |
| Node.js | 18+ | Required by the Gmail MCP server |

---

### Google Cloud Application & OAuth

The Gmail MCP server authenticates to Gmail on your behalf using **OAuth 2.0**.  You need to create a Google Cloud project and generate credentials.

#### Step 1 — Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Click **Select a project → New project**.
3. Name it (e.g. `gmail-analyzer-dev`) and click **Create**.

#### Step 2 — Enable the Gmail API

1. In the left menu: **APIs & Services → Library**.
2. Search for **"Gmail API"** and click **Enable**.

#### Step 3 — Configure the OAuth Consent Screen

1. **APIs & Services → OAuth consent screen**.
2. Choose **External** (or **Internal** if you're using Google Workspace).
3. Fill in:
   - App name: `Gmail Analyzer`
   - User support email: your Google account
   - Developer contact: your email
4. Click **Save and Continue** through all screens.
5. On the **Scopes** page, add:
   - `https://www.googleapis.com/auth/gmail.readonly`
   *(Add `gmail.modify` only if you plan to label/archive messages in future.)*
6. Add yourself as a **Test user** on the last screen.

#### Step 4 — Create OAuth 2.0 Credentials

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Desktop app**.
3. Name: `gmail-analyzer-desktop`.
4. Click **Create** then **Download JSON**.
5. Rename the downloaded file to **`credentials.json`** and keep it safe — this is your client secret.

> ⚠️ **Never commit `credentials.json` or `token.json` to version control.**  
> Add both to `.gitignore`.

---

### Gmail MCP Server

The [Gmail MCP server](https://github.com/modelcontextprotocol/servers/tree/main/src/gmail) runs locally and bridges JSON-RPC calls to the Gmail REST API.

```bash
# Install globally with npm (or use npx per-run)
npm install -g @modelcontextprotocol/server-gmail

# Place credentials.json in the directory you'll run from, then:
mcp-server-gmail
# → Listening on http://localhost:3000
```

On first run the server will open a browser window for the OAuth flow.  
After you authorise, a `token.json` is saved next to `credentials.json` for future runs.

**Environment variables (optional overrides):**

| Variable | Default | Purpose |
|----------|---------|---------|
| `GMAIL_MCP_PORT` | `3000` | Port the server listens on |
| `GMAIL_CREDENTIALS_PATH` | `./credentials.json` | Path to OAuth client secret |
| `GMAIL_TOKEN_PATH` | `./token.json` | Path to stored OAuth token |

If your MCP server runs on a different host/port, set `GMAIL_MCP_URL` before running the analyzer:

```bash
export GMAIL_MCP_URL=http://192.168.1.50:3000
```

*(The `GmailMCPClient` class reads this from its `base_url` parameter, which you can pass from an env var in your launcher script.)*

---

### Ollama & llama3.2:1b

#### Install and start Ollama

```bash
# macOS / Linux
curl -fsSL https://ollama.com/install.sh | sh

# Start the server (runs on http://localhost:11434 by default)
ollama serve
```

#### Pull the model

```bash
ollama pull llama3.2:1b
```

This downloads ~1.3 GB.  Confirm it's available:

```bash
ollama list
# NAME            ID            SIZE   MODIFIED
# llama3.2:1b     ...           1.3 GB  ...
```

#### Configuration notes for llama3.2:1b

`llama3.2:1b` is optimized for edge/local use.  Key characteristics and how the analyzer is tuned for it:

| Parameter | Value used | Rationale |
|-----------|-----------|-----------|
| `num_ctx` | 4096 | Default context window; fits most email threads comfortably |
| `temperature` | 0.2 | Low temperature keeps responses factual and structured |
| `max_tokens` | 1024 (Ollama default) | Sufficient for summaries and troubleshooting reports |
| Timeout | 120 s | 1b is fast on CPU (typically 5–30 s); 120 s gives headroom |

**Performance expectations:**

- On an Apple M-series chip (8 GB RAM): ~50–80 tokens/s → response in ~5 s.
- On a mid-range CPU (no GPU): ~10–20 tokens/s → response in ~15–30 s.
- On GPU (CUDA/Metal offload): ~100–200 tokens/s.

**Reducing latency tips:**

- Keep bodies short: the `MAX_EMAIL_BODY_CHARS` config (default 6 000) truncates before sending.
- Use `SUMMARIZE` or `HEADERS` mode rather than `FULL` when throughput matters.
- For long threads, summarize each message independently rather than concatenating all bodies.

**Using a larger model:**  
Change `Config.OLLAMA_MODEL` to e.g. `"llama3.2:3b"` for better quality at the cost of ~3× the RAM and latency.

---

### Project Installation

```bash
# Clone / copy the project
cd gmail-analyzer

# Create venv and install all dependencies (including dev)
uv sync

# Verify
uv run python -c "import gmail_analyzer; print('OK')"
```

---

## Usage

Start the Gmail MCP server and Ollama first, then:

```bash
# Summarize the 5 most recent messages from a sender
uv run gmail-analyzer search "from:boss@example.com" --max 5 --mode summarize

# Full analysis (summary + headers + MIME + troubleshoot) of one message
uv run gmail-analyzer message 18f3a2b1c0d9e8f7 --mode full

# Analyze an entire thread
uv run gmail-analyzer thread 18f3a2b1c0d9e8f7 --mode summarize

# Only check headers and authentication for suspicious mail
uv run gmail-analyzer message 18f3a2b1c0d9e8f7 --mode headers

# Troubleshoot a bounced / delayed message
uv run gmail-analyzer message 18f3a2b1c0d9e8f7 --mode troubleshoot
```

**Finding a message ID or thread ID:**  
In Gmail's web UI, open a message and look at the URL:
`https://mail.google.com/mail/u/0/#inbox/18f3a2b1c0d9e8f7`
The hex string at the end is the message ID.

---

## Analysis Modes

| Mode | LLM used? | What it does |
|------|-----------|--------------|
| `summarize` | ✅ | 3–5 sentence summary, key actions, deadlines |
| `headers` | ❌ | Authentication (DKIM/SPF/DMARC), spam flags, delivery hops, date sanity, Reply-To mismatch, mailing-list headers |
| `mime` | ❌ | MIME tree: part types, attachments, risky MIME types, deep nesting |
| `troubleshoot` | ✅ | Heuristic warnings **+** LLM analysis of root cause and remediation |
| `full` | ✅ (×2) | All of the above |

---

## Configuration Reference

All config lives in the `Config` class at the top of `gmail_analyzer.py`.  
Override by subclassing or by setting values before instantiation.

```python
class Config:
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.2:1b"
    OLLAMA_TIMEOUT: float = 120.0
    OLLAMA_NUM_CTX: int = 4096
    OLLAMA_TEMPERATURE: float = 0.2
    MAX_EMAIL_BODY_CHARS: int = 6_000
    MCP_TIMEOUT: float = 30.0
```

Future versions should read these from environment variables or a `.env` file (see [Potential Improvements](#potential-improvements)).

---

## Developer Notes

### Key design decisions

**Async-first with httpx**  
Both `GmailMCPClient` and `OllamaClient` use `httpx.AsyncClient`.  This allows future batching of multiple messages concurrently without blocking.  The CLI uses `asyncio.run()` as the sync entry point.

**Pure-Python heuristics, LLM only for synthesis**  
Authentication checks, spam flag detection, MIME tree walking, and delivery-hop counting are deterministic and don't need an LLM.  The LLM is invoked only for natural-language summarization and for synthesizing the troubleshooting narrative from structured findings.  This keeps costs (CPU time, tokens) low and makes the heuristics unit-testable without mocking Ollama.

**`EmailMessage` is a data boundary**  
All Gmail API specifics (base64 decoding, nested `parts`, header list format) are encapsulated in `EmailParser`.  The rest of the codebase works with the clean `EmailMessage` dataclass — easier to swap in a different mail source later.

**`respx` for HTTP mocking in tests**  
Rather than monkeypatching `httpx.AsyncClient`, we use [respx](https://lundberg.github.io/respx/) to intercept HTTP calls at the transport layer.  This gives realistic coverage of request/response handling without network access.

### Adding a new analysis mode

1. Add a value to `AnalysisMode`.
2. Write a new `XyzAnalyzer` class with an `analyze(msg)` method.
3. Call it inside `GmailAnalyzer._run_analysis` under the new mode.
4. Add tests in `tests/test_gmail_analyzer.py`.

### Error taxonomy

| Exception | Meaning |
|-----------|---------|
| `GmailMCPError` | MCP server unreachable, returned an RPC error, or returned empty data |
| `OllamaError` | Ollama unreachable, model missing, timeout, or HTTP error |

Both are intentionally distinct so callers can handle connectivity vs model issues separately.

---

## Running Tests

```bash
# Run the full suite
uv run pytest -v

# Run with coverage
uv run pytest --cov=gmail_analyzer --cov-report=term-missing

# Run a single test class
uv run pytest -v tests/test_gmail_analyzer.py::TestHeaderAnalyzer

# Run with log output visible
uv run pytest -v -s
```

The suite is fully offline — all HTTP calls are intercepted by `respx` mocks.

---

## Potential Improvements

### Short term

- **Environment-variable config** — Read `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `GMAIL_MCP_URL` etc. from env vars (via `python-dotenv` or `pydantic-settings`) instead of hardcoded class attributes.
- **JSON / structured output** — Add `--output json` flag to emit machine-readable `AnalysisResult` objects for piping into other tools.
- **Streaming Ollama responses** — Switch `stream=True` and print tokens as they arrive for a more responsive CLI feel.
- **Attachment content analysis** — For text-based attachments (`.txt`, `.csv`, `.eml`), pass content to Ollama for summarization.

### Medium term

- **Conversation threading** — When analyzing a thread, build a single coherent prompt from all messages (preserving chronological order) so the LLM can reason about the full conversation arc rather than summarizing each message in isolation.
- **Async concurrency** — Use `asyncio.gather` to fetch and analyze multiple messages in parallel instead of sequential `for` loops.
- **Caching** — Cache parsed `EmailMessage` objects (keyed by message ID + version) to avoid re-fetching unchanged messages.
- **Interactive TUI** — Replace the bare-stdout CLI with a [Textual](https://textual.textualize.io/) or [rich](https://github.com/Textualize/rich) dashboard for browsing results.
- **Custom prompts** — Allow users to supply their own system prompt or analysis question via `--prompt` flag.

### Longer term

- **Embeddings-based search** — Embed message bodies with a local embedding model and store in a vector DB (e.g. ChromaDB) to enable semantic search across a mailbox.
- **Scheduled monitoring** — Wrap the skill as a background service that polls for new messages matching a query and alerts on deliverability regressions.
- **Multi-account support** — Extend `GmailMCPClient` to support multiple OAuth tokens and route requests to the correct account.
- **Larger model fallback** — Auto-detect available Ollama models and fall back to `llama3.2:3b` or `llama3.1:8b` when the 1b model confidence is low (detectable via repetition or very short responses).
- **Postfix / Exim log correlation** — For self-hosted mail servers, correlate message headers with MTA logs to pinpoint exact delivery failures.
- **DMARC aggregate report parsing** — Parse `rua` DMARC aggregate reports (XML attachments) and generate human-readable summaries.
