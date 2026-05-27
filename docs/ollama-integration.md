# Ollama Integration

MailMind is designed so that locally-running Ollama models can query it
over HTTP to retrieve relevant email context before generating a response.
This page explains the integration patterns.

---

## How MailMind uses Ollama

MailMind calls Ollama for two purposes:

| Purpose | Endpoint | Trigger |
|---------|----------|---------|
| **Email summarisation** | `POST /api/generate` | `POST /analyze` with mode `summarize` or `full` |
| **Troubleshooting analysis** | `POST /api/generate` | `POST /analyze` with mode `troubleshoot` or `full` |

Scheduled ingest does **not** call Ollama — it only fetches and indexes
emails. Ollama is only invoked during on-demand analysis.

---

## How Ollama models query MailMind

Ollama models can retrieve email context from MailMind by calling
`POST /query` before generating a response. This is the retrieval-augmented
generation (RAG) pattern:

```
User prompt
    │
    ▼
Ollama model decides it needs email context
    │
    ▼
POST http://127.0.0.1:8765/query  {"query": "invoice March", "top_k": 3}
    │
    ▼
MailMind returns relevant chunks from the local SQLite store
    │
    ▼
Model uses chunks as context to generate a grounded response
```

---

## Wiring Ollama to MailMind via tool use

Models that support function/tool calling can be given a `query_email`
tool definition that calls MailMind automatically.

### Example: Ollama tool definition

```json
{
  "name": "query_email",
  "description": "Search your local email store for messages related to a topic. Returns the most relevant email excerpts.",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "Natural language search query, e.g. 'AWS invoice March 2024' or 'meeting with Alice next week'"
      },
      "top_k": {
        "type": "integer",
        "description": "Maximum number of email excerpts to return (1-10, default 3)",
        "default": 3
      }
    },
    "required": ["query"]
  }
}
```

### Example: Python client calling MailMind after tool invocation

```python
import httpx
import json

MAILMIND_URL = "http://127.0.0.1:8765"
OLLAMA_URL   = "http://127.0.0.1:11434"

def call_mailmind_query(query: str, top_k: int = 3) -> list[dict]:
    """Call MailMind's /query endpoint and return chunks."""
    resp = httpx.post(
        f"{MAILMIND_URL}/query",
        json={"query": query, "top_k": top_k},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["chunks"]

def format_chunks_for_context(chunks: list[dict]) -> str:
    """Format retrieved chunks as a context block for the LLM."""
    parts = []
    for i, chunk in enumerate(chunks, 1):
        m = chunk["meta"]
        parts.append(
            f"[Email {i}] From: {m['sender']}  Subject: {m['subject']}  Date: {m['date']}\n"
            f"{chunk['text']}\n"
        )
    return "\n---\n".join(parts)

def ask_with_email_context(question: str) -> str:
    """Answer a question using email context retrieved from MailMind."""
    # 1. Retrieve relevant context
    chunks = call_mailmind_query(question, top_k=3)
    context = format_chunks_for_context(chunks)

    # 2. Build a prompt that includes the context
    prompt = f"""You are a helpful email assistant. Use the following email excerpts
to answer the user's question. If the emails do not contain relevant information,
say so clearly.

Email context:
{context}

Question: {question}

Answer:"""

    # 3. Call Ollama
    resp = httpx.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model":  "llama3.2:1b",
            "prompt": prompt,
            "stream": False,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["response"].strip()

# Usage
answer = ask_with_email_context("What did AWS charge me last month?")
print(answer)
```

---

## Wiring via the Node.js MCP server

If you are using the Node.js MCP server (`mailmind/tools/`), the
`summarize_email.js` tool already calls MailMind's `/query` endpoint.
The response format is identical to what `summarize_email.js` expects:

```json
{
  "chunks": [
    {
      "text":  "...",
      "score": 4.821,
      "meta":  {"id": "...", "subject": "...", "sender": "...", "date": "...", "thread_id": "...", "chunk_index": 0}
    }
  ]
}
```

Update `RAG_INGEST_SCRIPT` in the Node.js `.env` to point to MailMind's
HTTP endpoint instead of a Python subprocess:

```javascript
// tools/summarize_email.js — replace the _queryRag subprocess call with:
async function queryRag(query, topK) {
  const resp = await fetch("http://127.0.0.1:8765/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, top_k: topK }),
  });
  if (!resp.ok) throw new Error(`MailMind /query error: ${resp.status}`);
  const data = await resp.json();
  return data.chunks;
}
```

---

## Configuring the Ollama model

MailMind uses whatever model is set in `OLLAMA_MODEL`. Pull the model before
starting the service:

```bash
ollama pull llama3.2:1b    # default, fast, low memory
ollama pull llama3.1:8b    # higher quality analysis
ollama pull mistral:7b     # strong instruction following
```

To switch models without restarting the service, update `OLLAMA_MODEL` in
`.env` and restart. The model is read from `Settings` on each `generate()`
call, so a restart is required to pick up `.env` changes.

---

## Context window management

By default, MailMind forwards up to `GMAIL_MAX_BODY_CHARS` (6000)
characters of body text per message to Ollama for analysis. For the
`/query` endpoint, the `top_k` parameter controls how many chunks are
returned.

Recommended values by model context window:

| Model context | `OLLAMA_NUM_CTX` | `top_k` | `GMAIL_MAX_BODY_CHARS` |
|---------------|-----------------|---------|----------------------|
| 4096 tokens | `4096` | 3 | `4000` |
| 8192 tokens | `8192` | 5 | `6000` |
| 32768 tokens | `32768` | 10 | `12000` |

The LLM prompt for summarisation includes: system prompt (~80 tokens),
thread metadata (~50 tokens), and up to 5 messages at `GMAIL_MAX_BODY_CHARS`
each. Size your context window accordingly.

---

## Checking available models

```bash
# Via MailMind
curl http://127.0.0.1:8765/models

# Directly via Ollama
curl http://localhost:11434/api/tags
```

If Ollama is not running, `GET /models` returns HTTP 503 with a helpful
error message. The service itself continues running — Ollama being offline
only affects endpoints that call it (`/analyze`).
