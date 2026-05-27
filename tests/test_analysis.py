"""Tests for mailmind.analysis — HeaderAnalyzer, MIMEAnalyzer, TroubleshootAnalyzer, GmailAnalyzer."""

from __future__ import annotations

import email.utils
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from mailmind.analysis import GmailAnalyzer, HeaderAnalyzer, MIMEAnalyzer, TroubleshootAnalyzer
from mailmind.config import Settings
from mailmind.models import AnalysisMode, AnalysisResult, GmailError, MessageSummary, OllamaError, ThreadSummary
from mailmind.ollama import OllamaClient
from tests.conftest import RAW_THREAD


def _msg(**kwargs: str) -> MessageSummary:
    defaults = dict(
        message_id="x", thread_id="t", subject="s", sender="a@b.com",
        recipients=[], date="Mon, 01 Jan 2024 12:00:00 +0000",
        snippet="", labels=[],
    )
    defaults.update(kwargs)
    return MessageSummary(**defaults)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# HeaderAnalyzer
# ─────────────────────────────────────────────────────────────────────────────


class TestHeaderAnalyzer:
    def test_auth_pass(self, sample_message: MessageSummary) -> None:
        rep = HeaderAnalyzer.analyze(sample_message)
        assert rep["authentication"]["dkim"]  == "pass"
        assert rep["authentication"]["spf"]   == "pass"
        assert rep["authentication"]["dmarc"] == "pass"

    def test_auth_absent_when_no_header(self) -> None:
        rep = HeaderAnalyzer.analyze(_msg(raw_headers={}))
        assert rep["authentication"]["dkim"] == "absent"

    def test_spam_flag_warning(self) -> None:
        rep = HeaderAnalyzer.analyze(
            _msg(raw_headers={"x-spam-flag": "YES", "x-spam-score": "9.5"})
        )
        assert any("spam" in w.lower() for w in rep["warnings"])

    def test_reply_to_mismatch(self) -> None:
        rep = HeaderAnalyzer.analyze(
            _msg(sender="real@example.com", raw_headers={"reply-to": "phisher@evil.com"})
        )
        assert any("Reply-To" in w for w in rep["warnings"])

    def test_unparseable_date(self) -> None:
        rep = HeaderAnalyzer.analyze(_msg(date="not-a-date", raw_headers={}))
        assert any("parse" in w.lower() for w in rep["warnings"])

    def test_list_headers_detected(self) -> None:
        rep = HeaderAnalyzer.analyze(
            _msg(raw_headers={"list-unsubscribe": "<mailto:unsub@example.com>"})
        )
        assert "list-unsubscribe" in rep["list_headers"]

    def test_arc_absent(self, sample_message: MessageSummary) -> None:
        assert HeaderAnalyzer.analyze(sample_message)["authentication"]["arc"] == "absent"

    def test_no_warnings_clean_message(self) -> None:
        recent = email.utils.format_datetime(datetime.now(UTC))
        rep = HeaderAnalyzer.analyze(_msg(
            date=recent,
            raw_headers={"authentication-results": "mx.example.com; dkim=pass; spf=pass; dmarc=pass"},
        ))
        assert rep["warnings"] == []


# ─────────────────────────────────────────────────────────────────────────────
# MIMEAnalyzer
# ─────────────────────────────────────────────────────────────────────────────


class TestMIMEAnalyzer:
    def test_top_level_mime(self, sample_message: MessageSummary) -> None:
        assert MIMEAnalyzer.analyze(sample_message)[0]["mime_type"] == "multipart/mixed"

    def test_pdf_detected(self, sample_message: MessageSummary) -> None:
        parts = MIMEAnalyzer.analyze(sample_message)
        assert any(p.get("filename") == "report.pdf" for p in parts if p["is_attachment"])

    def test_exe_warning(self) -> None:
        risky = [
            p for p in MIMEAnalyzer.analyze(_msg(attachment_filenames=["bad.exe"]))
            if p.get("filename") == "bad.exe"
        ]
        assert risky and risky[0]["warnings"]

    def test_sh_warning(self) -> None:
        assert any(
            p.get("warnings") for p in MIMEAnalyzer.analyze(_msg(attachment_filenames=["s.sh"]))
        )

    def test_safe_attachments_no_warnings(self) -> None:
        parts = MIMEAnalyzer.analyze(_msg(attachment_filenames=["report.pdf", "data.csv"]))
        assert all(not p.get("warnings") for p in parts)

    def test_no_attachments(self) -> None:
        parts = MIMEAnalyzer.analyze(_msg(mime_type="text/plain"))
        assert not any(p["is_attachment"] for p in parts)


# ─────────────────────────────────────────────────────────────────────────────
# TroubleshootAnalyzer
# ─────────────────────────────────────────────────────────────────────────────


class TestTroubleshootAnalyzer:
    async def test_llm_analysis_populated(
        self, sample_thread: ThreadSummary, mock_ollama: AsyncMock
    ) -> None:
        rep = await TroubleshootAnalyzer(mock_ollama).analyze(sample_thread)
        assert rep["llm_analysis"] == "LLM response."

    async def test_spam_warning_propagated(self, mock_ollama: AsyncMock) -> None:
        thread = ThreadSummary(
            thread_id="t",
            messages=[_msg(raw_headers={"x-spam-flag": "YES"})],
        )
        rep = await TroubleshootAnalyzer(mock_ollama).analyze(thread)
        assert any("spam" in w.lower() for w in rep["automated_warnings"])

    async def test_ollama_error_propagates(self, sample_thread: ThreadSummary) -> None:
        bad = AsyncMock(spec=OllamaClient)
        bad.generate.side_effect = OllamaError("no model")
        with pytest.raises(OllamaError):
            await TroubleshootAnalyzer(bad).analyze(sample_thread)

    async def test_reports_present(
        self, sample_thread: ThreadSummary, mock_ollama: AsyncMock
    ) -> None:
        rep = await TroubleshootAnalyzer(mock_ollama).analyze(sample_thread)
        assert "header_reports" in rep and "mime_reports" in rep


# ─────────────────────────────────────────────────────────────────────────────
# GmailAnalyzer
# ─────────────────────────────────────────────────────────────────────────────


class TestGmailAnalyzer:
    async def test_summarize_calls_llm_once(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.SUMMARIZE)
        assert result.summary == "LLM response."
        mock_ollama.generate.assert_called_once()

    async def test_headers_mode_no_llm(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.HEADERS)
        assert result.header_report
        mock_ollama.generate.assert_not_called()

    async def test_mime_mode_no_llm(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.MIME)
        assert result.mime_report
        mock_ollama.generate.assert_not_called()

    async def test_troubleshoot_calls_llm_once(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.TROUBLESHOOT)
        assert result.troubleshoot_report
        mock_ollama.generate.assert_called_once()

    async def test_full_mode_calls_llm_twice(
        self, analyzer: GmailAnalyzer, mock_ollama: AsyncMock
    ) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.FULL)
        assert result.summary and result.header_report and result.mime_report
        assert mock_ollama.generate.call_count == 2

    async def test_search_and_analyze(
        self, analyzer: GmailAnalyzer, mock_gmail: AsyncMock
    ) -> None:
        results = await analyzer.search_and_analyze("from:boss@corp.com", max_results=3)
        assert len(results) == 1
        mock_gmail.search_threads.assert_called_once_with("from:boss@corp.com", max_results=3)

    async def test_failed_thread_skipped(
        self, settings: Settings, mock_ollama: AsyncMock
    ) -> None:
        gmail = AsyncMock()
        gmail.search_threads.return_value = [{"id": "good"}, {"id": "bad"}]
        gmail.get_thread.side_effect = [RAW_THREAD, GmailError("not found")]
        results = await GmailAnalyzer(settings, gmail, mock_ollama).search_and_analyze("test")
        assert len(results) == 1

    async def test_timestamp_ends_with_z(self, analyzer: GmailAnalyzer) -> None:
        result = await analyzer.analyze_thread("thread_001", AnalysisMode.SUMMARIZE)
        assert result.timestamp.endswith("Z")

    async def test_message_count(self, analyzer: GmailAnalyzer) -> None:
        assert (await analyzer.analyze_thread("thread_001", AnalysisMode.SUMMARIZE)).message_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# AnalysisResult shape invariants
# ─────────────────────────────────────────────────────────────────────────────


class TestAnalysisResultShape:
    async def test_full_populates_all(self, analyzer: GmailAnalyzer) -> None:
        r = await analyzer.analyze_thread("thread_001", AnalysisMode.FULL)
        assert isinstance(r.warnings, list)
        assert isinstance(r.mime_report, list)
        assert isinstance(r.header_report, dict)
        assert isinstance(r.troubleshoot_report, dict)

    async def test_headers_mode_no_summary(self, analyzer: GmailAnalyzer) -> None:
        assert (await analyzer.analyze_thread("thread_001", AnalysisMode.HEADERS)).summary == ""

    async def test_mime_mode_no_header_report(self, analyzer: GmailAnalyzer) -> None:
        assert (await analyzer.analyze_thread("thread_001", AnalysisMode.MIME)).header_report == {}

    async def test_summarize_no_header_or_mime(self, analyzer: GmailAnalyzer) -> None:
        r = await analyzer.analyze_thread("thread_001", AnalysisMode.SUMMARIZE)
        assert r.header_report == {} and r.mime_report == []
