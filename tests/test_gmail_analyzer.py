"""
Complete pytest suite for gmail_analyzer (Google Gmail MCP / gmailmcp.googleapis.com).

All HTTP calls are intercepted by respx — no network access required.
Run with:  uv run pytest -v
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from gmail_analyzer import (
    AnalysisMode,
    AnalysisResult,
    Config,
    GmailAnalyzer,
    GmailMCPClient,
    GmailMCPError,
    HeaderAnalyzer,
    MessageSummary,
    MIMEAnalyzer,
    OAuthError,
    OAuthTokenManager,
    OllamaClient,
    OllamaError,
    ThreadParser,
    ThreadSummary,
    TroubleshootAnalyzer,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _mcp_ok(payload: Any) -> httpx.Response:
    """Wrap a Python value in an MCP tools/call success envelope."""
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            },
        },
    )


def _mcp_error(code: int, message: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": code, "message": message},
        },
    )


# ─── Fixtures ─────────────────────────────────────────────────────────────────

RAW_THREAD: dict[str, Any] = {
    "id": "thread_001",
    "messages": [
        {
            "id": "msg_001",
            "threadId": "thread_001",
            "labelIds": ["INBOX", "UNREAD"],
            "snippet": "Hello this is a test",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "Subject", "value": "Test Subject"},
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "recipient@example.com, cc@example.com"},
                    {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
                    {"name": "Message-ID", "value": "<test@example.com>"},
                    {
                        "name": "Authentication-Results",
                        "value": "mx.example.com; dkim=pass; spf=pass; dmarc=pass",
                    },
                    {"name": "DKIM-Signature", "value": "v=1; a=rsa-sha256; ..."},
                    {"name": "Received", "value": "from mail.example.com by mx.example.com"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "headers": [],
                        "body": {"data": _b64("Hello, this is the plain text body."), "size": 36},
                        "parts": [],
                    },
                    {
                        "mimeType": "application/pdf",
                        "filename": "report.pdf",
                        "headers": [
                            {"name": "Content-Disposition", "value": "attachment; filename=report.pdf"}
                        ],
                        "body": {"size": 12345},
                        "parts": [],
                    },
                ],
            },
        }
    ],
}


@pytest.fixture
def sample_message() -> MessageSummary:
    thread = ThreadParser.parse_thread(RAW_THREAD)
    return thread.messages[0]


@pytest.fixture
def sample_thread() -> ThreadSummary:
    return ThreadParser.parse_thread(RAW_THREAD)


@pytest.fixture
def mock_ollama() -> AsyncMock:
    m = AsyncMock(spec=OllamaClient)
    m.generate.return_value = "LLM response."
    return m


@pytest.fixture
def mock_tokens() -> AsyncMock:
    m = AsyncMock(spec=OAuthTokenManager)
    m.get_access_token.return_value = "fake_access_token"
    return m


@pytest.fixture
def mock_mcp(mock_tokens: AsyncMock) -> AsyncMock:
    m = AsyncMock(spec=GmailMCPClient)
    m.get_thread.return_value = RAW_THREAD
    m.search_threads.return_value = [{"id": "thread_001"}]
    return m


@pytest.fixture
def analyzer(mock_mcp: AsyncMock, mock_ollama: AsyncMock) -> GmailAnalyzer:
    return GmailAnalyzer(mcp=mock_mcp, ollama=mock_ollama)


# ─── ThreadParser ─────────────────────────────────────────────────────────────

class TestThreadParser:
    def test_parse_thread_id(self) -> None:
        t = ThreadParser.parse_thread(RAW_THREAD)
        assert t.thread_id == "thread_001"

    def test_parse_message_count(self) -> None:
        t = ThreadParser.parse_thread(RAW_THREAD)
        assert len(t.messages) == 1

    def test_parse_subject(self, sample_message: MessageSummary) -> None:
        assert sample_message.subject == "Test Subject"

    def test_parse_sender(self, sample_message: MessageSummary) -> None:
        assert sample_message.sender == "sender@example.com"

    def test_parse_recipients(self, sample_message: MessageSummary) -> None:
        assert "recipient@example.com" in sample_message.recipients
        assert "cc@example.com" in sample_message.recipients

    def test_parse_labels(self, sample_message: MessageSummary) -> None:
        assert "INBOX" in sample_message.labels

    def test_parse_body_plain(self, sample_message: MessageSummary) -> None:
        assert "Hello, this is the plain text body." in sample_message.body

    def test_parse_attachment_filenames(self, sample_message: MessageSummary) -> None:
        assert "report.pdf" in sample_message.attachment_filenames

    def test_parse_mime_type(self, sample_message: MessageSummary) -> None:
        assert sample_message.mime_type == "multipart/mixed"

    def test_parse_raw_headers_lowercase(self, sample_message: MessageSummary) -> None:
        assert "subject" in sample_message.raw_headers
        assert "from" in sample_message.raw_headers

    def test_body_preview_truncation(self) -> None:
        long_body = "x" * (Config.MAX_BODY_CHARS + 500)
        raw = {
            "id": "t",
            "messages": [
                {
                    "id": "m",
                    "threadId": "t",
                    "labelIds": [],
                    "snippet": "",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [],
                        "body": {"data": _b64(long_body), "size": len(long_body)},
                        "parts": [],
                    },
                }
            ],
        }
        t = ThreadParser.parse_thread(raw)
        assert len(t.messages[0].body_preview) <= Config.MAX_BODY_CHARS

    def test_missing_subject_defaults(self) -> None:
        raw: dict[str, Any] = {
            "id": "t",
            "messages": [
                {
                    "id": "m",
                    "threadId": "t",
                    "labelIds": [],
                    "snippet": "",
                    "payload": {"mimeType": "text/plain", "headers": [], "body": {"size": 0}, "parts": []},
                }
            ],
        }
        t = ThreadParser.parse_thread(raw)
        assert t.messages[0].subject == "(no subject)"

    def test_thread_subject_from_first_message(self, sample_thread: ThreadSummary) -> None:
        assert sample_thread.subject == "Test Subject"

    def test_thread_participants_unique(self, sample_thread: ThreadSummary) -> None:
        participants = sample_thread.participants
        assert len(participants) == len(set(participants))

    def test_empty_thread(self) -> None:
        t = ThreadParser.parse_thread({"id": "t", "messages": []})
        assert t.messages == []
        assert t.subject == "(empty thread)"


# ─── HeaderAnalyzer ───────────────────────────────────────────────────────────

class TestHeaderAnalyzer:
    def test_auth_pass(self, sample_message: MessageSummary) -> None:
        rep = HeaderAnalyzer.analyze(sample_message)
        assert rep["authentication"]["dkim"] == "pass"
        assert rep["authentication"]["spf"] == "pass"
        assert rep["authentication"]["dmarc"] == "pass"

    def test_auth_absent_when_no_header(self) -> None:
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="s", sender="a@b.com",
            recipients=[], date="Mon, 01 Jan 2024 12:00:00 +0000",
            snippet="", labels=[], raw_headers={},
        )
        rep = HeaderAnalyzer.analyze(msg)
        assert rep["authentication"]["dkim"] == "absent"

    def test_spam_flag_warning(self) -> None:
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="s", sender="a@b.com",
            recipients=[], date="Mon, 01 Jan 2024 12:00:00 +0000",
            snippet="", labels=[],
            raw_headers={"x-spam-flag": "YES", "x-spam-score": "9.5"},
        )
        rep = HeaderAnalyzer.analyze(msg)
        assert any("spam" in w.lower() for w in rep["warnings"])
        assert rep["spam_headers"].get("x-spam-flag") == "YES"

    def test_reply_to_mismatch_warning(self) -> None:
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="s", sender="real@example.com",
            recipients=[], date="Mon, 01 Jan 2024 12:00:00 +0000",
            snippet="", labels=[],
            raw_headers={"reply-to": "phisher@evil.com"},
        )
        rep = HeaderAnalyzer.analyze(msg)
        assert any("Reply-To" in w for w in rep["warnings"])

    def test_invalid_date_warning(self) -> None:
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="s", sender="a@b.com",
            recipients=[], date="not-a-date",
            snippet="", labels=[], raw_headers={},
        )
        rep = HeaderAnalyzer.analyze(msg)
        assert any("parse" in w.lower() for w in rep["warnings"])

    def test_list_headers_detected(self) -> None:
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="s", sender="a@b.com",
            recipients=[], date="Mon, 01 Jan 2024 12:00:00 +0000",
            snippet="", labels=[],
            raw_headers={
                "list-unsubscribe": "<mailto:unsub@example.com>",
                "list-id": "newsletter.example.com",
            },
        )
        rep = HeaderAnalyzer.analyze(msg)
        assert "list-unsubscribe" in rep["list_headers"]

    def test_arc_present(self, sample_message: MessageSummary) -> None:
        # arc-seal not in sample — should be absent
        assert sample_message.raw_headers.get("arc-seal") is None
        rep = HeaderAnalyzer.analyze(sample_message)
        assert rep["authentication"]["arc"] == "absent"

    def test_no_warnings_on_clean_message(self) -> None:
        """A message with a recent date and clean headers should produce no warnings."""
        import email.utils
        from datetime import datetime, timezone
        recent_date = email.utils.format_datetime(datetime.now(timezone.utc))
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="Clean",
            sender="alice@example.com", recipients=["bob@example.com"],
            date=recent_date, snippet="", labels=[],
            raw_headers={
                "authentication-results": "mx.example.com; dkim=pass; spf=pass; dmarc=pass",
            },
        )
        rep = HeaderAnalyzer.analyze(msg)
        assert rep["warnings"] == []


# ─── MIMEAnalyzer ─────────────────────────────────────────────────────────────

class TestMIMEAnalyzer:
    def test_top_level_mime_type(self, sample_message: MessageSummary) -> None:
        parts = MIMEAnalyzer.analyze(sample_message)
        assert parts[0]["mime_type"] == "multipart/mixed"

    def test_attachment_detected(self, sample_message: MessageSummary) -> None:
        parts = MIMEAnalyzer.analyze(sample_message)
        attachments = [p for p in parts if p["is_attachment"]]
        assert any(a["filename"] == "report.pdf" for a in attachments)

    def test_risky_exe_warning(self) -> None:
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="s", sender="a@b.com",
            recipients=[], date="", snippet="", labels=[],
            mime_type="multipart/mixed",
            attachment_filenames=["malware.exe"],
        )
        parts = MIMEAnalyzer.analyze(msg)
        exe_parts = [p for p in parts if p.get("filename") == "malware.exe"]
        assert exe_parts
        assert exe_parts[0]["warnings"]

    def test_risky_sh_warning(self) -> None:
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="s", sender="a@b.com",
            recipients=[], date="", snippet="", labels=[],
            attachment_filenames=["setup.sh"],
        )
        parts = MIMEAnalyzer.analyze(msg)
        risky = [p for p in parts if p.get("warnings")]
        assert risky

    def test_safe_attachment_no_warning(self) -> None:
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="s", sender="a@b.com",
            recipients=[], date="", snippet="", labels=[],
            attachment_filenames=["report.pdf", "data.csv"],
        )
        parts = MIMEAnalyzer.analyze(msg)
        assert all(not p.get("warnings") for p in parts)

    def test_no_attachments(self) -> None:
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="s", sender="a@b.com",
            recipients=[], date="", snippet="", labels=[],
            mime_type="text/plain",
        )
        parts = MIMEAnalyzer.analyze(msg)
        assert not any(p["is_attachment"] for p in parts)


# ─── OllamaClient ─────────────────────────────────────────────────────────────

class TestOllamaClient:
    @respx.mock
    async def test_generate_success(self) -> None:
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]})
        )
        respx.post(f"{Config.OLLAMA_BASE_URL}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "Great summary."})
        )
        client = OllamaClient()
        result = await client.generate("Summarize.")
        assert result == "Great summary."
        await client.close()

    @respx.mock
    async def test_model_not_found(self) -> None:
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        client = OllamaClient()
        with pytest.raises(OllamaError, match="not found"):
            await client.generate("hello")
        await client.close()

    @respx.mock
    async def test_ollama_not_running(self) -> None:
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            side_effect=httpx.ConnectError("refused")
        )
        client = OllamaClient()
        with pytest.raises(OllamaError, match="running"):
            await client.generate("hello")
        await client.close()

    @respx.mock
    async def test_timeout(self) -> None:
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]})
        )
        respx.post(f"{Config.OLLAMA_BASE_URL}/api/generate").mock(
            side_effect=httpx.TimeoutException("timeout")
        )
        client = OllamaClient()
        with pytest.raises(OllamaError, match="timed out"):
            await client.generate("hello")
        await client.close()

    @respx.mock
    async def test_http_error(self) -> None:
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]})
        )
        respx.post(f"{Config.OLLAMA_BASE_URL}/api/generate").mock(
            return_value=httpx.Response(500, text="Error")
        )
        client = OllamaClient()
        with pytest.raises(OllamaError, match="500"):
            await client.generate("hello")
        await client.close()

    @respx.mock
    async def test_system_prompt_sent(self) -> None:
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]})
        )
        route = respx.post(f"{Config.OLLAMA_BASE_URL}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "ok"})
        )
        client = OllamaClient()
        await client.generate("prompt", system="You are an expert.")
        body = json.loads(route.calls[0].request.content)
        assert body["system"] == "You are an expert."
        await client.close()

    @respx.mock
    async def test_partial_model_name_match(self) -> None:
        """llama3.2:1b-instruct-q4_0 should still match 'llama3.2'."""
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "llama3.2:1b-instruct-q4_0"}]}
            )
        )
        respx.post(f"{Config.OLLAMA_BASE_URL}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "ok"})
        )
        client = OllamaClient()
        result = await client.generate("hello")
        assert result == "ok"
        await client.close()


# ─── GmailMCPClient (now wraps Gmail REST API) ──────────────────────────────────

class TestGmailMCPClient:
    """Tests for GmailMCPClient, which now uses the Gmail REST API directly."""

    BASE = "https://gmail.googleapis.com/gmail/v1"

    def _make_client(self, token: str = "tok") -> GmailMCPClient:
        tokens = AsyncMock(spec=OAuthTokenManager)
        tokens.get_access_token.return_value = token
        return GmailMCPClient(tokens)

    @respx.mock
    async def test_search_threads_success(self) -> None:
        respx.get(f"{self.BASE}/users/me/threads").mock(
            return_value=httpx.Response(200, json={"threads": [{"id": "t1"}, {"id": "t2"}]})
        )
        client = self._make_client()
        threads = await client.search_threads("from:test@example.com")
        assert threads == [{"id": "t1"}, {"id": "t2"}]
        await client.close()

    @respx.mock
    async def test_get_thread_success(self) -> None:
        respx.get(f"{self.BASE}/users/me/threads/thread_001").mock(
            return_value=httpx.Response(200, json=RAW_THREAD)
        )
        client = self._make_client()
        result = await client.get_thread("thread_001")
        assert result["id"] == "thread_001"
        await client.close()

    @respx.mock
    async def test_list_labels_success(self) -> None:
        respx.get(f"{self.BASE}/users/me/labels").mock(
            return_value=httpx.Response(200, json={"labels": [{"id": "Label_1", "name": "Work"}]})
        )
        client = self._make_client()
        labels = await client.list_labels()
        assert labels[0]["name"] == "Work"
        await client.close()

    @respx.mock
    async def test_mcp_rpc_error_raises(self) -> None:
        respx.get(f"{self.BASE}/users/me/threads").mock(
            return_value=httpx.Response(400, json={"error": {"message": "Bad Request"}})
        )
        client = self._make_client()
        with pytest.raises(GmailMCPError, match="400"):
            await client.search_threads("test")
        await client.close()

    @respx.mock
    async def test_mcp_tool_error_raises(self) -> None:
        respx.get(f"{self.BASE}/users/me/threads/bad_id").mock(
            return_value=httpx.Response(404, json={"error": {"message": "Thread not found"}})
        )
        client = self._make_client()
        with pytest.raises(GmailMCPError, match="404"):
            await client.get_thread("bad_id")
        await client.close()

    @respx.mock
    async def test_http_401_raises(self) -> None:
        respx.get(f"{self.BASE}/users/me/threads").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        client = self._make_client()
        with pytest.raises(GmailMCPError, match="401"):
            await client.search_threads("test")
        await client.close()

    @respx.mock
    async def test_connection_error_raises(self) -> None:
        respx.get(f"{self.BASE}/users/me/threads").mock(
            side_effect=httpx.ConnectError("refused")
        )
        client = self._make_client()
        with pytest.raises(GmailMCPError, match="connection error"):
            await client.search_threads("test")
        await client.close()

    @respx.mock
    async def test_timeout_raises(self) -> None:
        respx.get(f"{self.BASE}/users/me/threads").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        client = self._make_client()
        with pytest.raises(GmailMCPError, match="timed out"):
            await client.search_threads("test")
        await client.close()

    @respx.mock
    async def test_bearer_token_sent(self) -> None:
        route = respx.get(f"{self.BASE}/users/me/threads").mock(
            return_value=httpx.Response(200, json={"threads": []})
        )
        client = self._make_client(token="my_secret_token")
        await client.search_threads("test")
        auth_header = route.calls[0].request.headers.get("authorization", "")
        assert auth_header == "Bearer my_secret_token"
        await client.close()

    @respx.mock
    async def test_search_sends_query_param(self) -> None:
        route = respx.get(f"{self.BASE}/users/me/threads").mock(
            return_value=httpx.Response(200, json={"threads": []})
        )
        client = self._make_client()
        await client.search_threads("is:unread", max_results=3)
        qs = dict(httpx.URL(str(route.calls[0].request.url)).params)
        assert qs["q"] == "is:unread"
        assert qs["maxResults"] == "3"
        await client.close()


# ─── OAuthTokenManager ───────────────────────────────────────────────────────

class TestOAuthTokenManager:
    async def test_missing_credentials_raises(self) -> None:
        mgr = OAuthTokenManager(client_id="", client_secret="")
        with pytest.raises(OAuthError, match="OAUTH_CLIENT_ID"):
            await mgr.get_access_token()
        await mgr.close()

    @respx.mock
    async def test_refresh_token_used(self, tmp_path: Any) -> None:
        token_file = str(tmp_path / "token.json")
        import json as _j
        _j.dump(
            {
                "access_token": "old_tok",
                "refresh_token": "refresh_me",
                "expiry": "2000-01-01T00:00:00+00:00",  # expired
            },
            open(token_file, "w"),
        )
        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "new_tok",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        )
        mgr = OAuthTokenManager(
            client_id="cid", client_secret="csec", token_file=token_file
        )
        tok = await mgr.get_access_token()
        assert tok == "new_tok"
        await mgr.close()

    @respx.mock
    async def test_valid_cached_token_returned(self, tmp_path: Any) -> None:
        from datetime import timedelta, UTC
        from datetime import datetime
        token_file = str(tmp_path / "token.json")
        import json as _j
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        _j.dump(
            {"access_token": "cached_tok", "refresh_token": "", "expiry": future},
            open(token_file, "w"),
        )
        mgr = OAuthTokenManager(
            client_id="cid", client_secret="csec", token_file=token_file
        )
        tok = await mgr.get_access_token()
        assert tok == "cached_tok"
        await mgr.close()

    @respx.mock
    async def test_refresh_failure_raises(self, tmp_path: Any) -> None:
        token_file = str(tmp_path / "token.json")
        import json as _j
        _j.dump(
            {"access_token": "x", "refresh_token": "r", "expiry": "2000-01-01T00:00:00+00:00"},
            open(token_file, "w"),
        )
        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        mgr = OAuthTokenManager(client_id="cid", client_secret="csec", token_file=token_file)
        with pytest.raises(OAuthError, match="refresh failed"):
            await mgr.get_access_token()
        await mgr.close()


# ─── GmailAnalyzer ────────────────────────────────────────────────────────────

class TestGmailAnalyzer:
    async def test_analyze_thread_summarize(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.SUMMARIZE)
        assert isinstance(result, AnalysisResult)
        assert result.mode == AnalysisMode.SUMMARIZE
        assert result.summary == "LLM response."
        mock_ollama.generate.assert_called_once()

    async def test_analyze_thread_headers_no_llm(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.HEADERS)
        assert result.header_report
        mock_ollama.generate.assert_not_called()

    async def test_analyze_thread_mime_no_llm(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.MIME)
        assert result.mime_report
        mock_ollama.generate.assert_not_called()

    async def test_analyze_thread_troubleshoot(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.TROUBLESHOOT)
        assert result.troubleshoot_report
        mock_ollama.generate.assert_called_once()

    async def test_analyze_thread_full_calls_llm_twice(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.FULL)
        assert result.summary
        assert result.header_report
        assert result.mime_report
        assert result.troubleshoot_report
        assert mock_ollama.generate.call_count == 2

    async def test_search_and_analyze(
        self, analyzer: GmailAnalyzer, mock_mcp: AsyncMock
    ) -> None:
        results = await analyzer.search_and_analyze("from:boss@example.com", max_results=3)
        assert len(results) == 1
        mock_mcp.search_threads.assert_called_once_with(
            query="from:boss@example.com", max_results=3
        )

    async def test_search_skips_failed_threads(
        self, mock_mcp: AsyncMock, mock_ollama: AsyncMock
    ) -> None:
        mock_mcp.search_threads.return_value = [
            {"id": "good"},
            {"id": "bad"},
        ]
        mock_mcp.get_thread.side_effect = [
            RAW_THREAD,
            GmailMCPError("Not found"),
        ]
        a = GmailAnalyzer(mcp=mock_mcp, ollama=mock_ollama)
        results = await a.search_and_analyze("test")
        assert len(results) == 1

    async def test_result_timestamp_utc(self, analyzer: GmailAnalyzer) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.SUMMARIZE)
        assert result.timestamp.endswith("Z")

    async def test_result_message_count(self, analyzer: GmailAnalyzer) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.SUMMARIZE)
        assert result.message_count == 1

    async def test_mcp_error_propagates(
        self, mock_mcp: AsyncMock, mock_ollama: AsyncMock
    ) -> None:
        mock_mcp.get_thread.side_effect = GmailMCPError("Auth failed")
        a = GmailAnalyzer(mcp=mock_mcp, ollama=mock_ollama)
        with pytest.raises(GmailMCPError, match="Auth failed"):
            await a.analyze_thread("thread_001")

    async def test_oauth_error_propagates(
        self, mock_mcp: AsyncMock, mock_ollama: AsyncMock
    ) -> None:
        mock_mcp.get_thread.side_effect = OAuthError("Token expired")
        a = GmailAnalyzer(mcp=mock_mcp, ollama=mock_ollama)
        with pytest.raises(OAuthError):
            await a.analyze_thread("t1")


# ─── TroubleshootAnalyzer ─────────────────────────────────────────────────────

class TestTroubleshootAnalyzer:
    async def test_returns_llm_analysis(
        self, sample_thread: ThreadSummary, mock_ollama: AsyncMock
    ) -> None:
        ts = TroubleshootAnalyzer(mock_ollama)
        rep = await ts.analyze(sample_thread)
        assert rep["llm_analysis"] == "LLM response."

    async def test_automated_warnings_from_spam(
        self, mock_ollama: AsyncMock
    ) -> None:
        msg = MessageSummary(
            message_id="x", thread_id="t", subject="s", sender="a@b.com",
            recipients=[], date="Mon, 01 Jan 2024 12:00:00 +0000",
            snippet="", labels=[],
            raw_headers={"x-spam-flag": "YES"},
        )
        thread = ThreadSummary(thread_id="t", messages=[msg])
        ts = TroubleshootAnalyzer(mock_ollama)
        rep = await ts.analyze(thread)
        assert any("spam" in w.lower() for w in rep["automated_warnings"])

    async def test_ollama_error_propagates(
        self, sample_thread: ThreadSummary
    ) -> None:
        bad_ollama = AsyncMock(spec=OllamaClient)
        bad_ollama.generate.side_effect = OllamaError("No model")
        ts = TroubleshootAnalyzer(bad_ollama)
        with pytest.raises(OllamaError, match="No model"):
            await ts.analyze(sample_thread)

    async def test_header_and_mime_reports_present(
        self, sample_thread: ThreadSummary, mock_ollama: AsyncMock
    ) -> None:
        ts = TroubleshootAnalyzer(mock_ollama)
        rep = await ts.analyze(sample_thread)
        assert "header_reports" in rep
        assert "mime_reports" in rep


# ─── AnalysisResult shape ─────────────────────────────────────────────────────

class TestAnalysisResultShape:
    async def test_full_mode_has_all_fields(self, analyzer: GmailAnalyzer) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.FULL)
        assert isinstance(result.warnings, list)
        assert isinstance(result.mime_report, list)
        assert isinstance(result.header_report, dict)
        assert isinstance(result.troubleshoot_report, dict)

    async def test_headers_mode_no_summary(
        self, analyzer: GmailAnalyzer
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.HEADERS)
        assert result.summary == ""

    async def test_mime_mode_no_header_report(
        self, analyzer: GmailAnalyzer
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.MIME)
        assert result.header_report == {}

    async def test_summarize_mode_no_header_or_mime(
        self, analyzer: GmailAnalyzer
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.SUMMARIZE)
        assert result.header_report == {}
        assert result.mime_report == []
