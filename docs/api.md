# HTTP API Reference

MailMind exposes a JSON REST API on `http://127.0.0.1:8765` by default.
An interactive Swagger UI is available at `/docs` and a ReDoc view at `/redoc`.

All request bodies must be sent as `Content-Type: application/json`.
All responses are JSON. Error responses follow the FastAPI default shape:

```json
{"detail": "Human-readable error message"}
```

---

## Endpoints

| Method | Path | Tag | Purpose |
|--------|------|-----|---------|
| `GET` | `/health` | ops | Liveness probe |
| `GET` | `/status` | ops | Service state snapshot |
| `GET` | `/models` | ollama | List available Ollama models |
| `POST` | `/query` | rag | Retrieve relevant email chunks |
| `POST` | `/analyze` | analysis | Fetch and analyse a Gmail thread |
| `POST` | `/ingest` | scheduler | Trigger an immediate ingest run |
| `POST` | `/refresh` | rag | Re-index the RAG store |

---

## `GET /health`

Liveness probe. Returns 200 as long as the process is running.

**Response**

```json
{"status": "ok"}
```

---

## `GET /status`

Returns a snapshot of the service state: scheduler activity, last ingest
summary, RAG store size, and uptime.

**Response**

```json
{
  "status": "running",
  "uptime_s": 3842.1,
  "store_stats": {
    "emails": 1240,
    "chunks": 5863,
    "term_entries": 98241
  },
  "last_ingest": {
    "started_at":  "2024-06-01T07:00:02Z",
    "finished_at": "2024-06-01T07:01:45Z",
    "elapsed_s":   103.4,
    "fetched":     87,
    "new":         12,
    "updated":     75,
    "errors":      0,
    "query":       "category:inbox -category:trash"
  },
  "next_ingest": "2024-06-02T07:00:00Z",
  "scheduler_enabled": true,
  "model": "llama3.2:1b"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `uptime_s` | float | Seconds since the service started. |
| `store_stats.emails` | int | Total email records in the RAG store. |
| `store_stats.chunks` | int | Total text chunks indexed. |
| `store_stats.term_entries` | int | Total term-frequency index entries. |
| `last_ingest` | object \| null | Result of the most recent ingest run, or `null` if none has completed yet. |
| `next_ingest` | ISO 8601 \| null | Scheduled time of the next automatic ingest, or `null` if scheduling is disabled. |

---

## `GET /models`

Lists all models currently available on the local Ollama server and
indicates which model MailMind is configured to use.

**Response**

```json
{
  "models": ["llama3.2:1b", "mistral:7b", "llama3.1:8b"],
  "configured": "llama3.2:1b"
}
```

**Error responses**

| Status | Condition |
|--------|-----------|
| `503` | Ollama server is unreachable. |

---

## `POST /query`

The primary endpoint for Ollama models. Queries the local RAG store and
returns the most relevant email chunks for the given text.

The response format is compatible with the `summarize_email.js` MCP tool
contract, so no adapter is needed when connecting from the Node.js MCP server.

**Request body**

```json
{
  "query": "AWS invoice March 2024",
  "top_k": 3
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | *required* | Free-text search query. |
| `top_k` | integer | `3` | Maximum number of chunks to return. |

**Response**

```json
{
  "query": "AWS invoice March 2024",
  "top_k": 3,
  "chunks": [
    {
      "text": "Your AWS bill for March 2024 is $142.38. This invoice covers EC2 usage in us-east-1...",
      "score": 4.821,
      "meta": {
        "id":          "18f3a2b4c5d6e7f8",
        "subject":     "Your AWS Bill is ready",
        "sender":      "billing@amazon.com",
        "date":        "Mon, 01 Apr 2024 09:15:00 +0000",
        "thread_id":   "18f3a2b4c5d6e7f8",
        "chunk_index": 0
      }
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `chunks[].text` | string | The raw chunk body text. |
| `chunks[].score` | float | BM25 relevance score. Higher is more relevant. |
| `chunks[].meta.id` | string | Gmail message ID. |
| `chunks[].meta.subject` | string | Email subject line. |
| `chunks[].meta.sender` | string | `From` header value. |
| `chunks[].meta.date` | string | `Date` header value. |
| `chunks[].meta.thread_id` | string | Gmail thread ID. |
| `chunks[].meta.chunk_index` | int | Position of this chunk within the message (0-based). |

**Error responses**

| Status | Condition |
|--------|-----------|
| `422` | `query` is empty or missing. |

---

## `POST /analyze`

Fetches a Gmail thread by ID from the live Gmail API, runs the requested
analysis passes using the local Ollama model, and returns a structured result.

**Request body**

```json
{
  "thread_id": "18f3a2b4c5d6e7f8",
  "mode": "full"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `thread_id` | string | *required* | Gmail thread ID. |
| `mode` | string | `"summarize"` | Analysis mode. One of `summarize`, `headers`, `mime`, `troubleshoot`, `full`. |

### Analysis modes

| Mode | LLM calls | What is populated |
|------|-----------|-------------------|
| `summarize` | 1 | `summary`, `llm_response`, `warnings` |
| `headers` | 0 | `header_report`, `warnings` |
| `mime` | 0 | `mime_report`, `warnings` |
| `troubleshoot` | 1 | `troubleshoot_report`, `llm_response`, `warnings` |
| `full` | 2 | All fields |

**Response**

```json
{
  "thread_id":     "18f3a2b4c5d6e7f8",
  "subject":       "Q2 budget review",
  "mode":          "full",
  "timestamp":     "2024-06-01T14:32:11Z",
  "message_count": 4,
  "summary":       "A four-message thread between Alice and Bob discussing the Q2 budget...",
  "llm_response":  "A four-message thread between Alice and Bob discussing the Q2 budget...",
  "warnings":      [],
  "header_report": {
    "msg_001": {
      "authentication": {"dkim": "pass", "spf": "pass", "dmarc": "pass", "arc": "absent"},
      "spam_headers": {},
      "delivery_hop_estimate": 2,
      "list_headers": {},
      "date": "Mon, 01 Apr 2024 09:15:00 +0000",
      "warnings": []
    }
  },
  "mime_report": [
    {"mime_type": "multipart/mixed", "is_attachment": false, "filename": null, "size_bytes": null, "warnings": []},
    {"mime_type": "attachment",      "is_attachment": true,  "filename": "budget.xlsx", "size_bytes": null, "warnings": []}
  ]
}
```

**Error responses**

| Status | Condition |
|--------|-----------|
| `401` | OAuth token is missing or expired. Run `mailmind auth`. |
| `422` | Invalid `mode` value. |
| `502` | Gmail API returned an error (thread not found, quota exceeded, etc.). |
| `503` | Ollama server is unreachable or the model is unavailable. |

---

## `POST /ingest`

Triggers an immediate ingest run outside the normal schedule. The request
blocks until the run completes and returns the result summary.

**Request body** *(all fields optional)*

```json
{
  "query": "from:important-client@example.com"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string \| null | `null` | Override the `GMAIL_DEFAULT_QUERY` for this run only. If omitted, the configured default is used. |

**Response**

```json
{
  "started_at":  "2024-06-01T14:00:00Z",
  "finished_at": "2024-06-01T14:01:32Z",
  "elapsed_s":   92.1,
  "fetched":     54,
  "new":         8,
  "updated":     46,
  "errors":      0,
  "query":       "from:important-client@example.com"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `fetched` | int | Total individual messages processed (across all threads). |
| `new` | int | Messages inserted into the store for the first time. |
| `updated` | int | Messages already in the store that were re-indexed with fresh content. |
| `errors` | int | Threads skipped due to Gmail API errors. Individual thread failures do not abort the batch. |

---

## `POST /refresh`

Re-indexes the local RAG store without re-fetching any emails from Gmail.
Use this after changing `RAG_CHUNK_SIZE` or `RAG_CHUNK_OVERLAP`, or after
a database migration.

**Request body** *(all fields optional)*

```json
{
  "since":        "2024-06-01T00:00:00Z",
  "until":        "2024-06-30T23:59:59Z",
  "full_reindex": false,
  "dry_run":      false
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `since` | ISO 8601 \| null | `null` | Re-index only emails ingested on or after this timestamp. `null` means no lower bound. |
| `until` | ISO 8601 \| null | `null` | Re-index only emails ingested on or before this timestamp. `null` means now. |
| `full_reindex` | bool | `false` | When `true`, re-index the entire corpus. Ignores `since`/`until`. |
| `dry_run` | bool | `false` | When `true`, count the emails in scope without making any changes. |

**Response**

```json
{
  "reindexed":   1240,
  "status":      "ok",
  "duration_ms": 843,
  "detail":      "Full corpus re-index",
  "store_stats": {
    "emails":       1240,
    "chunks":       5863,
    "term_entries": 98241
  }
}
```

---

## Using the API with curl

```bash
BASE=http://127.0.0.1:8765

# Liveness
curl -s $BASE/health

# Service state
curl -s $BASE/status | python -m json.tool

# Query
curl -s -X POST $BASE/query \
  -H "Content-Type: application/json" \
  -d '{"query": "invoice payment overdue", "top_k": 5}' \
  | python -m json.tool

# Trigger ingest for a specific sender
curl -s -X POST $BASE/ingest \
  -H "Content-Type: application/json" \
  -d '{"query": "from:finance@company.com"}' \
  | python -m json.tool

# Analyse a thread
curl -s -X POST $BASE/analyze \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "18f3a2b4c5d6e7f8", "mode": "summarize"}' \
  | python -m json.tool

# Full reindex (dry run first)
curl -s -X POST $BASE/refresh \
  -H "Content-Type: application/json" \
  -d '{"full_reindex": true, "dry_run": true}' \
  | python -m json.tool
```
