"""
RAG search & summarization MCP tools.

Tools implemented:
  - search_emails       Keyword search over the email cache
  - summarize_email     Summarize a specific cached email via Ollama
  - ask_emails          Answer a question using cached emails as context
  - refresh_rag         Trigger a RAG index rebuild
  - cache_stats         Return cache & system health stats
"""
from __future__ import annotations

from typing import Any

import structlog

from ..rag_client import RAGClient

log = structlog.get_logger(__name__)


def _rag() -> RAGClient:
    return RAGClient()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def search_emails(
    query: str,
    top_k: int = 10,
) -> dict[str, Any]:
    """Search the local email cache using a keyword query.

    This searches *cached* emails (MongoDB), not Gmail directly.
    Use ``search_gmail`` if you need real-time Gmail search.

    Args:
        query: Search terms.
        top_k: Maximum number of results to return.

    Returns:
        Dict with a ``results`` list of matching email documents.
    """
    log.info("tool_search_emails", query=query, top_k=top_k)
    results = _rag().keyword_search(query=query, top_k=top_k)
    return {
        "query": query,
        "result_count": len(results),
        "results": results,
    }


def summarize_email(
    message_id: str | None = None,
    body: str | None = None,
    subject: str = "",
) -> dict[str, Any]:
    """Summarize an email using the local Ollama LLM.

    Provide either ``message_id`` (to look up in cache) or a raw ``body``
    string.

    Args:
        message_id: Gmail message ID to look up in the local cache.
        body:       Raw email body text (used when message_id is not supplied).
        subject:    Optional subject line for better summarization context.

    Returns:
        Dict with ``summary`` and the source ``message_id`` (if provided).
    """
    rag = _rag()
    if message_id:
        doc = rag.get_cached_email(message_id)
        if not doc:
            return {
                "error": "not_found",
                "message": f"Email {message_id} not found in cache. Run ingest_emails first.",
            }
        email_body = doc.get("body", "")
        subject = subject or doc.get("subject", "")
        log.info("tool_summarize_email", message_id=message_id)
    elif body:
        email_body = body
        log.info("tool_summarize_email_raw_body")
    else:
        return {"error": "missing_input", "message": "Provide message_id or body."}

    summary = rag.summarize_email(body=email_body, subject=subject)
    result: dict[str, Any] = {"summary": summary}
    if message_id:
        result["message_id"] = message_id
    return result


def ask_emails(
    question: str,
    search_query: str | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """Answer a question about your emails using RAG.

    Retrieves relevant cached emails and uses the local LLM to synthesise
    an answer.

    Args:
        question:     The question to answer.
        search_query: Optional override for the cache search query.
                      Defaults to the value of ``question``.
        top_k:        Number of emails to include as context.

    Returns:
        Dict with ``answer`` and the ``context_emails`` used.
    """
    log.info("tool_ask_emails", question=question, top_k=top_k)
    rag = _rag()
    context = rag.keyword_search(query=search_query or question, top_k=top_k)
    if not context:
        return {
            "answer": "No relevant emails found in cache. Try running ingest_emails first.",
            "context_emails": [],
        }
    answer = rag.answer_question(question=question, context_emails=context)
    return {
        "answer": answer,
        "context_emails": [
            {
                "message_id": e.get("message_id"),
                "subject": e.get("subject"),
                "from": e.get("from"),
                "date": e.get("date"),
            }
            for e in context
        ],
    }


def refresh_rag() -> dict[str, Any]:
    """Rebuild the RAG index from the current email cache.

    This is a background Celery task — it returns immediately with a task ID
    that you can use to check progress.

    Returns:
        Dict with ``task_id`` and ``status``.
    """
    log.info("tool_refresh_rag")
    task_id = _rag().trigger_refresh()
    return {"task_id": task_id, "status": "dispatched"}


def cache_stats() -> dict[str, Any]:
    """Return statistics about the email cache and system health.

    Returns:
        Dict with ``cached_email_count``, ``ollama_available``, and
        ``ollama_model``.
    """
    log.info("tool_cache_stats")
    return _rag().cache_stats()
