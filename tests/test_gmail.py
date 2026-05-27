"""Tests for mailmind.gmail — GmailClient and ThreadParser."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from mailmind.config import Settings
from mailmind.gmail import GmailClient, ThreadParser
from mailmind.models import GmailError, MessageSummary, ThreadSummary
from mailmind.oauth import OAuthTokenManager
from tests.conftest import RAW_THREAD, b64encode


# ── Helpers ────────────────────────────────────────────────────────────────────


def _client(settings: Settings) -> GmailClient:
    tokens = AsyncMock(spec=OAuthTokenManager)
    tokens.get_access_token.return_value = "test_token"
    return GmailClient(settings, tokens)


BASE = "https://gmail.googleapis.com/gmail/v1"


# ─────────────────────────────────────────────────────────────────────────────
# ThreadParser
# ─────────────────────────────────────────────────────────────────────────────


class TestThreadParser:
    def test_thread_id(self) -> None:
        assert ThreadParser.parse_thread(RAW_THREAD).thread_id == "thread_001"

    def test_message_count(self) -> None:
        assert len(ThreadParser.parse_thread(RAW_THREAD).messages) == 1

    def test_subject(self, sample_message: MessageSummary) -> None:
        assert sample_message.subject == "Test Subject"

    def test_sender(self, sample_message: MessageSummary) -> None:
        assert sample_message.sender == "sender@example.com"

    def test_recipients(self, sample_message: MessageSummary) -> None:
        assert "recipient@example.com" in sample_message.recipients
        assert "cc@example.com" in sample_message.recipients

    def test_labels(self, sample_message: MessageSummary) -> None:
        assert "INBOX" in sample_message.labels

    def test_plain_text_body(self, sample_message: MessageSummary) -> None:
        assert "Hello, this is the plain text body." in sample_message.body

    def test_attachment_filename(self, sample_message: MessageSummary) -> None:
        assert "report.pdf" in sample_message.attachment_filenames

    def test_mime_type(self, sample_message: MessageSummary) -> None:
        assert sample_message.mime_type == "multipart/mixed"

    def test_headers_lowercased(self, sample_message: MessageSummary) -> None:
        assert "subject" in sample_message.raw_headers
        assert "from"    in sample_message.raw_headers

    def test_body_preview_truncated(self, settings: Settings) -> None:
        long_body = "x" * (settings.gmail_max_body_chars + 500)
        raw: dict[str, Any] = {
            "id": "t", "messages": [{
                "id": "m", "threadId": "t", "labelIds": [], "snippet": "",
                "payload": {
                    "mimeType": "text/plain", "headers": [],
                    "body": {"data": b64encode(long_body), "size": len(long_body)},
                    "parts": [],
                },
            }],
        }
        msg = ThreadParser.parse_thread(raw).messages[0]
        assert len(msg.body_preview(settings.gmail_max_body_chars)) <= settings.gmail_max_body_chars

    def test_missing_subject_default(self) -> None:
        raw: dict[str, Any] = {
            "id": "t", "messages": [{
                "id": "m", "threadId": "t", "labelIds": [], "snippet": "",
                "payload": {"mimeType": "text/plain", "headers": [], "body": {}, "parts": []},
            }],
        }
        assert ThreadParser.parse_thread(raw).messages[0].subject == "(no subject)"

    def test_thread_subject_from_first_message(self, sample_thread: ThreadSummary) -> None:
        assert sample_thread.subject == "Test Subject"

    def test_participants_unique(self, sample_thread: ThreadSummary) -> None:
        p = sample_thread.participants
        assert len(p) == len(set(p))

    def test_empty_thread(self) -> None:
        t = ThreadParser.parse_thread({"id": "t", "messages": []})
        assert t.subject  == "(empty thread)"
        assert t.messages == []


# ─────────────────────────────────────────────────────────────────────────────
# GmailClient
# ─────────────────────────────────────────────────────────────────────────────


class TestGmailClient:
    @respx.mock
    async def test_search_threads_success(self, settings: Settings) -> None:
        respx.get(f"{BASE}/users/me/threads").mock(
            return_value=httpx.Response(200, json={"threads": [{"id": "t1"}]})
        )
        assert await _client(settings).search_threads("test") == [{"id": "t1"}]

    @respx.mock
    async def test_get_thread_success(self, settings: Settings) -> None:
        respx.get(f"{BASE}/users/me/threads/thread_001").mock(
            return_value=httpx.Response(200, json=RAW_THREAD)
        )
        result = await _client(settings).get_thread("thread_001")
        assert result["id"] == "thread_001"

    @respx.mock
    async def test_list_labels(self, settings: Settings) -> None:
        respx.get(f"{BASE}/users/me/labels").mock(
            return_value=httpx.Response(200, json={"labels": [{"id": "L1", "name": "Work"}]})
        )
        assert (await _client(settings).list_labels())[0]["name"] == "Work"

    @respx.mock
    async def test_http_400_raises(self, settings: Settings) -> None:
        respx.get(f"{BASE}/users/me/threads").mock(
            return_value=httpx.Response(400, json={"error": "bad"})
        )
        with pytest.raises(GmailError, match="400"):
            await _client(settings).search_threads("test")

    @respx.mock
    async def test_http_404_raises(self, settings: Settings) -> None:
        respx.get(f"{BASE}/users/me/threads/bad").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        with pytest.raises(GmailError, match="404"):
            await _client(settings).get_thread("bad")

    @respx.mock
    async def test_connection_error_raises(self, settings: Settings) -> None:
        respx.get(f"{BASE}/users/me/threads").mock(
            side_effect=httpx.ConnectError("refused")
        )
        with pytest.raises(GmailError, match="connection error"):
            await _client(settings).search_threads("test")

    @respx.mock
    async def test_timeout_raises(self, settings: Settings) -> None:
        respx.get(f"{BASE}/users/me/threads").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        with pytest.raises(GmailError, match="timed out"):
            await _client(settings).search_threads("test")

    @respx.mock
    async def test_bearer_token_in_header(self, settings: Settings) -> None:
        route = respx.get(f"{BASE}/users/me/threads").mock(
            return_value=httpx.Response(200, json={"threads": []})
        )
        await _client(settings).search_threads("test")
        assert route.calls[0].request.headers["authorization"] == "Bearer test_token"

    @respx.mock
    async def test_query_param_sent(self, settings: Settings) -> None:
        import httpx as _httpx
        route = respx.get(f"{BASE}/users/me/threads").mock(
            return_value=httpx.Response(200, json={"threads": []})
        )
        await _client(settings).search_threads("is:unread", max_results=5)
        qs = dict(_httpx.URL(str(route.calls[0].request.url)).params)
        assert qs["q"] == "is:unread"
        assert qs["maxResults"] == "5"
