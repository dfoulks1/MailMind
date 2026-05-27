"""
Shared pytest fixtures for the MailMind test suite.

All fixtures are available without explicit import.
The ``no_dotenv`` session fixture prevents any on-disk ``.env`` from
influencing test behaviour.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from mailmind.config import Settings
from mailmind.gmail import GmailClient, ThreadParser
from mailmind.models import MessageSummary, ThreadSummary
from mailmind.oauth import OAuthTokenManager
from mailmind.ollama import OllamaClient
from mailmind.analysis import GmailAnalyzer


# ── Environment isolation ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="session")
def no_dotenv() -> Any:
    """Prevent any .env file on disk from influencing test behaviour."""
    with patch("mailmind.service.load_dotenv"):
        yield


# ── Default settings ───────────────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    """A ``Settings`` instance with safe test defaults (no real credentials)."""
    return Settings(
        oauth_client_id     = "test-client-id",
        oauth_client_secret = "test-client-secret",
        oauth_token_file    = "/tmp/mailmind_test_token.json",
        rag_db_path         = ":memory:",
        scheduler_enabled   = False,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────


def b64encode(text: str) -> str:
    """URL-safe base64-encode a string, matching the Gmail API encoding."""
    return base64.urlsafe_b64encode(text.encode()).decode()


# ── Shared raw thread ──────────────────────────────────────────────────────────

#: Minimal complete Gmail REST API thread resource used across all test modules.
RAW_THREAD: dict[str, Any] = {
    "id": "thread_001",
    "messages": [
        {
            "id":       "msg_001",
            "threadId": "thread_001",
            "labelIds": ["INBOX", "UNREAD"],
            "snippet":  "Hello this is a test",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "Subject",                "value": "Test Subject"},
                    {"name": "From",                   "value": "sender@example.com"},
                    {"name": "To",                     "value": "recipient@example.com, cc@example.com"},
                    {"name": "Date",                   "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
                    {"name": "Message-ID",             "value": "<test@example.com>"},
                    {"name": "Authentication-Results",
                     "value": "mx.example.com; dkim=pass; spf=pass; dmarc=pass"},
                    {"name": "DKIM-Signature",         "value": "v=1; a=rsa-sha256; ..."},
                    {"name": "Received",
                     "value": "from mail.example.com by mx.example.com"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "headers": [],
                        "body": {
                            "data": b64encode("Hello, this is the plain text body."),
                            "size": 36,
                        },
                        "parts": [],
                    },
                    {
                        "mimeType": "application/pdf",
                        "filename": "report.pdf",
                        "headers": [
                            {"name":  "Content-Disposition",
                             "value": "attachment; filename=report.pdf"},
                        ],
                        "body":  {"size": 12345},
                        "parts": [],
                    },
                ],
            },
        }
    ],
}


# ── pytest fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def raw_thread() -> dict[str, Any]:
    return RAW_THREAD


@pytest.fixture
def sample_thread(settings: Settings) -> ThreadSummary:
    return ThreadParser.parse_thread(RAW_THREAD)


@pytest.fixture
def sample_message(sample_thread: ThreadSummary) -> MessageSummary:
    return sample_thread.messages[0]


@pytest.fixture
def mock_ollama() -> AsyncMock:
    m = AsyncMock(spec=OllamaClient)
    m.generate.return_value = "LLM response."
    return m


@pytest.fixture
def mock_tokens(settings: Settings) -> AsyncMock:
    m = AsyncMock(spec=OAuthTokenManager)
    m.get_access_token.return_value = "fake_access_token"
    return m


@pytest.fixture
def mock_gmail(mock_tokens: AsyncMock) -> AsyncMock:
    m = AsyncMock(spec=GmailClient)
    m.get_thread.return_value      = RAW_THREAD
    m.search_threads.return_value  = [{"id": "thread_001"}]
    return m


@pytest.fixture
def analyzer(settings: Settings, mock_gmail: AsyncMock, mock_ollama: AsyncMock) -> GmailAnalyzer:
    return GmailAnalyzer(settings=settings, gmail=mock_gmail, ollama=mock_ollama)
