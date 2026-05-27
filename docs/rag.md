# RAG Store

MailMind's retrieval layer is a local SQLite database with a BM25-style
term-frequency scorer. It requires no external dependencies beyond the Python
standard library.

---

## Database schema

```sql
-- One row per email message (not per thread)
CREATE TABLE emails (
    id           TEXT PRIMARY KEY,      -- Gmail message ID
    thread_id    TEXT NOT NULL,
    subject      TEXT NOT NULL,
    sender       TEXT NOT NULL,
    date         TEXT NOT NULL,
    raw_headers  TEXT NOT NULL,         -- JSON-encoded header dict
    ingested_at  REAL NOT NULL          -- POSIX timestamp
);

-- Each email is split into overlapping word-window chunks
CREATE TABLE chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id    TEXT    NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,       -- 0-based position within the email
    body        TEXT    NOT NULL,
    word_count  INTEGER NOT NULL
);

-- Inverted index: how often each term appears in each chunk
CREATE TABLE term_freq (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    term     TEXT    NOT NULL,
    freq     INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, term)
);
```

Foreign keys cascade on delete, so removing an email row automatically
cleans up its chunks and term-frequency entries.

---

## Ingestion pipeline

When `RagStore.ingest_email(record)` is called:

1. **Upsert the email row** — if the message ID already exists, the metadata
   is updated. If not, a new row is inserted.

2. **Delete stale chunks** — old chunks and their term-frequency rows are
   removed via the cascade constraint.

3. **Chunk the text** — the subject and body are concatenated and split into
   overlapping word-window chunks with `chunk_text()`.

4. **Tokenise and index** — each chunk is tokenised, stop words are removed,
   and term frequencies are written to the `term_freq` table.

This means re-ingesting a message (e.g. when a thread is updated) is
fully idempotent: the old index is discarded and rebuilt from the current
body text.

---

## Text processing

### Tokenisation

`tokenise(text)` normalises text for indexing:

1. Lowercase the input.
2. Replace all non-alphanumeric characters with spaces.
3. Split on whitespace.
4. Discard single-character tokens.
5. Discard tokens in the stop-word set.

```python
tokenise("Your AWS Bill for March 2024 is $142.38")
# → ["aws", "bill", "march", "2024", "142", "38"]
```

The stop-word set covers common English function words (`the`, `and`, `from`,
`is`, ...) and common email openers (`hi`, `hello`, `thanks`, `regards`, ...).

### Chunking

`chunk_text(text, chunk_size=400, overlap=40)` splits text into overlapping
word windows:

```python
chunk_text("a b c d e f g h", chunk_size=4, overlap=2)
# → ["a b c d", "c d e f", "e f g h"]
```

Overlap ensures that sentences spanning a boundary appear in both adjacent
chunks, which improves recall for short queries that might otherwise miss a
key phrase that was split across a boundary.

**Tuning**

| Setting | Effect |
|---------|--------|
| Smaller `RAG_CHUNK_SIZE` | More, shorter chunks. Better precision for narrow queries. More rows in `term_freq`. |
| Larger `RAG_CHUNK_SIZE` | Fewer, longer chunks. Better recall for broad queries. Fewer rows. |
| Larger `RAG_CHUNK_OVERLAP` | More shared context between chunks. Improves recall. Increases total chunk count. |

---

## BM25 retrieval

`RagStore.query(query_text, top_k)` uses a BM25-style scorer:

```
score(chunk) = Σ  freq(term, chunk) × IDF(term)

IDF(term) = log( (N - df + 0.5) / (df + 0.5) + 1 )
```

Where:
- `N` = total number of chunks in the store
- `df` = number of chunks that contain the term
- `freq(term, chunk)` = how many times the term appears in the chunk

Terms that appear in many chunks (low IDF) contribute little to the score.
Terms that appear in only a few chunks (high IDF) contribute heavily.

### Result shape

```json
{
  "text":  "Your AWS bill for March 2024 is $142.38...",
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
```

---

## Re-indexing

Re-indexing rebuilds the `term_freq` table from the stored chunk bodies
without re-fetching any emails from Gmail. Use it when:

- You change `RAG_CHUNK_SIZE` or `RAG_CHUNK_OVERLAP` and want to rechunk
  existing emails. Note: rechunking requires re-running ingest, not just
  re-indexing.
- You update the stop-word list in `rag.py` and want the new list applied
  to existing data.
- The `term_freq` table becomes corrupt or out of sync.

```bash
# Check what would be re-indexed (dry run)
curl -X POST http://127.0.0.1:8765/refresh \
  -H "Content-Type: application/json" \
  -d '{"full_reindex": true, "dry_run": true}'

# Re-index everything
curl -X POST http://127.0.0.1:8765/refresh \
  -H "Content-Type: application/json" \
  -d '{"full_reindex": true}'

# Re-index only emails from the last 7 days
curl -X POST http://127.0.0.1:8765/refresh \
  -H "Content-Type: application/json" \
  -d '{
    "since": "2024-05-25T00:00:00Z",
    "until": "2024-06-01T00:00:00Z"
  }'
```

---

## Store statistics

```bash
curl http://127.0.0.1:8765/status | python -m json.tool
```

The `store_stats` field shows:

```json
{
  "emails":       1240,
  "chunks":       5863,
  "term_entries": 98241
}
```

A typical 400-word chunk produces ~120 unique index terms (after stop-word
removal). Expect roughly 4–8 chunks per email depending on body length.

---

## Upgrade to semantic search

The current BM25 implementation is intentionally simple. To upgrade to
vector embeddings:

### Option A — sentence-transformers + BLOB column

1. Uncomment `sentence-transformers` in `pyproject.toml` and run `uv sync`.

2. Add an `embedding BLOB` column to the `chunks` table.

3. In `RagStore.ingest_email()`, after creating each chunk, generate and
   store an embedding:
   ```python
   from sentence_transformers import SentenceTransformer
   _model = SentenceTransformer("all-MiniLM-L6-v2")

   embedding = _model.encode(chunk_body)
   # store as pickle or numpy bytes in the BLOB column
   ```

4. Replace the BM25 scoring loop in `RagStore.query()` with cosine
   similarity:
   ```python
   import numpy as np
   query_vec = _model.encode(query_text)
   # load all embeddings, compute cosine similarity, sort
   ```

### Option B — ChromaDB

1. Uncomment `chromadb` in `pyproject.toml` and run `uv sync`.

2. Replace `RagStore` with a class that wraps a ChromaDB `Collection`
   and implements the same interface:
   - `open()` / `close()` / `__enter__` / `__exit__`
   - `ingest_email(record) -> bool`
   - `query(query_text, top_k) -> list[dict]`
   - `reindex_range(since_ts, until_ts, dry_run) -> int`
   - `full_reindex(dry_run) -> int`
   - `stats() -> dict`

   The rest of the codebase (`scheduler.py`, `service.py`, `tests/`) will
   work without modification because they call only the public interface.
