"""
Gmail MCP tools.

Each function in this module corresponds to one MCP tool exposed by the
server.  Functions accept plain Python kwargs and return plain dicts
(serialised to JSON by the server layer).

Tools implemented:
  - search_gmail         Search Gmail using a query string
  - get_email            Fetch full content of a single email
  - get_email_headers    Fetch headers only (fast)
  - list_labels          List all Gmail labels
  - create_label         Create a new label
  - add_label            Add a label to a message
  - remove_label         Remove a label from a message
  - mark_read            Mark message(s) as read
  - mark_unread          Mark message(s) as unread
  - trash_email          Move a message to trash
  - delete_email         Permanently delete a message
  - ingest_emails        Trigger background ingest of new emails
"""
from __future__ import annotations

import json
from typing import Any

import structlog

from ..gmail_client import GmailClient
from ..rag_client import RAGClient

log = structlog.get_logger(__name__)


def _client() -> GmailClient:
    return GmailClient()


def _rag() -> RAGClient:
    return RAGClient()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def search_gmail(
    query: str,
    max_results: int = 20,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Search Gmail messages using a Gmail query string.

    Args:
        query: Gmail search query (e.g. ``"from:boss@example.com is:unread"``).
        max_results: Maximum number of results to return (1–500).
        page_token: Pagination token from a previous search response.

    Returns:
        Dict with ``messages`` (list of stubs) and optional ``nextPageToken``.
    """
    log.info("tool_search_gmail", query=query, max_results=max_results)
    result = _client().list_messages(
        query=query, max_results=max_results, page_token=page_token
    )
    return {
        "messages": result.get("messages", []),
        "result_size_estimate": result.get("resultSizeEstimate", 0),
        "next_page_token": result.get("nextPageToken"),
    }


def get_email(message_id: str, include_body: bool = True) -> dict[str, Any]:
    """Retrieve the full content of a Gmail message.

    Args:
        message_id: The Gmail message ID.
        include_body: If ``True`` (default), decode and include the plain-text body.

    Returns:
        Dict with ``id``, ``subject``, ``from``, ``to``, ``date``, ``snippet``,
        and optionally ``body``.
    """
    log.info("tool_get_email", message_id=message_id)
    gc = _client()
    msg = gc.get_message(message_id, fmt="full")
    headers = {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    result: dict[str, Any] = {
        "id": msg["id"],
        "thread_id": msg.get("threadId"),
        "label_ids": msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
    }
    if include_body:
        result["body"] = gc.get_message_body(message_id)
    return result


def get_email_headers(message_id: str) -> dict[str, Any]:
    """Retrieve only the headers of a Gmail message (lightweight).

    Args:
        message_id: The Gmail message ID.

    Returns:
        Dict mapping lowercase header names to their values.
    """
    log.info("tool_get_email_headers", message_id=message_id)
    headers = _client().get_headers(message_id)
    return {"message_id": message_id, "headers": headers}


def list_labels() -> dict[str, Any]:
    """List all Gmail labels for the authenticated account.

    Returns:
        Dict with a ``labels`` list, each entry having ``id`` and ``name``.
    """
    log.info("tool_list_labels")
    labels = _client().list_labels()
    return {"labels": [{"id": l["id"], "name": l["name"]} for l in labels]}


def create_label(name: str) -> dict[str, Any]:
    """Create a new Gmail label.

    Args:
        name: Display name for the new label.

    Returns:
        Dict with ``id`` and ``name`` of the created label.
    """
    log.info("tool_create_label", name=name)
    label = _client().create_label(name)
    return {"id": label["id"], "name": label["name"]}


def add_label(message_id: str, label_id: str) -> dict[str, Any]:
    """Add a label to a Gmail message.

    Args:
        message_id: Gmail message ID.
        label_id:   Gmail label ID (use ``list_labels`` to find IDs).

    Returns:
        Updated message metadata.
    """
    log.info("tool_add_label", message_id=message_id, label_id=label_id)
    result = _client().modify_labels(message_id, add_labels=[label_id])
    return {"message_id": result["id"], "label_ids": result.get("labelIds", [])}


def remove_label(message_id: str, label_id: str) -> dict[str, Any]:
    """Remove a label from a Gmail message.

    Args:
        message_id: Gmail message ID.
        label_id:   Gmail label ID to remove.

    Returns:
        Updated message metadata.
    """
    log.info("tool_remove_label", message_id=message_id, label_id=label_id)
    result = _client().modify_labels(message_id, remove_labels=[label_id])
    return {"message_id": result["id"], "label_ids": result.get("labelIds", [])}


def mark_read(message_id: str) -> dict[str, Any]:
    """Mark a Gmail message as read.

    Args:
        message_id: Gmail message ID.
    """
    log.info("tool_mark_read", message_id=message_id)
    result = _client().modify_labels(message_id, remove_labels=["UNREAD"])
    return {"message_id": result["id"], "status": "read"}


def mark_unread(message_id: str) -> dict[str, Any]:
    """Mark a Gmail message as unread.

    Args:
        message_id: Gmail message ID.
    """
    log.info("tool_mark_unread", message_id=message_id)
    result = _client().modify_labels(message_id, add_labels=["UNREAD"])
    return {"message_id": result["id"], "status": "unread"}


def trash_email(message_id: str) -> dict[str, Any]:
    """Move a Gmail message to the trash.

    Args:
        message_id: Gmail message ID.
    """
    log.info("tool_trash_email", message_id=message_id)
    result = _client().trash_message(message_id)
    return {"message_id": result["id"], "status": "trashed"}


def delete_email(message_id: str, confirm: bool = False) -> dict[str, Any]:
    """Permanently delete a Gmail message (non-recoverable).

    Args:
        message_id: Gmail message ID.
        confirm:    Must be ``True`` to proceed. Safety guard.
    """
    if not confirm:
        return {
            "error": "deletion_not_confirmed",
            "message": "Set confirm=True to permanently delete this email.",
        }
    log.warning("tool_delete_email", message_id=message_id)
    _client().delete_message(message_id)
    return {"message_id": message_id, "status": "deleted"}


def ingest_emails(max_emails: int = 50) -> dict[str, Any]:
    """Trigger a background job to ingest new Gmail emails into the RAG cache.

    Args:
        max_emails: Maximum number of emails to ingest in this batch.

    Returns:
        Dict with the Celery ``task_id`` for status tracking.
    """
    log.info("tool_ingest_emails", max_emails=max_emails)
    task_id = _rag().trigger_ingest(max_emails=max_emails)
    return {"task_id": task_id, "status": "dispatched", "max_emails": max_emails}
