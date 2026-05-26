"""
Complete pytest suite for gmail_analyzer.

Run with:
    uv run pytest -v
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from gmail_analyzer import (
    AnalysisMode,
    AnalysisResult,
    Config,
    EmailMessage,
    EmailParser,
    GmailAnalyzer,
    GmailMCPClient,
    GmailMCPError,
    HeaderAnalyzer,
    MIMEAnalyzer,
    OllamaClient,
    OllamaError,
    TroubleshootAnalyzer,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


SAMPLE_GMAIL_PAYLOAD: dict[str, Any] = {
    "id": "msg_001",
    "threadId": "thread_001",
    "labelIds": ["INBOX", "UNREAD"],
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
            {"name": "Received", "value": "from client.example.com by mail.example.com"},
        ],
        "parts": [
            {
                "mimeType": "text/plain",
                "headers": [{"name": "Content-Type", "value": "text/plain; charset=utf-8"}],
                "body": {"data": _b64("Hello, this is the plain text body."), "size": 36},
                "parts": [],
            },
            {
                "mimeType": "text/html",
                "headers": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
                "body": {"data": _b64("<p>Hello, this is the HTML body.</p>"), "size": 36},
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


@pytest.fixture
def sample_payload() -> dict[str, Any]:
    return SAMPLE_GMAIL_PAYLOAD


@pytest.fixture
def sample_email(sample_payload: dict[str, Any]) -> EmailMessage:
    return EmailParser.parse(sample_payload)


@pytest.fixture
def mock_ollama() -> AsyncMock:
    mock = AsyncMock(spec=OllamaClient)
    mock.generate.return_value = "This is an LLM response."
    return mock


@pytest.fixture
def mock_mcp() -> AsyncMock:
    mock = AsyncMock(spec=GmailMCPClient)
    mock.get_raw_message.return_value = SAMPLE_GMAIL_PAYLOAD
    mock.list_messages.return_value = [{"id": "msg_001", "threadId": "thread_001"}]
    mock.get_thread.return_value = [SAMPLE_GMAIL_PAYLOAD]
    return mock


@pytest.fixture
def analyzer(mock_mcp: AsyncMock, mock_ollama: AsyncMock) -> GmailAnalyzer:
    return GmailAnalyzer(mcp=mock_mcp, ollama=mock_ollama)


# ─── EmailParser ─────────────────────────────────────────────────────────────

class TestEmailParser:
    def test_parse_basic_fields(self, sample_payload: dict[str, Any]) -> None:
        msg = EmailParser.parse(sample_payload)
        assert msg.message_id == "msg_001"
        assert msg.thread_id == "thread_001"
        assert msg.subject == "Test Subject"
        assert msg.sender == "sender@example.com"

    def test_parse_recipients(self, sample_payload: dict[str, Any]) -> None:
        msg = EmailParser.parse(sample_payload)
        assert "recipient@example.com" in msg.recipients
        assert "cc@example.com" in msg.recipients

    def test_parse_body_plain(self, sample_email: EmailMessage) -> None:
        assert "Hello, this is the plain text body." in sample_email.body_plain

    def test_parse_body_html(self, sample_email: EmailMessage) -> None:
        assert "<p>Hello" in sample_email.body_html

    def test_parse_labels(self, sample_email: EmailMessage) -> None:
        assert "INBOX" in sample_email.labels
        assert "UNREAD" in sample_email.labels

    def test_parse_raw_headers_lowercased(self, sample_email: EmailMessage) -> None:
        assert "subject" in sample_email.raw_headers
        assert "from" in sample_email.raw_headers
        assert "authentication-results" in sample_email.raw_headers

    def test_parse_mime_structure(self, sample_email: EmailMessage) -> None:
        assert len(sample_email.mime_structure) >= 1
        assert sample_email.mime_structure[0]["mimeType"] == "multipart/mixed"

    def test_parse_missing_subject(self) -> None:
        payload: dict[str, Any] = {
            "id": "x",
            "threadId": "t",
            "labelIds": [],
            "payload": {"mimeType": "text/plain", "headers": [], "body": {"size": 0}, "parts": []},
        }
        msg = EmailParser.parse(payload)
        assert msg.subject == "(no subject)"

    def test_parse_encoded_subject(self) -> None:
        """RFC 2047 encoded subject should be decoded."""
        import quopri
        encoded = "=?utf-8?Q?Hello_W=C3=B6rld?="
        payload = {
            "id": "x",
            "threadId": "t",
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": encoded}],
                "body": {"size": 0},
                "parts": [],
            },
        }
        msg = EmailParser.parse(payload)
        assert "Hello" in msg.subject

    def test_body_preview_truncation(self, sample_payload: dict[str, Any]) -> None:
        long_body = "x" * (Config.MAX_EMAIL_BODY_CHARS + 500)
        payload = dict(sample_payload)
        payload["payload"] = dict(sample_payload["payload"])
        payload["payload"]["parts"] = [
            {
                "mimeType": "text/plain",
                "headers": [],
                "body": {"data": _b64(long_body), "size": len(long_body)},
                "parts": [],
            }
        ]
        msg = EmailParser.parse(payload)
        assert len(msg.body_preview) <= Config.MAX_EMAIL_BODY_CHARS

    def test_delivery_path(self, sample_email: EmailMessage) -> None:
        assert len(sample_email.delivery_path) == 2

    def test_auth_results_parsed(self, sample_email: EmailMessage) -> None:
        auth = sample_email.auth_results
        assert auth.get("dkim") == "pass"
        assert auth.get("spf") == "pass"
        assert auth.get("dmarc") == "pass"


# ─── HeaderAnalyzer ──────────────────────────────────────────────────────────

class TestHeaderAnalyzer:
    def test_authentication_pass(self, sample_email: EmailMessage) -> None:
        report = HeaderAnalyzer.analyze(sample_email)
        auth = report["authentication"]
        assert auth["dkim"] == "pass"
        assert auth["spf"] == "pass"
        assert auth["dmarc"] == "pass"

    def test_authentication_missing(self) -> None:
        payload: dict[str, Any] = {
            "id": "x",
            "threadId": "t",
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
                    {"name": "From", "value": "a@b.com"},
                ],
                "body": {"size": 0},
                "parts": [],
            },
        }
        msg = EmailParser.parse(payload)
        report = HeaderAnalyzer.analyze(msg)
        assert report["authentication"]["dkim"] == "absent"

    def test_spam_flag_warning(self) -> None:
        payload: dict[str, Any] = {
            "id": "x",
            "threadId": "t",
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
                    {"name": "From", "value": "a@b.com"},
                    {"name": "X-Spam-Flag", "value": "YES"},
                    {"name": "X-Spam-Score", "value": "7.5"},
                ],
                "body": {"size": 0},
                "parts": [],
            },
        }
        msg = EmailParser.parse(payload)
        report = HeaderAnalyzer.analyze(msg)
        assert any("spam" in w.lower() for w in report["warnings"])
        assert report["spam_headers"].get("x-spam-flag") == "YES"

    def test_reply_to_mismatch_warning(self) -> None:
        payload: dict[str, Any] = {
            "id": "x",
            "threadId": "t",
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
                    {"name": "From", "value": "real@example.com"},
                    {"name": "Reply-To", "value": "phisher@evil.com"},
                ],
                "body": {"size": 0},
                "parts": [],
            },
        }
        msg = EmailParser.parse(payload)
        report = HeaderAnalyzer.analyze(msg)
        assert any("Reply-To" in w for w in report["warnings"])

    def test_many_hops_warning(self) -> None:
        headers = [
            {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
            {"name": "From", "value": "a@b.com"},
        ]
        for i in range(10):
            headers.append({"name": "Received", "value": f"from hop{i}.example.com"})
        payload: dict[str, Any] = {
            "id": "x",
            "threadId": "t",
            "labelIds": [],
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"size": 0}, "parts": []},
        }
        msg = EmailParser.parse(payload)
        report = HeaderAnalyzer.analyze(msg)
        assert any("hop" in w.lower() for w in report["warnings"])

    def test_list_headers_detected(self) -> None:
        payload: dict[str, Any] = {
            "id": "x",
            "threadId": "t",
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
                    {"name": "From", "value": "a@b.com"},
                    {"name": "List-Unsubscribe", "value": "<mailto:unsub@example.com>"},
                    {"name": "List-Id", "value": "newsletter.example.com"},
                ],
                "body": {"size": 0},
                "parts": [],
            },
        }
        msg = EmailParser.parse(payload)
        report = HeaderAnalyzer.analyze(msg)
        assert "list-unsubscribe" in report["list_headers"]


# ─── MIMEAnalyzer ─────────────────────────────────────────────────────────────

class TestMIMEAnalyzer:
    def test_basic_parts(self, sample_email: EmailMessage) -> None:
        report = MIMEAnalyzer.analyze(sample_email)
        mime_types = {p["mime_type"] for p in report}
        assert "multipart/mixed" in mime_types
        assert "text/plain" in mime_types
        assert "application/pdf" in mime_types

    def test_attachment_detected(self, sample_email: EmailMessage) -> None:
        report = MIMEAnalyzer.analyze(sample_email)
        attachments = [p for p in report if p["is_attachment"]]
        assert any(a["filename"] == "report.pdf" for a in attachments)

    def test_risky_mime_type_warning(self) -> None:
        payload: dict[str, Any] = {
            "id": "x",
            "threadId": "t",
            "labelIds": [],
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [],
                "body": {"size": 0},
                "parts": [
                    {
                        "mimeType": "application/x-msdownload",
                        "filename": "malware.exe",
                        "headers": [],
                        "body": {"size": 99},
                        "parts": [],
                    }
                ],
            },
        }
        msg = EmailParser.parse(payload)
        report = MIMEAnalyzer.analyze(msg)
        exe_parts = [p for p in report if "x-msdownload" in p["mime_type"]]
        assert exe_parts
        assert exe_parts[0]["warnings"]

    def test_deep_nesting_warning(self) -> None:
        """Build a 6-level deep MIME tree."""
        def nested(depth: int) -> dict[str, Any]:
            if depth == 0:
                return {
                    "mimeType": "text/plain",
                    "headers": [],
                    "body": {"data": _b64("leaf"), "size": 4},
                    "parts": [],
                }
            return {
                "mimeType": "multipart/mixed",
                "headers": [],
                "body": {"size": 0},
                "parts": [nested(depth - 1)],
            }

        payload: dict[str, Any] = {
            "id": "x",
            "threadId": "t",
            "labelIds": [],
            "payload": nested(6),
        }
        msg = EmailParser.parse(payload)
        report = MIMEAnalyzer.analyze(msg)
        deep_warnings = [
            w for p in report for w in p.get("warnings", []) if "Deeply nested" in w
        ]
        assert deep_warnings


# ─── OllamaClient ─────────────────────────────────────────────────────────────

class TestOllamaClient:
    @respx.mock
    async def test_generate_success(self) -> None:
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            return_value=httpx.Response(
                200,
                json={"models": [{"name": "llama3.2:1b"}]},
            )
        )
        respx.post(f"{Config.OLLAMA_BASE_URL}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "Great summary."})
        )
        client = OllamaClient()
        result = await client.generate("Summarize this.")
        assert result == "Great summary."
        await client.close()

    @respx.mock
    async def test_model_not_found_raises(self) -> None:
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        client = OllamaClient()
        with pytest.raises(OllamaError, match="not found"):
            await client.generate("hello")
        await client.close()

    @respx.mock
    async def test_ollama_not_running_raises(self) -> None:
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            side_effect=httpx.ConnectError("refused")
        )
        client = OllamaClient()
        with pytest.raises(OllamaError, match="running"):
            await client.generate("hello")
        await client.close()

    @respx.mock
    async def test_generate_timeout_raises(self) -> None:
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
    async def test_generate_http_error_raises(self) -> None:
        respx.get(f"{Config.OLLAMA_BASE_URL}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]})
        )
        respx.post(f"{Config.OLLAMA_BASE_URL}/api/generate").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        client = OllamaClient()
        with pytest.raises(OllamaError, match="500"):
            await client.generate("hello")
        await client.close()

    @respx.mock
    async def test_system_prompt_included(self) -> None:
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


# ─── GmailMCPClient ───────────────────────────────────────────────────────────

class TestGmailMCPClient:
    @respx.mock
    async def test_list_messages_success(self) -> None:
        respx.post("http://localhost:3000/rpc").mock(
            return_value=httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"messages": [{"id": "m1", "threadId": "t1"}]},
                },
            )
        )
        client = GmailMCPClient()
        msgs = await client.list_messages(query="from:test@example.com")
        assert msgs == [{"id": "m1", "threadId": "t1"}]
        await client.close()

    @respx.mock
    async def test_mcp_error_response_raises(self) -> None:
        respx.post("http://localhost:3000/rpc").mock(
            return_value=httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32600, "message": "Invalid request"},
                },
            )
        )
        client = GmailMCPClient()
        with pytest.raises(GmailMCPError, match="Invalid request"):
            await client.list_messages()
        await client.close()

    @respx.mock
    async def test_mcp_http_error_raises(self) -> None:
        respx.post("http://localhost:3000/rpc").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        client = GmailMCPClient()
        with pytest.raises(GmailMCPError, match="401"):
            await client.list_messages()
        await client.close()

    @respx.mock
    async def test_mcp_connection_error_raises(self) -> None:
        respx.post("http://localhost:3000/rpc").mock(
            side_effect=httpx.ConnectError("refused")
        )
        client = GmailMCPClient()
        with pytest.raises(GmailMCPError, match="connection error"):
            await client.list_messages()
        await client.close()

    @respx.mock
    async def test_get_raw_message_empty_result_raises(self) -> None:
        respx.post("http://localhost:3000/rpc").mock(
            return_value=httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": None},
            )
        )
        client = GmailMCPClient()
        with pytest.raises(GmailMCPError, match="Empty response"):
            await client.get_raw_message("msg_999")
        await client.close()

    @respx.mock
    async def test_timeout_raises(self) -> None:
        respx.post("http://localhost:3000/rpc").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        client = GmailMCPClient()
        with pytest.raises(GmailMCPError, match="timed out"):
            await client.list_messages()
        await client.close()


# ─── GmailAnalyzer ────────────────────────────────────────────────────────────

class TestGmailAnalyzer:
    async def test_analyze_message_summarize(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_message("msg_001", AnalysisMode.SUMMARIZE)
        assert isinstance(result, AnalysisResult)
        assert result.mode == AnalysisMode.SUMMARIZE
        assert result.summary == "This is an LLM response."
        mock_ollama.generate.assert_called_once()

    async def test_analyze_message_headers(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_message("msg_001", AnalysisMode.HEADERS)
        assert result.header_report
        # LLM should NOT be called for headers-only mode
        mock_ollama.generate.assert_not_called()

    async def test_analyze_message_mime(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_message("msg_001", AnalysisMode.MIME)
        assert result.mime_report
        mock_ollama.generate.assert_not_called()

    async def test_analyze_message_troubleshoot(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_message("msg_001", AnalysisMode.TROUBLESHOOT)
        assert result.troubleshoot_report
        mock_ollama.generate.assert_called_once()

    async def test_analyze_message_full(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_message("msg_001", AnalysisMode.FULL)
        assert result.summary
        assert result.header_report
        assert result.mime_report
        assert result.troubleshoot_report
        # full mode calls LLM twice: once for summary, once for troubleshoot
        assert mock_ollama.generate.call_count == 2

    async def test_analyze_thread(
        self, analyzer: GmailAnalyzer, mock_mcp: AsyncMock
    ) -> None:
        results = await analyzer.analyze_thread("thread_001", AnalysisMode.SUMMARIZE)
        assert len(results) == 1
        mock_mcp.get_thread.assert_called_once_with("thread_001")

    async def test_search_and_analyze(
        self, analyzer: GmailAnalyzer, mock_mcp: AsyncMock
    ) -> None:
        results = await analyzer.search_and_analyze("from:boss@example.com", max_results=3)
        assert len(results) == 1
        mock_mcp.list_messages.assert_called_once_with(
            query="from:boss@example.com", max_results=3
        )

    async def test_search_skips_failed_messages(
        self, mock_mcp: AsyncMock, mock_ollama: AsyncMock
    ) -> None:
        mock_mcp.list_messages.return_value = [
            {"id": "msg_good", "threadId": "t1"},
            {"id": "msg_bad", "threadId": "t2"},
        ]
        mock_mcp.get_raw_message.side_effect = [
            SAMPLE_GMAIL_PAYLOAD,
            GmailMCPError("Not found"),
        ]
        analyzer = GmailAnalyzer(mcp=mock_mcp, ollama=mock_ollama)
        results = await analyzer.search_and_analyze("test")
        # bad message should be skipped, not raise
        assert len(results) == 1
        assert results[0].message_id == "msg_001"

    async def test_result_has_timestamp(
        self, analyzer: GmailAnalyzer
    ) -> None:
        result = await analyzer.analyze_message("msg_001", AnalysisMode.SUMMARIZE)
        assert result.timestamp.endswith("Z")

    async def test_mcp_error_propagates(
        self, mock_mcp: AsyncMock, mock_ollama: AsyncMock
    ) -> None:
        mock_mcp.get_raw_message.side_effect = GmailMCPError("Auth failed")
        analyzer = GmailAnalyzer(mcp=mock_mcp, ollama=mock_ollama)
        with pytest.raises(GmailMCPError, match="Auth failed"):
            await analyzer.analyze_message("msg_001")


# ─── TroubleshootAnalyzer ─────────────────────────────────────────────────────

class TestTroubleshootAnalyzer:
    async def test_returns_llm_analysis(
        self, sample_email: EmailMessage, mock_ollama: AsyncMock
    ) -> None:
        ts = TroubleshootAnalyzer(mock_ollama)
        report = await ts.analyze(sample_email)
        assert "llm_analysis" in report
        assert report["llm_analysis"] == "This is an LLM response."

    async def test_automated_warnings_included(
        self, mock_ollama: AsyncMock
    ) -> None:
        """A message with spam flag should produce automated warnings."""
        payload: dict[str, Any] = {
            "id": "x",
            "threadId": "t",
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
                    {"name": "From", "value": "a@b.com"},
                    {"name": "X-Spam-Flag", "value": "YES"},
                ],
                "body": {"size": 0},
                "parts": [],
            },
        }
        msg = EmailParser.parse(payload)
        ts = TroubleshootAnalyzer(mock_ollama)
        report = await ts.analyze(msg)
        assert any("spam" in w.lower() for w in report["automated_warnings"])

    async def test_ollama_error_propagates(
        self, sample_email: EmailMessage
    ) -> None:
        failing_ollama = AsyncMock(spec=OllamaClient)
        failing_ollama.generate.side_effect = OllamaError("Model not found")
        ts = TroubleshootAnalyzer(failing_ollama)
        with pytest.raises(OllamaError, match="Model not found"):
            await ts.analyze(sample_email)


# ─── Integration-style: AnalysisResult shape ─────────────────────────────────

class TestAnalysisResultShape:
    async def test_full_mode_result_fields(
        self, analyzer: GmailAnalyzer
    ) -> None:
        result = await analyzer.analyze_message("msg_001", AnalysisMode.FULL)
        assert result.mode == AnalysisMode.FULL
        assert isinstance(result.warnings, list)
        assert isinstance(result.mime_report, list)
        assert isinstance(result.header_report, dict)
        assert isinstance(result.troubleshoot_report, dict)

    async def test_summarize_mode_no_header_report(
        self, analyzer: GmailAnalyzer
    ) -> None:
        result = await analyzer.analyze_message("msg_001", AnalysisMode.SUMMARIZE)
        assert result.header_report == {}

    async def test_headers_mode_no_llm_summary(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_message("msg_001", AnalysisMode.HEADERS)
        assert result.summary == ""
        mock_ollama.generate.assert_not_called()
