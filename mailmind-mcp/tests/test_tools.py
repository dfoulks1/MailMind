"""
Unit tests for Gmail and RAG tool functions.

All external dependencies (GmailClient, RAGClient) are mocked so these
tests run without real credentials or a live database.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Gmail tool tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_gmail_client() -> MagicMock:
    client = MagicMock()
    return client


@patch("mailmind_mcp.tools.gmail._client")
def test_search_gmail_returns_messages(mock_client_fn: MagicMock) -> None:
    mock_client_fn.return_value.list_messages.return_value = {
        "messages": [{"id": "abc123", "threadId": "t1"}],
        "resultSizeEstimate": 1,
    }
    from mailmind_mcp.tools.gmail import search_gmail

    result = search_gmail(query="from:test@example.com")
    assert result["messages"] == [{"id": "abc123", "threadId": "t1"}]
    assert result["result_size_estimate"] == 1
    assert result["next_page_token"] is None


@patch("mailmind_mcp.tools.gmail._client")
def test_search_gmail_empty_result(mock_client_fn: MagicMock) -> None:
    mock_client_fn.return_value.list_messages.return_value = {}
    from mailmind_mcp.tools.gmail import search_gmail

    result = search_gmail(query="nothing")
    assert result["messages"] == []


@patch("mailmind_mcp.tools.gmail._client")
def test_get_email_returns_fields(mock_client_fn: MagicMock) -> None:
    mock_client_fn.return_value.get_message.return_value = {
        "id": "msg1",
        "threadId": "thread1",
        "labelIds": ["INBOX"],
        "snippet": "Hello there",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Test subject"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            ]
        },
    }
    mock_client_fn.return_value.get_message_body.return_value = "Email body text."
    from mailmind_mcp.tools.gmail import get_email

    result = get_email("msg1")
    assert result["id"] == "msg1"
    assert result["subject"] == "Test subject"
    assert result["from"] == "sender@example.com"
    assert result["body"] == "Email body text."


@patch("mailmind_mcp.tools.gmail._client")
def test_get_email_no_body(mock_client_fn: MagicMock) -> None:
    mock_client_fn.return_value.get_message.return_value = {
        "id": "msg2",
        "threadId": "t2",
        "labelIds": [],
        "snippet": "",
        "payload": {"headers": []},
    }
    from mailmind_mcp.tools.gmail import get_email

    result = get_email("msg2", include_body=False)
    assert "body" not in result


@patch("mailmind_mcp.tools.gmail._client")
def test_list_labels(mock_client_fn: MagicMock) -> None:
    mock_client_fn.return_value.list_labels.return_value = [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "Label_1", "name": "Work", "type": "user"},
    ]
    from mailmind_mcp.tools.gmail import list_labels

    result = list_labels()
    assert len(result["labels"]) == 2
    assert result["labels"][0] == {"id": "INBOX", "name": "INBOX"}


@patch("mailmind_mcp.tools.gmail._client")
def test_create_label(mock_client_fn: MagicMock) -> None:
    mock_client_fn.return_value.create_label.return_value = {
        "id": "Label_99",
        "name": "MyNewLabel",
    }
    from mailmind_mcp.tools.gmail import create_label

    result = create_label("MyNewLabel")
    assert result == {"id": "Label_99", "name": "MyNewLabel"}


@patch("mailmind_mcp.tools.gmail._client")
def test_mark_read(mock_client_fn: MagicMock) -> None:
    mock_client_fn.return_value.modify_labels.return_value = {
        "id": "msg3",
        "labelIds": ["INBOX"],
    }
    from mailmind_mcp.tools.gmail import mark_read

    result = mark_read("msg3")
    assert result["status"] == "read"
    mock_client_fn.return_value.modify_labels.assert_called_once_with(
        "msg3", remove_labels=["UNREAD"]
    )


@patch("mailmind_mcp.tools.gmail._client")
def test_trash_email(mock_client_fn: MagicMock) -> None:
    mock_client_fn.return_value.trash_message.return_value = {"id": "msg4"}
    from mailmind_mcp.tools.gmail import trash_email

    result = trash_email("msg4")
    assert result["status"] == "trashed"


def test_delete_email_requires_confirm() -> None:
    from mailmind_mcp.tools.gmail import delete_email

    result = delete_email("msg5", confirm=False)
    assert result["error"] == "deletion_not_confirmed"


@patch("mailmind_mcp.tools.gmail._client")
def test_delete_email_confirmed(mock_client_fn: MagicMock) -> None:
    mock_client_fn.return_value.delete_message.return_value = None
    from mailmind_mcp.tools.gmail import delete_email

    result = delete_email("msg5", confirm=True)
    assert result["status"] == "deleted"


@patch("mailmind_mcp.tools.gmail._rag")
def test_ingest_emails(mock_rag_fn: MagicMock) -> None:
    mock_rag_fn.return_value.trigger_ingest.return_value = "task-abc"
    from mailmind_mcp.tools.gmail import ingest_emails

    result = ingest_emails(max_emails=10)
    assert result["task_id"] == "task-abc"
    assert result["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Search / RAG tool tests
# ---------------------------------------------------------------------------


@patch("mailmind_mcp.tools.search._rag")
def test_search_emails(mock_rag_fn: MagicMock) -> None:
    mock_rag_fn.return_value.keyword_search.return_value = [
        {"message_id": "id1", "subject": "Hello", "body": "World"}
    ]
    from mailmind_mcp.tools.search import search_emails

    result = search_emails(query="Hello", top_k=5)
    assert result["result_count"] == 1
    assert result["results"][0]["message_id"] == "id1"


@patch("mailmind_mcp.tools.search._rag")
def test_summarize_email_by_id(mock_rag_fn: MagicMock) -> None:
    mock_rag_fn.return_value.get_cached_email.return_value = {
        "message_id": "id1",
        "subject": "Hello",
        "body": "This is a test email.",
    }
    mock_rag_fn.return_value.summarize_email.return_value = "A short summary."
    from mailmind_mcp.tools.search import summarize_email

    result = summarize_email(message_id="id1")
    assert result["summary"] == "A short summary."
    assert result["message_id"] == "id1"


@patch("mailmind_mcp.tools.search._rag")
def test_summarize_email_not_found(mock_rag_fn: MagicMock) -> None:
    mock_rag_fn.return_value.get_cached_email.return_value = None
    from mailmind_mcp.tools.search import summarize_email

    result = summarize_email(message_id="missing")
    assert result["error"] == "not_found"


@patch("mailmind_mcp.tools.search._rag")
def test_summarize_email_raw_body(mock_rag_fn: MagicMock) -> None:
    mock_rag_fn.return_value.summarize_email.return_value = "Summary here."
    from mailmind_mcp.tools.search import summarize_email

    result = summarize_email(body="Some long email text.")
    assert result["summary"] == "Summary here."
    assert "message_id" not in result


def test_summarize_email_no_input() -> None:
    from mailmind_mcp.tools.search import summarize_email

    result = summarize_email()
    assert result["error"] == "missing_input"


@patch("mailmind_mcp.tools.search._rag")
def test_ask_emails(mock_rag_fn: MagicMock) -> None:
    mock_rag_fn.return_value.keyword_search.return_value = [
        {"message_id": "id1", "subject": "Q4 report", "body": "Revenue was great."}
    ]
    mock_rag_fn.return_value.answer_question.return_value = "Revenue was great in Q4."
    from mailmind_mcp.tools.search import ask_emails

    result = ask_emails(question="How was Q4 revenue?")
    assert result["answer"] == "Revenue was great in Q4."
    assert len(result["context_emails"]) == 1


@patch("mailmind_mcp.tools.search._rag")
def test_ask_emails_no_cache(mock_rag_fn: MagicMock) -> None:
    mock_rag_fn.return_value.keyword_search.return_value = []
    from mailmind_mcp.tools.search import ask_emails

    result = ask_emails(question="Anything?")
    assert "No relevant emails found" in result["answer"]


@patch("mailmind_mcp.tools.search._rag")
def test_refresh_rag(mock_rag_fn: MagicMock) -> None:
    mock_rag_fn.return_value.trigger_refresh.return_value = "task-xyz"
    from mailmind_mcp.tools.search import refresh_rag

    result = refresh_rag()
    assert result["task_id"] == "task-xyz"
    assert result["status"] == "dispatched"


@patch("mailmind_mcp.tools.search._rag")
def test_cache_stats(mock_rag_fn: MagicMock) -> None:
    mock_rag_fn.return_value.cache_stats.return_value = {
        "cached_email_count": 42,
        "ollama_available": True,
        "ollama_model": "llama3.2:1b",
    }
    from mailmind_mcp.tools.search import cache_stats

    result = cache_stats()
    assert result["cached_email_count"] == 42
    assert result["ollama_available"] is True
