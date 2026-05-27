# Architecture

## Overview

MailMind is a single long-running Python process with three independent
layers that share a common set of resources:

```
┌─────────────────────────────────────────────────────────────┐
│                      MailMindService                         │
│                                                              │
│  ┌──────────────────┐   ┌──────────────────┐                │
│  │ IngestionScheduler│   │   FastAPI app     │                │
│  │ (APScheduler)     │   │   (Uvicorn)       │                │
│  │                  │   │                  │                │
│  │ cron / interval  │   │ /health  /status  │                │
│  │ fires _ingest_   │   │ /query   /analyze │                │
│  │ job on schedule  │   │ /ingest  /refresh │                │
│  └───────┬──────────┘   └────────┬─────────┘                │
│          │                       │                           │
│          ▼                       ▼                           │
│  ┌───────────────────────────────────────────────────────┐  │
│  │              Shared resource layer                     │  │
│  │                                                        │  │
│  │  GmailClient   OllamaClient   RagStore (SQLite)        │  │
│  │  OAuthTokenManager            GmailAnalyzer            │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
   Gmail REST API      Ollama HTTP API       mailmind.db
```

All resources are created once at startup and held for the service lifetime.
There is one database connection, one Gmail HTTP client, and one Ollama HTTP
client — no connection pooling complexity, no shared-state race conditions.

---

## Module map

```
mailmind/
├── __init__.py        Package version
├── config.py          Settings dataclass — all env var reading in one place
├── models.py          Shared domain types: enums, exceptions, dataclasses
├── oauth.py           OAuthTokenManager — token lifecycle and persistence
├── gmail.py           GmailClient + ThreadParser — Gmail REST API access
├── ollama.py          OllamaClient — local Ollama HTTP interactions
├── rag.py             RagStore, tokenise, chunk_text — SQLite BM25 store
├── analysis.py        HeaderAnalyzer, MIMEAnalyzer, TroubleshootAnalyzer,
│                      GmailAnalyzer — analysis orchestration
├── scheduler.py       IngestionScheduler — APScheduler background ingest
└── service.py         MailMindService, FastAPI app, CLI entry point
```

### Dependency graph

```
service.py
  ├── config.py        (Settings)
  ├── models.py        (AnalysisMode, exceptions)
  ├── oauth.py         (OAuthTokenManager)
  │     └── config.py
  │     └── models.py
  ├── gmail.py         (GmailClient, ThreadParser)
  │     ├── config.py
  │     ├── models.py
  │     └── oauth.py
  ├── ollama.py        (OllamaClient)
  │     ├── config.py
  │     └── models.py
  ├── rag.py           (RagStore, tokenise, chunk_text)
  │     └── config.py
  ├── analysis.py      (GmailAnalyzer, *Analyzer classes)
  │     ├── config.py
  │     ├── models.py
  │     ├── gmail.py
  │     └── ollama.py
  └── scheduler.py     (IngestionScheduler)
        ├── config.py
        ├── models.py
        ├── gmail.py
        └── rag.py
```

`models.py` and `config.py` have no intra-package imports, making them
safe to import from any module without creating circular dependencies.

---

## Data flow

### Scheduled ingest

```
IngestionScheduler._ingest_job()
  │
  ├── GmailClient.search_threads(default_query)
  │     └── Returns list of thread stubs [{id, threadId}]
  │
  ├── for each stub:
  │     GmailClient.get_thread(thread_id)
  │       └── Returns full thread with message payloads
  │
  │     ThreadParser.parse_thread(raw)
  │       └── Returns ThreadSummary with MessageSummary list
  │
  │     for each message:
  │       RagStore.ingest_email({id, threadId, headers, body})
  │         ├── Upsert emails row
  │         ├── Delete old chunks (cascades to term_freq)
  │         ├── chunk_text(subject + body)
  │         └── tokenise each chunk → INSERT term_freq rows
  │
  └── Returns {fetched, new, updated, errors, elapsed_s, ...}
```

### Query (RAG retrieval)

```
POST /query  {"query": "invoice March", "top_k": 3}
  │
  └── RagStore.query(query_text, top_k)
        ├── tokenise(query_text)  →  ["invoice", "march"]
        ├── For each unique term: compute IDF from term_freq table
        ├── Accumulate BM25 score per chunk_id
        ├── Sort descending, take top_k
        └── JOIN chunks + emails → [{text, score, meta}]
```

### On-demand analysis

```
POST /analyze  {"thread_id": "18f3a2...", "mode": "full"}
  │
  └── GmailAnalyzer.analyze_thread(thread_id, AnalysisMode.FULL)
        ├── GmailClient.get_thread(thread_id)
        ├── ThreadParser.parse_thread(raw)
        │
        ├── [SUMMARIZE] GmailAnalyzer._summarize(thread)
        │     └── OllamaClient.generate(prompt)
        │
        ├── [HEADERS]   HeaderAnalyzer.analyze(msg) for each msg
        │
        ├── [MIME]      MIMEAnalyzer.analyze(msg) for each msg
        │
        └── [TROUBLESHOOT] TroubleshootAnalyzer.analyze(thread)
              ├── HeaderAnalyzer.analyze() + MIMEAnalyzer.analyze()
              └── OllamaClient.generate(combined_prompt)
```

---

## Key design decisions

### Settings as an injectable dataclass

`Config` in the original codebase evaluated `os.getenv()` at class definition
time. Every test that needed different settings had to patch `os.environ`
*before import*, which is fragile and order-dependent.

`Settings` is a plain `@dataclass` with no side effects at definition time.
In production: `Settings.from_env()`. In tests: `Settings(rag_db_path=":memory:", ...)`.
This makes configuration completely deterministic.

### One database connection for the service lifetime

`RagStore.open()` is called once in `MailMindService.start()` and the
connection is held until `MailMindService.stop()`. Both the scheduler and
the HTTP handlers share the same connection object. SQLite's WAL journal mode
handles concurrent reads safely; we never have two writers at once because
the asyncio event loop is single-threaded.

### Scheduler fires on startup

APScheduler's `next_run_time=datetime.now(UTC)` argument causes the first
job execution to happen immediately when the service starts, rather than
waiting for the first scheduled tick. This means the store is populated on
day one without manual intervention.

### Individual thread failures do not abort the batch

The ingest job catches `GmailError` and `OAuthError` per-thread, logs a
warning, increments an error counter, and continues with the remaining
threads. A transient network error on one message never stops the rest of
the batch from being indexed.

### `GmailMCPClient` → `GmailClient`

The original class was named `GmailMCPClient` because an earlier version
routed through Google's hosted MCP server. The current implementation calls
the Gmail REST API directly. The rename removes the misleading MCP reference.

### BM25 over a vector store

The RAG store uses a BM25-style term-frequency / inverse document frequency
scorer implemented with a SQLite `term_freq` table. This was chosen
deliberately:

- **Zero external dependencies**: no separate process, no network calls, no
  Python packages beyond the stdlib.
- **Predictable**: exact term matching is transparent and debuggable.
- **Sufficient**: for a personal inbox, keyword retrieval works well.

The upgrade path to semantic search (sentence-transformers + ChromaDB/FAISS)
is documented in [rag.md](rag.md) and signposted in the source code.

---

## Concurrency model

MailMind runs on a single asyncio event loop managed by Uvicorn. All
I/O — Gmail API calls, Ollama calls, SQLite writes — is performed with
`async/await`. The APScheduler `AsyncIOScheduler` runs scheduled jobs as
coroutines on the same event loop, so there is no thread safety concern for
the database or HTTP clients.

SQLite operations in `RagStore` are synchronous and therefore blocking.
For a personal-scale inbox (tens of thousands of emails) this is not a
problem in practice — each ingest or query operation completes in
milliseconds. If you need non-blocking SQLite access at scale, replace
`sqlite3` with `aiosqlite`.
