"""
mailmind.rag — local SQLite BM25 retrieval store.

Provides three public symbols consumed by the service layer:

    ``RagStore``       — SQLite-backed email ingest + BM25 query + re-index
    ``tokenise``       — lowercase, strip punctuation, remove stop words
    ``chunk_text``     — overlapping word-window splitting

The store requires zero external dependencies beyond the Python stdlib.

Upgrade path to semantic search
--------------------------------
Replace the body of ``RagStore.query()`` with cosine-similarity lookups
against embeddings stored in a ``BLOB`` column::

    from sentence_transformers import SentenceTransformer
    _model = SentenceTransformer("all-MiniLM-L6-v2")
    query_vec = _model.encode(query_text)
    # ... cosine similarity against stored vectors ...

or swap the backend entirely for ChromaDB / FAISS by replacing ``RagStore``
with a compatible class that implements the same ``ingest_email``, ``query``,
``reindex_range``, ``full_reindex``, and ``stats`` interface.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from mailmind.config import Settings

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Stop-word set
# ─────────────────────────────────────────────────────────────────────────────

_STOP_WORDS: frozenset[str] = frozenset(
    "a an the and or but in on at to for of with by from is are was were be been "
    "have has had do does did will would could should may might shall can i you he "
    "she it we they this that these those not no so if as my your our its re s t "
    "hi hello dear thanks thank regards best please let know just get us me".split()
)


# ─────────────────────────────────────────────────────────────────────────────
# Text utilities
# ─────────────────────────────────────────────────────────────────────────────


def tokenise(text: str) -> list[str]:
    """
    Normalise ``text`` into a list of index terms.

    Steps:
    1. Lowercase.
    2. Replace non-alphanumeric characters with spaces.
    3. Split on whitespace.
    4. Drop single-character tokens and ``_STOP_WORDS`` members.

    Duplicates are preserved so callers can build frequency counts with
    ``collections.Counter``.

    Args:
        text: Raw string (email body, subject, or query).

    Returns:
        List of normalised terms; may be empty.
    """
    import re
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return [w for w in cleaned.split() if len(w) > 1 and w not in _STOP_WORDS]


def chunk_text(
    text: str,
    chunk_size: int = 400,
    overlap: int = 40,
) -> list[str]:
    """
    Split ``text`` into overlapping word-window chunks.

    Overlap ensures sentences spanning a boundary appear in both adjacent
    chunks, improving recall for short queries.

    Args:
        text:       Input string.
        chunk_size: Target word count per chunk.
        overlap:    Words shared between consecutive chunks.  Must be less
                    than ``chunk_size``; clamped internally if not.

    Returns:
        List of non-empty chunk strings; returns ``[]`` for empty input.

    Example::

        >>> chunk_text("a b c d e", chunk_size=3, overlap=1)
        ['a b c', 'c d e']
    """
    words = text.split()
    if not words:
        return []
    step = max(1, chunk_size - overlap)
    chunks: list[str] = []
    for i in range(0, len(words), step):
        chunk = " ".join(words[i : i + chunk_size])
        if chunk:
            chunks.append(chunk)
        if i + chunk_size >= len(words):
            break
    return chunks


def iso_to_timestamp(iso: str | None) -> float | None:
    """
    Parse an ISO 8601 datetime string to a POSIX timestamp.

    Args:
        iso: ISO 8601 string (trailing ``Z`` accepted), or ``None``.

    Returns:
        POSIX timestamp on success, ``None`` if falsy or unparseable.
    """
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        log.warning("iso_to_timestamp: cannot parse %r — ignoring", iso)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# RagStore
# ─────────────────────────────────────────────────────────────────────────────


class RagStore:
    """
    Local SQLite-backed RAG store with BM25-style retrieval.

    Schema
    ------
    .. code-block:: sql

        emails    (id PK, thread_id, subject, sender, date,
                   raw_headers JSON, ingested_at REAL)
        chunks    (id PK AUTOINCREMENT, email_id FK, chunk_index,
                   body TEXT, word_count)
        term_freq (chunk_id FK, term TEXT, freq INTEGER,
                   PRIMARY KEY (chunk_id, term))

    Foreign keys cascade on delete, so removing an email row automatically
    cleans up its chunks and term-frequency entries.

    Lifecycle
    ---------
    Use as a context manager::

        with RagStore(settings) as store:
            store.ingest_email(record)
            results = store.query("invoice payment")

    Or call ``open()`` / ``close()`` explicitly for long-lived instances
    (e.g. the ``MailMindService`` holds one open for its lifetime).
    """

    _DDL = """
        CREATE TABLE IF NOT EXISTS emails (
            id           TEXT PRIMARY KEY,
            thread_id    TEXT NOT NULL DEFAULT '',
            subject      TEXT NOT NULL DEFAULT '',
            sender       TEXT NOT NULL DEFAULT '',
            date         TEXT NOT NULL DEFAULT '',
            raw_headers  TEXT NOT NULL DEFAULT '{}',
            ingested_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id    TEXT    NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            body        TEXT    NOT NULL,
            word_count  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS term_freq (
            chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
            term     TEXT    NOT NULL,
            freq     INTEGER NOT NULL,
            PRIMARY KEY (chunk_id, term)
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_email ON chunks(email_id);
        CREATE INDEX IF NOT EXISTS idx_tf_term      ON term_freq(term);
        CREATE INDEX IF NOT EXISTS idx_emails_date  ON emails(ingested_at);
    """

    def __init__(self, settings: Settings) -> None:
        self._settings  = settings
        self._conn: sqlite3.Connection | None = None

    # ── context manager / lifecycle ───────────────────────────────────────────

    def open(self) -> RagStore:
        """
        Open (or create) the SQLite database and apply the schema.

        Returns:
            ``self``, to allow chained usage: ``store = RagStore(cfg).open()``.
        """
        self._conn = sqlite3.connect(self._settings.rag_db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._DDL)
        self._conn.commit()
        return self

    def close(self) -> None:
        """Close the database connection if open."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> RagStore:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError(
                "RagStore is not open — call open() or use as a context manager."
            )
        return self._conn

    # ── ingest ────────────────────────────────────────────────────────────────

    def ingest_email(self, record: dict[str, Any]) -> bool:
        """
        Ingest one email record into the store.

        The record shape matches the NDJSON produced by ``ingest_mail.js`` and
        the structured data pulled directly by ``IngestionScheduler``::

            {
              "id":       "<gmail message id>",
              "threadId": "<thread id>",
              "headers":  {"subject": ..., "from": ..., "date": ...},
              "body":     "<decoded plain text>"
            }

        Upserts the email row and **rebuilds** chunks and term-frequency rows
        from scratch, so re-ingesting an updated body re-indexes cleanly.

        Args:
            record: Email record dict.

        Returns:
            ``True`` if this was a new email, ``False`` if it was an update.
        """
        import json as _json

        email_id  = record.get("id",       "").strip()
        thread_id = record.get("threadId", "").strip()
        headers   = record.get("headers",  {})
        body      = record.get("body",     "").strip()

        if not email_id:
            log.warning("ingest_email: skipping record with no 'id'")
            return False

        is_new = not self._db.execute(
            "SELECT 1 FROM emails WHERE id = ?", (email_id,)
        ).fetchone()

        self._db.execute(
            """
            INSERT INTO emails
                (id, thread_id, subject, sender, date, raw_headers, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                thread_id   = excluded.thread_id,
                subject     = excluded.subject,
                sender      = excluded.sender,
                date        = excluded.date,
                raw_headers = excluded.raw_headers,
                ingested_at = excluded.ingested_at
            """,
            (
                email_id,
                thread_id,
                headers.get("subject", "(no subject)"),
                headers.get("from",    ""),
                headers.get("date",    ""),
                _json.dumps(headers),
                time.time(),
            ),
        )

        # Delete stale chunks; cascades to term_freq via FK.
        self._db.execute("DELETE FROM chunks WHERE email_id = ?", (email_id,))

        full_text = f"{headers.get('subject', '')}\n\n{body}".strip()
        for idx, chunk_body in enumerate(
            chunk_text(full_text, self._settings.rag_chunk_size, self._settings.rag_chunk_overlap)
        ):
            cur = self._db.execute(
                "INSERT INTO chunks (email_id, chunk_index, body, word_count)"
                " VALUES (?, ?, ?, ?)",
                (email_id, idx, chunk_body, len(chunk_body.split())),
            )
            chunk_id = cur.lastrowid
            freq     = Counter(tokenise(chunk_body))
            self._db.executemany(
                "INSERT OR REPLACE INTO term_freq (chunk_id, term, freq) VALUES (?, ?, ?)",
                [(chunk_id, term, count) for term, count in freq.items()],
            )

        self._db.commit()
        return is_new

    # ── query ─────────────────────────────────────────────────────────────────

    def query(self, query_text: str, top_k: int = 3) -> list[dict[str, Any]]:
        """
        Retrieve the ``top_k`` most relevant chunks using BM25-style scoring.

        Score formula::

            score(chunk) = Σ freq(term) × IDF(term)
            IDF(term)    = log((N - df + 0.5) / (df + 0.5) + 1)

        where ``N`` = total chunks, ``df`` = chunks containing the term.

        Args:
            query_text: Free-text query string.
            top_k:      Maximum number of results to return.

        Returns:
            List of result dicts sorted by descending score::

                [
                  {
                    "text":  "<chunk body>",
                    "score": 3.14,
                    "meta": {
                      "id": "<email id>", "subject": "...",
                      "sender": "...", "date": "...",
                      "thread_id": "...", "chunk_index": 0
                    }
                  }
                ]

            Returns ``[]`` when the store is empty or no terms match.
        """
        terms = tokenise(query_text)
        if not terms:
            return []

        total: int = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if total == 0:
            return []

        idf: dict[str, float] = {}
        for term in set(terms):
            df = self._db.execute(
                "SELECT COUNT(DISTINCT chunk_id) FROM term_freq WHERE term = ?", (term,)
            ).fetchone()[0]
            idf[term] = math.log((total - df + 0.5) / (df + 0.5) + 1)

        scores: dict[int, float] = {}
        for term in terms:
            for row in self._db.execute(
                "SELECT chunk_id, freq FROM term_freq WHERE term = ?", (term,)
            ).fetchall():
                cid = row["chunk_id"]
                scores[cid] = scores.get(cid, 0.0) + row["freq"] * idf.get(term, 0.0)

        if not scores:
            return []

        results: list[dict[str, Any]] = []
        for cid in sorted(scores, key=scores.__getitem__, reverse=True)[:top_k]:
            row = self._db.execute(
                """
                SELECT c.body, c.chunk_index,
                       e.id, e.subject, e.sender, e.date, e.thread_id
                FROM   chunks c
                JOIN   emails e ON e.id = c.email_id
                WHERE  c.id = ?
                """,
                (cid,),
            ).fetchone()
            if row:
                results.append({
                    "text":  row["body"],
                    "score": round(scores[cid], 4),
                    "meta": {
                        "id":          row["id"],
                        "subject":     row["subject"],
                        "sender":      row["sender"],
                        "date":        row["date"],
                        "thread_id":   row["thread_id"],
                        "chunk_index": row["chunk_index"],
                    },
                })
        return results

    # ── refresh / reindex ─────────────────────────────────────────────────────

    def reindex_range(
        self,
        since_ts: float | None,
        until_ts: float | None,
        dry_run: bool = False,
    ) -> int:
        """
        Rebuild the term-frequency index for emails ingested in
        ``[since_ts, until_ts]``.

        This is a local operation: it re-tokenises stored chunk bodies without
        re-fetching from Gmail.  Use it after tokenisation strategy changes or
        schema migrations that do not alter body text.

        Args:
            since_ts: POSIX lower bound (inclusive), or ``None``.
            until_ts: POSIX upper bound (inclusive), or ``None``.
            dry_run:  Count only; make no changes.

        Returns:
            Number of emails re-indexed (or that would be re-indexed).
        """
        clauses: list[str]  = []
        params:  list[float] = []
        if since_ts is not None:
            clauses.append("ingested_at >= ?")
            params.append(since_ts)
        if until_ts is not None:
            clauses.append("ingested_at <= ?")
            params.append(until_ts)

        where     = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        email_rows = self._db.execute(
            f"SELECT id FROM emails {where}", params
        ).fetchall()

        if dry_run:
            return len(email_rows)

        for row in email_rows:
            for chunk_row in self._db.execute(
                "SELECT id, body FROM chunks WHERE email_id = ?", (row["id"],)
            ).fetchall():
                freq = Counter(tokenise(chunk_row["body"]))
                self._db.execute(
                    "DELETE FROM term_freq WHERE chunk_id = ?", (chunk_row["id"],)
                )
                self._db.executemany(
                    "INSERT INTO term_freq (chunk_id, term, freq) VALUES (?, ?, ?)",
                    [(chunk_row["id"], term, count) for term, count in freq.items()],
                )

        self._db.commit()
        return len(email_rows)

    def full_reindex(self, dry_run: bool = False) -> int:
        """
        Rebuild the term-frequency index for the entire corpus.

        Args:
            dry_run: Count only; make no changes.

        Returns:
            Number of emails re-indexed.
        """
        return self.reindex_range(None, None, dry_run=dry_run)

    def stats(self) -> dict[str, int]:
        """
        Return a snapshot of current store size.

        Returns:
            Dict with integer keys ``emails``, ``chunks``, ``term_entries``.
        """
        return {
            "emails":       self._db.execute("SELECT COUNT(*) FROM emails").fetchone()[0],
            "chunks":       self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "term_entries": self._db.execute("SELECT COUNT(*) FROM term_freq").fetchone()[0],
        }
