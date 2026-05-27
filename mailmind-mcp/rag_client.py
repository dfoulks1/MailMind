"""
RAG & cache client for Mailmind.

Provides two backends that can be used independently or together:

* ``MongoCache``  — read/write the MongoDB email cache.
* ``OllamaClient`` — send prompts to the local Ollama HTTP API.
* ``RAGClient``  — high-level interface (search, ingest trigger, refresh).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings, get_settings

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# MongoDB cache
# ---------------------------------------------------------------------------


class MongoCache:
    """Thin wrapper around a MongoDB collection for email documents."""

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = (settings or get_settings()).cache
        # Lazy import so tests can patch pymongo without installing it.
        import pymongo  # noqa: PLC0415

        self._client = pymongo.MongoClient(cfg.mongo_uri)
        self._col = self._client[cfg.mongo_db][cfg.mongo_collection]

    def upsert_email(self, doc: dict[str, Any]) -> None:
        """Insert or update an email document (keyed on ``message_id``)."""
        self._col.update_one(
            {"message_id": doc["message_id"]},
            {"$set": {**doc, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    def find_by_query(
        self, mongo_filter: dict[str, Any], limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return documents matching *mongo_filter*."""
        return list(self._col.find(mongo_filter, {"_id": 0}).limit(limit))

    def count(self) -> int:
        """Return total number of cached emails."""
        return self._col.count_documents({})

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Ollama HTTP client
# ---------------------------------------------------------------------------


class OllamaClient:
    """Async-friendly HTTP client for Ollama's /api/generate endpoint."""

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = (settings or get_settings()).rag
        self._base_url = cfg.ollama_url.rstrip("/")
        self._model = cfg.ollama_model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def generate(self, prompt: str, model: str | None = None) -> str:
        """Send a prompt to Ollama and return the response text (synchronous)."""
        payload = {
            "model": model or self._model,
            "prompt": prompt,
            "stream": False,
        }
        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(f"{self._base_url}/api/generate", json=payload)
                resp.raise_for_status()
                return resp.json().get("response", "")
        except httpx.HTTPError as exc:
            log.error("ollama_generate_error", error=str(exc))
            raise

    def health(self) -> bool:
        """Return True if Ollama is reachable."""
        try:
            with httpx.Client(timeout=5) as client:
                r = client.get(f"{self._base_url}/api/tags")
                return r.status_code == 200
        except httpx.HTTPError:
            return False


# ---------------------------------------------------------------------------
# High-level RAG client
# ---------------------------------------------------------------------------


class RAGClient:
    """
    High-level interface for the Mailmind RAG system.

    * Uses ``MongoCache`` for structured email lookups.
    * Uses ``OllamaClient`` for summarization / generation.
    * Celery task dispatch is used for async ingest / refresh jobs.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._cache = MongoCache(self._settings)
        self._ollama = OllamaClient(self._settings)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def keyword_search(
        self, query: str, top_k: int | None = None
    ) -> list[dict[str, Any]]:
        """Full-text keyword search over the cached email corpus.

        Falls back to a simple ``$text`` index search on MongoDB.
        """
        k = top_k or self._settings.search.default_top_k
        mongo_filter: dict[str, Any] = {"$text": {"$search": query}}
        results = self._cache.find_by_query(mongo_filter, limit=k)
        log.info("rag_keyword_search", query=query, results=len(results))
        return results

    def get_cached_email(self, message_id: str) -> dict[str, Any] | None:
        """Retrieve a single cached email by Gmail message ID."""
        results = self._cache.find_by_query({"message_id": message_id}, limit=1)
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Summarization
    # ------------------------------------------------------------------

    def summarize_email(self, body: str, subject: str = "") -> str:
        """Ask the local LLM to summarize an email body."""
        prompt = (
            f"Summarize the following email concisely.\n\n"
            f"Subject: {subject}\n\n"
            f"Body:\n{body[:4000]}\n\n"
            "Summary:"
        )
        return self._ollama.generate(prompt)

    def answer_question(self, question: str, context_emails: list[dict[str, Any]]) -> str:
        """Answer *question* using *context_emails* as RAG context."""
        context = "\n\n---\n\n".join(
            f"Subject: {e.get('subject', '')}\n{e.get('body', '')[:1000]}"
            for e in context_emails[:5]
        )
        prompt = (
            f"Using the following emails as context, answer the question.\n\n"
            f"Context:\n{context}\n\n"
            f"Question: {question}\n\n"
            "Answer:"
        )
        return self._ollama.generate(prompt)

    # ------------------------------------------------------------------
    # Ingest / refresh triggers via Celery
    # ------------------------------------------------------------------

    def trigger_ingest(self, max_emails: int | None = None) -> str:
        """Dispatch a Celery task to ingest new Gmail messages into the cache.

        Returns the Celery task ID.
        """
        try:
            from celery import Celery  # noqa: PLC0415

            app = Celery(broker=self._settings.cache.redis_url)
            result = app.send_task(
                "mailmind.tasks.ingest_emails",
                kwargs={"max_emails": max_emails or self._settings.rag.max_ingest_batch},
            )
            log.info("ingest_task_dispatched", task_id=result.id)
            return result.id
        except Exception as exc:
            log.error("ingest_trigger_failed", error=str(exc))
            raise

    def trigger_refresh(self) -> str:
        """Dispatch a Celery task to rebuild the RAG index.

        Returns the Celery task ID.
        """
        try:
            from celery import Celery  # noqa: PLC0415

            app = Celery(broker=self._settings.cache.redis_url)
            result = app.send_task("mailmind.tasks.refresh_rag")
            log.info("refresh_task_dispatched", task_id=result.id)
            return result.id
        except Exception as exc:
            log.error("refresh_trigger_failed", error=str(exc))
            raise

    def cache_stats(self) -> dict[str, Any]:
        """Return basic stats about the email cache."""
        count = self._cache.count()
        ollama_ok = self._ollama.health()
        return {
            "cached_email_count": count,
            "ollama_available": ollama_ok,
            "ollama_model": self._settings.rag.ollama_model,
        }

    def close(self) -> None:
        self._cache.close()
