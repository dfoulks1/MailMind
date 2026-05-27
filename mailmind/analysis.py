"""
mailmind.analysis — email analysis passes.

``HeaderAnalyzer``      Pure-Python heuristic header report (no network).
``MIMEAnalyzer``        MIME structure and risky-attachment detection.
``TroubleshootAnalyzer`` Combined heuristic + Ollama LLM analysis.
``GmailAnalyzer``       Orchestrator: Gmail API → parse → analyze → result.
"""

from __future__ import annotations

import email.utils
import json
import logging
import textwrap
from datetime import UTC, datetime
from typing import Any

from mailmind.config import Settings
from mailmind.gmail import GmailClient, ThreadParser
from mailmind.models import (
    AnalysisMode,
    AnalysisResult,
    GmailError,
    MessageSummary,
    OllamaError,
    ThreadSummary,
)
from mailmind.ollama import OllamaClient

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_KNOWN_SPAM_HEADERS: frozenset[str] = frozenset(
    {"x-spam-status", "x-spam-flag", "x-spam-score"}
)

_RISKY_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {"exe", "bat", "sh", "cmd", "ps1", "vbs", "js", "msi"}
)

_SYSTEM_ANALYST = textwrap.dedent("""
    You are an expert email analyst and deliverability engineer.
    Be concise, technical where needed, and structure your answers clearly.
    Only work from the data provided; do not invent header values or details.
""").strip()


# ─────────────────────────────────────────────────────────────────────────────
# HeaderAnalyzer
# ─────────────────────────────────────────────────────────────────────────────


class HeaderAnalyzer:
    """
    Pure-Python heuristic analysis of a single message's headers.

    No external dependencies or network calls.  All analysis is deterministic
    given the same ``MessageSummary`` input.

    Checks performed
    ----------------
    * DKIM / SPF / DMARC from ``Authentication-Results``
    * ARC chain presence (``arc-seal`` header)
    * Spam flags (``X-Spam-Flag``, ``X-Spam-Score``, ``X-Spam-Status``)
    * Delivery hop estimate from ``Authentication-Results``
    * Date header sanity (>2 days from now triggers a warning)
    * Reply-To / From mismatch
    * ``List-*`` headers (newsletter / bulk mail detection)
    """

    @staticmethod
    def analyze(msg: MessageSummary) -> dict[str, Any]:
        """
        Analyse ``msg`` and return a structured header report.

        Args:
            msg: A ``MessageSummary`` with ``raw_headers`` populated.

        Returns:
            Dict with keys: ``authentication``, ``spam_headers``,
            ``delivery_hop_estimate``, ``list_headers``, ``date``,
            ``warnings``.
        """
        h        = msg.raw_headers
        warnings: list[str] = []

        # ── DKIM / SPF / DMARC ────────────────────────────────────────────────
        auth_raw = h.get("authentication-results", "")
        auth: dict[str, str] = {}
        for part in auth_raw.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                auth[k.strip().lower()] = v.strip().split()[0].lower()

        auth_report = {
            "dkim":  auth.get("dkim",  "absent"),
            "spf":   auth.get("spf",   "absent"),
            "dmarc": auth.get("dmarc", "absent"),
            "arc":   "present" if "arc-seal" in h else "absent",
        }
        for proto in ("dkim", "spf", "dmarc"):
            val = auth_report[proto]
            if val not in ("pass", "absent"):
                warnings.append(f"{proto.upper()} check: {val!r}")

        # ── Spam headers ──────────────────────────────────────────────────────
        spam: dict[str, str] = {k: h[k] for k in _KNOWN_SPAM_HEADERS if k in h}
        if spam.get("x-spam-flag", "").upper() == "YES":
            warnings.append("Message flagged as spam (X-Spam-Flag: YES)")

        # ── Delivery hop estimate ─────────────────────────────────────────────
        received_count = auth_raw.count("by ") + 1
        if received_count > 8:
            warnings.append(f"Unusually long delivery path (~{received_count} hops)")

        # ── Date sanity ───────────────────────────────────────────────────────
        try:
            parsed_date = email.utils.parsedate_to_datetime(msg.date)
            delta = abs(
                (datetime.now(parsed_date.tzinfo) - parsed_date).total_seconds()
            )
            if delta > 86_400 * 2:
                warnings.append(
                    f"Message date is >2 days off from now ({msg.date!r})"
                )
        except Exception:
            if msg.date:
                warnings.append(f"Could not parse Date header: {msg.date!r}")

        # ── Reply-To mismatch ─────────────────────────────────────────────────
        reply_to = h.get("reply-to", "")
        if reply_to and reply_to != msg.sender:
            warnings.append(
                f"Reply-To ({reply_to!r}) differs from From ({msg.sender!r})"
            )

        return {
            "authentication":       auth_report,
            "spam_headers":         spam,
            "delivery_hop_estimate": received_count,
            "list_headers":         {k: v for k, v in h.items() if k.startswith("list-")},
            "date":                 msg.date,
            "warnings":             warnings,
        }


# ─────────────────────────────────────────────────────────────────────────────
# MIMEAnalyzer
# ─────────────────────────────────────────────────────────────────────────────


class MIMEAnalyzer:
    """
    Analyse the MIME structure of a single message.

    Returns one descriptor per message part: the top-level part plus one entry
    per attachment.  Each descriptor has a ``warnings`` list populated when a
    risky file extension is detected.

    Risky extensions: ``exe bat sh cmd ps1 vbs js msi``
    """

    @staticmethod
    def analyze(msg: MessageSummary) -> list[dict[str, Any]]:
        """
        Analyse the MIME structure of ``msg``.

        Args:
            msg: A ``MessageSummary`` with ``mime_type`` and
                 ``attachment_filenames`` populated.

        Returns:
            List of part dicts.  Keys: ``mime_type``, ``is_attachment``,
            ``filename``, ``size_bytes``, ``warnings``.  First element is
            always the top-level message part.
        """
        parts: list[dict[str, Any]] = [
            {
                "mime_type":    msg.mime_type,
                "is_attachment": False,
                "filename":     None,
                "size_bytes":   None,
                "warnings":     [],
            }
        ]
        for fname in msg.attachment_filenames:
            ext   = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            entry: dict[str, Any] = {
                "mime_type":    "attachment",
                "is_attachment": True,
                "filename":     fname,
                "size_bytes":   None,
                "warnings":     [],
            }
            if ext in _RISKY_ATTACHMENT_EXTENSIONS:
                entry["warnings"].append(
                    f"Potentially risky attachment: {fname!r} (.{ext})"
                )
            parts.append(entry)
        return parts


# ─────────────────────────────────────────────────────────────────────────────
# TroubleshootAnalyzer
# ─────────────────────────────────────────────────────────────────────────────


class TroubleshootAnalyzer:
    """
    Combines header and MIME heuristics with an Ollama LLM call to produce
    a prioritised list of delivery, security, and MIME issues for a thread.

    The LLM receives only the structured analysis output — not the raw body —
    to keep the prompt compact and focused.
    """

    def __init__(self, ollama: OllamaClient) -> None:
        self._ollama = ollama

    async def analyze(self, thread: ThreadSummary) -> dict[str, Any]:
        """
        Run heuristic passes on every message then ask the LLM to prioritise.

        Args:
            thread: Fully populated ``ThreadSummary``.

        Returns:
            Dict with keys: ``header_reports``, ``mime_reports``,
            ``automated_warnings``, ``llm_analysis``.

        Raises:
            OllamaError: If the LLM call fails.
        """
        header_reports = {
            m.message_id: HeaderAnalyzer.analyze(m) for m in thread.messages
        }
        mime_reports = {
            m.message_id: MIMEAnalyzer.analyze(m) for m in thread.messages
        }

        all_warnings: list[str] = []
        for rep in header_reports.values():
            all_warnings.extend(rep.get("warnings", []))
        for parts in mime_reports.values():
            for p in parts:
                all_warnings.extend(p.get("warnings", []))

        first       = thread.messages[0] if thread.messages else None
        auth_summary = header_reports[first.message_id]["authentication"] if first else {}
        all_attachments = [
            f for m in thread.messages for f in m.attachment_filenames
        ]

        prompt = textwrap.dedent(f"""
            Analyze this email thread for delivery, security, or MIME issues.

            Subject: {thread.subject}
            Participants: {', '.join(thread.participants[:5])}
            Message count: {len(thread.messages)}

            First message authentication:
            {json.dumps(auth_summary, indent=2)}

            Attachment filenames across thread:
            {json.dumps(all_attachments, indent=2)}

            Automated warnings raised:
            {chr(10).join(f'- {w}' for w in all_warnings) or '(none)'}

            Provide a prioritised list of issues and recommended fixes.
        """).strip()

        llm_analysis = await self._ollama.generate(prompt, system=_SYSTEM_ANALYST)

        return {
            "header_reports":    header_reports,
            "mime_reports":      mime_reports,
            "automated_warnings": all_warnings,
            "llm_analysis":      llm_analysis,
        }


# ─────────────────────────────────────────────────────────────────────────────
# GmailAnalyzer
# ─────────────────────────────────────────────────────────────────────────────


class GmailAnalyzer:
    """
    High-level orchestrator: Gmail API → parse → analysis passes → result.

    Typical usage::

        analyzer = GmailAnalyzer(settings, gmail_client, ollama_client)
        result   = await analyzer.analyze_thread("18f3a2...", AnalysisMode.FULL)
        results  = await analyzer.search_and_analyze("is:unread from:boss@corp.com")

    The ``mode`` parameter controls which analysis passes run and which fields
    of ``AnalysisResult`` are populated.
    """

    def __init__(
        self,
        settings: Settings,
        gmail:    GmailClient,
        ollama:   OllamaClient,
    ) -> None:
        self._settings       = settings
        self._gmail          = gmail
        self._ollama         = ollama
        self._troubleshooter = TroubleshootAnalyzer(ollama)

    async def analyze_thread(
        self,
        thread_id: str,
        mode: AnalysisMode = AnalysisMode.FULL,
    ) -> AnalysisResult:
        """
        Fetch and analyse a single thread by ID.

        Args:
            thread_id: Gmail thread ID.
            mode:      Controls which analysis passes run.

        Raises:
            GmailError:  On Gmail API errors.
            OllamaError: On LLM errors (only for modes that invoke the LLM).
        """
        log.info("Fetching thread %s (mode=%s)", thread_id, mode.value)
        raw    = await self._gmail.get_thread(thread_id)
        thread = ThreadParser.parse_thread(raw)
        return await self._run_analysis(thread, mode)

    async def search_and_analyze(
        self,
        query: str,
        mode: AnalysisMode = AnalysisMode.SUMMARIZE,
        max_results: int | None = None,
    ) -> list[AnalysisResult]:
        """
        Search for threads and analyse each one.

        Threads that fail to fetch or analyse are logged and skipped.

        Args:
            query:       Gmail search query string.
            mode:        Analysis mode applied to every thread.
            max_results: Override the default from ``Settings``.
        """
        log.info("Searching %r", query)
        stubs   = await self._gmail.search_threads(query, max_results=max_results)
        results: list[AnalysisResult] = []
        for stub in stubs:
            tid = stub.get("id", "")
            if not tid:
                continue
            try:
                results.append(await self.analyze_thread(tid, mode))
            except (GmailError, OllamaError) as exc:
                log.warning("Skipping thread %s: %s", tid, exc)
        return results

    async def _run_analysis(
        self, thread: ThreadSummary, mode: AnalysisMode
    ) -> AnalysisResult:
        result = AnalysisResult(
            mode          = mode,
            thread_id     = thread.thread_id,
            subject       = thread.subject,
            timestamp     = datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            message_count = len(thread.messages),
        )

        if mode in (AnalysisMode.SUMMARIZE, AnalysisMode.FULL):
            result.summary      = await self._summarize(thread)
            result.llm_response = result.summary

        if mode in (AnalysisMode.HEADERS, AnalysisMode.FULL):
            for msg in thread.messages:
                rep = HeaderAnalyzer.analyze(msg)
                result.header_report[msg.message_id] = rep
                result.warnings.extend(rep.get("warnings", []))

        if mode in (AnalysisMode.MIME, AnalysisMode.FULL):
            for msg in thread.messages:
                parts = MIMEAnalyzer.analyze(msg)
                result.mime_report.extend(parts)
                for p in parts:
                    result.warnings.extend(p.get("warnings", []))

        if mode in (AnalysisMode.TROUBLESHOOT, AnalysisMode.FULL):
            ts = await self._troubleshooter.analyze(thread)
            result.troubleshoot_report = ts
            result.warnings.extend(ts.get("automated_warnings", []))
            if mode == AnalysisMode.TROUBLESHOOT:
                result.llm_response = ts.get("llm_analysis", "")

        return result

    async def _summarize(self, thread: ThreadSummary) -> str:
        """Build a compact prompt and request a 4-6 sentence summary."""
        max_chars  = self._settings.gmail_max_body_chars
        body_parts = [
            f"[Message {i}] From: {msg.sender}  Date: {msg.date}\n"
            f"{msg.body_preview(max_chars) or msg.snippet}"
            for i, msg in enumerate(thread.messages[:5], 1)
        ]
        prompt = textwrap.dedent(f"""
            Summarise this email thread in 4-6 sentences.
            Include the main topic, key action items, and any decisions or deadlines.

            Subject: {thread.subject}
            Participants: {', '.join(thread.participants[:6])}
            Messages: {len(thread.messages)}

            {chr(10).join(body_parts)}
        """).strip()
        return await self._ollama.generate(prompt, system=_SYSTEM_ANALYST)
