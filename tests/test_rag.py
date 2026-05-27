"""Tests for mailmind.rag — tokeniser, chunker, RagStore, iso_to_timestamp."""

from __future__ import annotations

import time
from typing import Any

import pytest

from mailmind.config import Settings
from mailmind.rag import RagStore, chunk_text, iso_to_timestamp, tokenise


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def store() -> RagStore:
    s = RagStore(Settings(rag_db_path=":memory:"))
    s.open()
    yield s
    s.close()


def _rec(
    email_id:  str = "msg_001",
    subject:   str = "Test Subject",
    body:      str = "This is a test email body with some unique words.",
    sender:    str = "sender@example.com",
    thread_id: str = "thread_001",
) -> dict[str, Any]:
    return {
        "id":       email_id,
        "threadId": thread_id,
        "headers":  {"subject": subject, "from": sender, "date": "Mon, 01 Jan 2024 12:00:00 +0000"},
        "body":     body,
    }


# ── tokenise ───────────────────────────────────────────────────────────────────


class TestTokenise:
    def test_lowercases_input(self) -> None:
        assert "invoice" in tokenise("Invoice Payment")

    def test_strips_punctuation(self) -> None:
        tokens = tokenise("invoice, payment!")
        assert "invoice" in tokens and "payment" in tokens

    def test_removes_stop_words(self) -> None:
        assert "the" not in tokenise("the quick brown fox")

    def test_removes_single_char_tokens(self) -> None:
        assert "a" not in tokenise("a b c invoice")

    def test_empty_string(self) -> None:
        assert tokenise("") == []

    def test_preserves_duplicates(self) -> None:
        assert tokenise("invoice invoice payment").count("invoice") == 2


# ── chunk_text ─────────────────────────────────────────────────────────────────


class TestChunkText:
    def test_empty_string(self) -> None:
        assert chunk_text("") == []

    def test_short_text_single_chunk(self) -> None:
        assert chunk_text("hello world foo bar", chunk_size=10, overlap=2) == [
            "hello world foo bar"
        ]

    def test_long_text_multiple_chunks(self) -> None:
        text = " ".join(f"w{i}" for i in range(100))
        assert len(chunk_text(text, chunk_size=20, overlap=5)) > 1

    def test_overlap_shares_words(self) -> None:
        text   = " ".join(f"w{i}" for i in range(30))
        chunks = chunk_text(text, chunk_size=10, overlap=3)
        assert set(chunks[0].split()[-3:]) & set(chunks[1].split()[:3])

    def test_no_empty_chunks(self) -> None:
        assert all(c.strip() for c in chunk_text("word " * 50, chunk_size=10, overlap=3))


# ── iso_to_timestamp ───────────────────────────────────────────────────────────


class TestIsoToTimestamp:
    def test_none_returns_none(self) -> None:
        assert iso_to_timestamp(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert iso_to_timestamp("") is None

    def test_z_suffix(self) -> None:
        assert isinstance(iso_to_timestamp("2024-06-01T00:00:00Z"), float)

    def test_offset_notation(self) -> None:
        assert isinstance(iso_to_timestamp("2024-06-01T00:00:00+00:00"), float)

    def test_unparseable_returns_none(self) -> None:
        assert iso_to_timestamp("not-a-date") is None


# ── RagStore.ingest_email ──────────────────────────────────────────────────────


class TestIngestEmail:
    def test_new_email_returns_true(self, store: RagStore) -> None:
        assert store.ingest_email(_rec()) is True

    def test_duplicate_id_returns_false(self, store: RagStore) -> None:
        r = _rec()
        store.ingest_email(r)
        assert store.ingest_email(r) is False

    def test_long_body_multiple_chunks(self, store: RagStore) -> None:
        store.ingest_email(_rec(body=" ".join(["word"] * 500)))
        assert store.stats()["chunks"] > 1

    def test_missing_id_skipped(self, store: RagStore) -> None:
        assert store.ingest_email({"threadId": "t", "headers": {}, "body": "x"}) is False
        assert store.stats()["emails"] == 0

    def test_upsert_rebuilds_chunks(self, store: RagStore) -> None:
        r = _rec(body="short")
        store.ingest_email(r)
        before = store.stats()["chunks"]
        r["body"] = " ".join(["word"] * 500)
        store.ingest_email(r)
        assert store.stats()["chunks"] > before

    def test_accumulates_multiple_emails(self, store: RagStore) -> None:
        for i in range(5):
            store.ingest_email(_rec(email_id=f"m{i}"))
        assert store.stats()["emails"] == 5

    def test_term_freq_populated(self, store: RagStore) -> None:
        store.ingest_email(_rec(body="invoice payment due"))
        assert store.stats()["term_entries"] > 0


# ── RagStore.query ─────────────────────────────────────────────────────────────


class TestQuery:
    def test_empty_store(self, store: RagStore) -> None:
        assert store.query("anything") == []

    def test_matching_term(self, store: RagStore) -> None:
        store.ingest_email(_rec(body="quarterly invoice payment pending"))
        assert len(store.query("invoice")) >= 1

    def test_result_shape(self, store: RagStore) -> None:
        store.ingest_email(_rec(body="budget approval"))
        r = store.query("budget")[0]
        assert {"text", "score", "meta"} <= r.keys()
        assert {"id", "subject", "sender", "date", "thread_id", "chunk_index"} <= r["meta"].keys()

    def test_top_k_limits(self, store: RagStore) -> None:
        for i in range(10):
            store.ingest_email(_rec(email_id=f"m{i}", body=f"invoice payment {i}"))
        assert len(store.query("invoice", top_k=3)) <= 3

    def test_more_relevant_scores_higher(self, store: RagStore) -> None:
        store.ingest_email(_rec(email_id="a", body="invoice invoice invoice payment"))
        store.ingest_email(_rec(email_id="b", body="birthday party fun weekend"))
        assert store.query("invoice", top_k=2)[0]["meta"]["id"] == "a"

    def test_empty_query(self, store: RagStore) -> None:
        store.ingest_email(_rec())
        assert store.query("") == []

    def test_stop_words_only_query(self, store: RagStore) -> None:
        store.ingest_email(_rec(body="quick brown fox"))
        assert store.query("the and or") == []


# ── RagStore reindex ───────────────────────────────────────────────────────────


class TestReindex:
    def test_full_reindex_count(self, store: RagStore) -> None:
        for i in range(3):
            store.ingest_email(_rec(email_id=f"m{i}"))
        assert store.full_reindex() == 3

    def test_dry_run_no_change(self, store: RagStore) -> None:
        store.ingest_email(_rec())
        before = store.stats()
        store.full_reindex(dry_run=True)
        assert store.stats() == before

    def test_range_scopes_correctly(self, store: RagStore) -> None:
        store.ingest_email(_rec(email_id="old"))
        store._db.execute(
            "UPDATE emails SET ingested_at = ? WHERE id = 'old'",
            (time.time() - 86_400 * 30,),
        )
        store._db.commit()
        store.ingest_email(_rec(email_id="new"))
        now = time.time()
        assert store.reindex_range(since_ts=now - 3600, until_ts=now + 3600) == 1
