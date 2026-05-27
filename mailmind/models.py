"""
mailmind.models — shared domain types.

Contains enums, exceptions, and dataclasses that are imported by multiple
modules.  Keeping them here avoids circular imports between the higher-level
modules (gmail, analysis, service).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────


class AnalysisMode(str, Enum):
    """Controls which analysis passes ``GmailAnalyzer`` runs for a thread."""

    SUMMARIZE    = "summarize"
    HEADERS      = "headers"
    MIME         = "mime"
    TROUBLESHOOT = "troubleshoot"
    FULL         = "full"


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────


class GmailError(Exception):
    """Raised for Gmail API HTTP errors, connection failures, or bad responses."""


class OllamaError(Exception):
    """Raised when Ollama is unreachable, returns an error, or times out."""


class OAuthError(Exception):
    """Raised for missing, expired, or unrefreshable OAuth tokens."""


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class MessageSummary:
    """
    Normalised view of a single Gmail message, populated by ``ThreadParser``.

    ``body`` and ``raw_headers`` are only non-empty when the message was
    fetched with ``format=full``.
    """

    message_id: str
    thread_id: str
    subject: str
    sender: str
    recipients: list[str]
    date: str
    snippet: str
    labels: list[str]
    body: str = ""
    raw_headers: dict[str, str] = field(default_factory=dict)
    mime_type: str = ""
    attachment_filenames: list[str] = field(default_factory=list)

    def body_preview(self, max_chars: int) -> str:
        """Return ``body`` truncated to ``max_chars`` characters."""
        return self.body[:max_chars]


@dataclass
class ThreadSummary:
    """
    A Gmail thread: an ordered list of ``MessageSummary`` objects.

    ``subject`` and ``participants`` are computed lazily from the message list.
    """

    thread_id: str
    messages: list[MessageSummary]

    @property
    def subject(self) -> str:
        """Subject line of the first message, or a placeholder for empty threads."""
        return self.messages[0].subject if self.messages else "(empty thread)"

    @property
    def participants(self) -> list[str]:
        """
        Ordered, deduplicated list of all senders and recipients in the thread.

        Insertion order is preserved so the original sender appears first.
        """
        seen: set[str] = set()
        out: list[str] = []
        for msg in self.messages:
            for addr in [msg.sender, *msg.recipients]:
                if addr and addr not in seen:
                    seen.add(addr)
                    out.append(addr)
        return out


@dataclass
class AnalysisResult:
    """
    Output of one ``GmailAnalyzer`` analysis run.

    Which fields are populated depends on the ``AnalysisMode``:

    =========== ======== ====== ====== ============= ========
    Mode        summary  header  mime  troubleshoot  warnings
    =========== ======== ====== ====== ============= ========
    SUMMARIZE   ✓                                    ✓
    HEADERS              ✓                           ✓
    MIME                        ✓                   ✓
    TROUBLESHOOT                        ✓            ✓
    FULL         ✓       ✓      ✓       ✓            ✓
    =========== ======== ====== ====== ============= ========
    """

    mode: AnalysisMode
    thread_id: str
    subject: str
    timestamp: str
    message_count: int = 0
    summary: str = ""
    header_report: dict[str, Any] = field(default_factory=dict)
    mime_report: list[dict[str, Any]] = field(default_factory=list)
    troubleshoot_report: dict[str, Any] = field(default_factory=dict)
    llm_response: str = ""
    warnings: list[str] = field(default_factory=list)
