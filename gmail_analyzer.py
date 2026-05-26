"""
Gmail Analyzer Skill — powered by Ollama (llama3.2:1b) + Gmail MCP
Analyzes, summarizes, and troubleshoots email conversations.
"""

from __future__ import annotations

import email
import email.policy
import json
import logging
import re
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header
from enum import Enum
from typing import Any

import httpx

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("gmail_analyzer")


# ─── Configuration ──────────────────────────────────────────────────────────

class Config:
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.2:1b"
    OLLAMA_TIMEOUT: float = 120.0          # seconds; 1b is fast but context varies
    OLLAMA_NUM_CTX: int = 4096             # context window — llama3.2:1b default
    OLLAMA_TEMPERATURE: float = 0.2        # low temp for analytical tasks
    MAX_EMAIL_BODY_CHARS: int = 6_000      # truncate very long bodies before send
    MCP_TIMEOUT: float = 30.0


# ─── Enums / constants ───────────────────────────────────────────────────────

class AnalysisMode(str, Enum):
    SUMMARIZE = "summarize"
    HEADERS = "headers"
    MIME = "mime"
    TROUBLESHOOT = "troubleshoot"
    FULL = "full"


KNOWN_SPAM_HEADERS = {"x-spam-status", "x-spam-flag", "x-spam-score"}
DMARC_HEADERS = {"dkim-signature", "authentication-results", "received-spf", "arc-seal"}
DELIVERY_HEADERS = {
    "received", "x-forwarded-to", "delivered-to", "x-original-to",
    "x-received", "x-google-smtp-source",
}


# ─── Data classes ───────────────────────────────────────────────────────────

@dataclass
class EmailMessage:
    message_id: str
    subject: str
    sender: str
    recipients: list[str]
    date: str
    body_plain: str
    body_html: str
    raw_headers: dict[str, list[str]]  # header_name -> [value, ...]
    mime_structure: list[dict[str, Any]]
    thread_id: str = ""
    labels: list[str] = field(default_factory=list)

    @property
    def body_preview(self) -> str:
        text = self.body_plain or re.sub(r"<[^>]+>", " ", self.body_html)
        text = re.sub(r"\s+", " ", text).strip()
        return text[: Config.MAX_EMAIL_BODY_CHARS]

    @property
    def delivery_path(self) -> list[str]:
        return self.raw_headers.get("received", [])

    @property
    def auth_results(self) -> dict[str, str]:
        results: dict[str, str] = {}
        for val in self.raw_headers.get("authentication-results", []):
            for part in val.split(";"):
                part = part.strip()
                if "=" in part:
                    k, _, v = part.partition("=")
                    results[k.strip().lower()] = v.strip().lower()
        return results


@dataclass
class AnalysisResult:
    mode: AnalysisMode
    message_id: str
    subject: str
    timestamp: str
    summary: str = ""
    header_report: dict[str, Any] = field(default_factory=dict)
    mime_report: list[dict[str, Any]] = field(default_factory=list)
    troubleshoot_report: dict[str, Any] = field(default_factory=dict)
    llm_response: str = ""
    warnings: list[str] = field(default_factory=list)


# ─── Gmail MCP Client ────────────────────────────────────────────────────────

class GmailMCPError(Exception):
    """Raised when the Gmail MCP returns an error or unexpected payload."""


class GmailMCPClient:
    """
    Thin async wrapper around the Gmail MCP server.

    The MCP server exposes JSON-RPC 2.0 over HTTP (default port 3000).
    Adjust `base_url` via env var GMAIL_MCP_URL if your setup differs.
    """

    def __init__(self, base_url: str = "http://localhost:3000") -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=Config.MCP_TIMEOUT)
        self._rpc_id = 0

    # ── low-level RPC ────────────────────────────────────────────────────────

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": method,
            "params": params,
        }
        try:
            response = await self._client.post(
                f"{self.base_url}/rpc", json=payload
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise GmailMCPError(f"MCP request timed out: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise GmailMCPError(
                f"MCP HTTP error {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise GmailMCPError(f"MCP connection error: {exc}") from exc

        data = response.json()
        if "error" in data:
            raise GmailMCPError(f"MCP error {data['error']['code']}: {data['error']['message']}")
        return data.get("result")

    # ── public helpers ───────────────────────────────────────────────────────

    async def list_messages(
        self,
        query: str = "",
        max_results: int = 10,
        label_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return a list of message stubs {id, threadId}."""
        params: dict[str, Any] = {"maxResults": max_results}
        if query:
            params["q"] = query
        if label_ids:
            params["labelIds"] = label_ids
        result = await self._rpc("gmail.messages.list", params)
        return result.get("messages", [])

    async def get_raw_message(self, message_id: str) -> dict[str, Any]:
        """Fetch the full RFC-822 message as a Gmail API payload."""
        result = await self._rpc(
            "gmail.messages.get",
            {"id": message_id, "format": "full"},
        )
        if not result:
            raise GmailMCPError(f"Empty response for message {message_id!r}")
        return result

    async def get_thread(self, thread_id: str) -> list[dict[str, Any]]:
        """Return all messages in a thread."""
        result = await self._rpc("gmail.threads.get", {"id": thread_id, "format": "full"})
        return result.get("messages", [])

    async def close(self) -> None:
        await self._client.aclose()


# ─── Email parser ────────────────────────────────────────────────────────────

class EmailParser:
    """Convert raw Gmail API payloads → EmailMessage dataclass."""

    @staticmethod
    def _decode_header_value(raw: str) -> str:
        parts = decode_header(raw)
        decoded = []
        for chunk, charset in parts:
            if isinstance(chunk, bytes):
                try:
                    decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    decoded.append(chunk.decode("latin-1", errors="replace"))
            else:
                decoded.append(chunk)
        return "".join(decoded)

    @staticmethod
    def _collect_headers(payload: dict[str, Any]) -> dict[str, list[str]]:
        """Return {lowercase_name: [value, ...]} from Gmail headers list."""
        headers: dict[str, list[str]] = {}
        for h in payload.get("headers", []):
            name = h.get("name", "").lower()
            value = h.get("value", "")
            headers.setdefault(name, []).append(value)
        return headers

    @classmethod
    def _extract_body(cls, payload: dict[str, Any]) -> tuple[str, str]:
        """Recursively extract (plain_text, html_text) from Gmail payload parts."""
        plain, html = "", ""
        mime_type = payload.get("mimeType", "")

        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                import base64
                plain = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        elif mime_type == "text/html":
            data = payload.get("body", {}).get("data", "")
            if data:
                import base64
                html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        else:
            for part in payload.get("parts", []):
                p, h = cls._extract_body(part)
                plain = plain or p
                html = html or h

        return plain, html

    @classmethod
    def _map_mime_structure(cls, payload: dict[str, Any], depth: int = 0) -> list[dict[str, Any]]:
        node: dict[str, Any] = {
            "depth": depth,
            "mimeType": payload.get("mimeType", "unknown"),
            "filename": payload.get("filename", ""),
            "size": payload.get("body", {}).get("size", 0),
            "headers": cls._collect_headers(payload),
        }
        children = [
            cls._map_mime_structure(p, depth + 1)
            for p in payload.get("parts", [])
        ]
        node["children"] = [item for sublist in children for item in sublist]
        return [node]

    @classmethod
    def parse(cls, gmail_payload: dict[str, Any]) -> EmailMessage:
        headers = cls._collect_headers(gmail_payload.get("payload", {}))
        plain, html = cls._extract_body(gmail_payload.get("payload", {}))
        mime_structure = cls._map_mime_structure(gmail_payload.get("payload", {}))

        def h(name: str) -> str:
            vals = headers.get(name, [""])
            return cls._decode_header_value(vals[0]) if vals else ""

        recipients = [
            r.strip()
            for r in re.split(r",\s*", h("to") + "," + h("cc"))
            if r.strip()
        ]

        return EmailMessage(
            message_id=gmail_payload.get("id", ""),
            thread_id=gmail_payload.get("threadId", ""),
            subject=h("subject") or "(no subject)",
            sender=h("from"),
            recipients=recipients,
            date=h("date"),
            body_plain=plain,
            body_html=html,
            raw_headers=headers,
            mime_structure=mime_structure,
            labels=gmail_payload.get("labelIds", []),
        )


# ─── Ollama client ────────────────────────────────────────────────────────────

class OllamaError(Exception):
    """Raised on Ollama connectivity or API errors."""


class OllamaClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=Config.OLLAMA_TIMEOUT)

    async def _check_model_available(self) -> None:
        try:
            resp = await self._client.get(f"{Config.OLLAMA_BASE_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            # accept both "llama3.2:1b" and "llama3.2:1b-instruct-q4_0" etc.
            if not any(Config.OLLAMA_MODEL.split(":")[0] in m for m in models):
                raise OllamaError(
                    f"Model {Config.OLLAMA_MODEL!r} not found in Ollama. "
                    f"Run: ollama pull {Config.OLLAMA_MODEL}"
                )
        except httpx.RequestError as exc:
            raise OllamaError(
                f"Cannot reach Ollama at {Config.OLLAMA_BASE_URL}. "
                "Is it running?  →  ollama serve"
            ) from exc

    async def generate(self, prompt: str, system: str = "") -> str:
        await self._check_model_available()
        body: dict[str, Any] = {
            "model": Config.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": Config.OLLAMA_TEMPERATURE,
                "num_ctx": Config.OLLAMA_NUM_CTX,
            },
        }
        if system:
            body["system"] = system

        try:
            resp = await self._client.post(
                f"{Config.OLLAMA_BASE_URL}/api/generate", json=body
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise OllamaError(
                f"Ollama timed out after {Config.OLLAMA_TIMEOUT}s. "
                "Try raising OLLAMA_TIMEOUT or using a shorter prompt."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaError(f"Ollama HTTP {exc.response.status_code}: {exc.response.text}") from exc

        return resp.json().get("response", "").strip()

    async def close(self) -> None:
        await self._client.aclose()


# ─── Analyzers ────────────────────────────────────────────────────────────────

SYSTEM_ANALYST = textwrap.dedent("""
    You are an expert email analyst and deliverability engineer.
    Be concise, technical where needed, and structure your answers clearly.
    Do not hallucinate header values — work only from data provided.
""").strip()


class HeaderAnalyzer:
    """Pure-Python header analysis — no LLM required for structured checks."""

    @staticmethod
    def analyze(msg: EmailMessage) -> dict[str, Any]:
        headers = msg.raw_headers
        warnings: list[str] = []
        info: dict[str, Any] = {}

        # ── authentication ───────────────────────────────────────────────────
        auth = msg.auth_results
        info["authentication"] = {
            "dkim": auth.get("dkim", "absent"),
            "spf": auth.get("spf", "absent"),
            "dmarc": auth.get("dmarc", "absent"),
            "arc": "present" if "arc-seal" in headers else "absent",
        }
        for protocol in ("dkim", "spf", "dmarc"):
            val = auth.get(protocol, "absent")
            if val not in ("pass", "absent"):
                warnings.append(f"{protocol.upper()} check: {val!r}")

        # ── spam indicators ──────────────────────────────────────────────────
        spam_info: dict[str, str] = {}
        for sh in KNOWN_SPAM_HEADERS:
            if sh in headers:
                spam_info[sh] = headers[sh][0]
        info["spam_headers"] = spam_info
        if spam_info.get("x-spam-flag", "").upper() == "YES":
            warnings.append("Message flagged as spam (X-Spam-Flag: YES)")

        # ── delivery hops ────────────────────────────────────────────────────
        received = msg.delivery_path
        info["delivery_hops"] = len(received)
        if len(received) > 8:
            warnings.append(f"Unusually long delivery path ({len(received)} hops)")

        # ── date sanity ──────────────────────────────────────────────────────
        info["date"] = msg.date
        try:
            parsed_date = email.utils.parsedate_to_datetime(msg.date)
            delta = abs((datetime.now(parsed_date.tzinfo) - parsed_date).total_seconds())
            if delta > 86_400 * 2:
                warnings.append(f"Message date is more than 2 days off from now ({msg.date})")
        except Exception:
            warnings.append(f"Could not parse Date header: {msg.date!r}")

        # ── list headers (newsletters / mailing lists) ────────────────────
        list_headers = {k: v[0] for k, v in headers.items() if k.startswith("list-")}
        info["list_headers"] = list_headers

        # ── reply-to mismatch ────────────────────────────────────────────────
        reply_to = headers.get("reply-to", [""])[0]
        if reply_to and reply_to != msg.sender:
            warnings.append(f"Reply-To ({reply_to}) differs from From ({msg.sender})")

        info["warnings"] = warnings
        return info


class MIMEAnalyzer:
    """Analyze MIME structure for part types, attachments, and anomalies."""

    @staticmethod
    def analyze(msg: EmailMessage) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        warnings: list[str] = []

        def walk(nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                entry: dict[str, Any] = {
                    "mime_type": node["mimeType"],
                    "depth": node["depth"],
                    "size_bytes": node["size"],
                    "filename": node.get("filename") or None,
                    "content_disposition": node["headers"].get("content-disposition", [""])[0],
                    "content_id": node["headers"].get("content-id", [""])[0],
                    "is_attachment": bool(node.get("filename")),
                    "warnings": [],
                }

                # flag suspicious executable MIME types
                risky_types = {
                    "application/x-msdownload", "application/x-executable",
                    "application/x-sh", "application/javascript",
                }
                if node["mimeType"] in risky_types:
                    w = f"Potentially risky attachment type: {node['mimeType']!r}"
                    entry["warnings"].append(w)
                    warnings.append(w)

                # deeply nested structure can indicate obfuscation
                if node["depth"] > 5:
                    w = f"Deeply nested MIME part at depth {node['depth']} ({node['mimeType']})"
                    entry["warnings"].append(w)

                report.append(entry)
                if node.get("children"):
                    walk(node["children"])

        walk(msg.mime_structure)
        return report


class TroubleshootAnalyzer:
    """Heuristic + LLM-backed email deliverability troubleshooter."""

    def __init__(self, ollama: OllamaClient) -> None:
        self._ollama = ollama

    async def analyze(self, msg: EmailMessage) -> dict[str, Any]:
        header_report = HeaderAnalyzer.analyze(msg)
        mime_report = MIMEAnalyzer.analyze(msg)
        all_warnings = list(header_report.get("warnings", []))
        for m in mime_report:
            all_warnings.extend(m.get("warnings", []))

        # Build a focused prompt with the most relevant data
        prompt = textwrap.dedent(f"""
            Analyze this email for potential delivery or security issues.

            Subject: {msg.subject}
            From: {msg.sender}
            Date: {msg.date}
            Labels: {', '.join(msg.labels) or 'none'}

            Authentication:
            {json.dumps(header_report.get('authentication', {}), indent=2)}

            Delivery hops: {header_report.get('delivery_hops', 0)}

            Spam headers: {json.dumps(header_report.get('spam_headers', {}), indent=2)}

            Reply-To vs From: {header_report.get('warnings', [])}

            MIME structure summary:
            {json.dumps([{"type": m["mime_type"], "attachment": m["is_attachment"],
                          "size": m["size_bytes"]} for m in mime_report], indent=2)}

            Automated warnings already raised:
            {chr(10).join(f'- {w}' for w in all_warnings) or '(none)'}

            Provide a prioritised list of issues and recommended fixes.
        """).strip()

        llm_analysis = await self._ollama.generate(prompt, system=SYSTEM_ANALYST)

        return {
            "header_report": header_report,
            "mime_report": mime_report,
            "automated_warnings": all_warnings,
            "llm_analysis": llm_analysis,
        }


# ─── Core orchestrator ───────────────────────────────────────────────────────

class GmailAnalyzer:
    """
    Main entry point.  Wire together MCP → parser → analyzer → Ollama.
    """

    def __init__(
        self,
        mcp: GmailMCPClient,
        ollama: OllamaClient,
    ) -> None:
        self._mcp = mcp
        self._ollama = ollama
        self._troubleshooter = TroubleshootAnalyzer(ollama)

    # ── public API ───────────────────────────────────────────────────────────

    async def analyze_message(
        self,
        message_id: str,
        mode: AnalysisMode = AnalysisMode.FULL,
    ) -> AnalysisResult:
        log.info("Fetching message %s (mode=%s)", message_id, mode.value)
        raw = await self._mcp.get_raw_message(message_id)
        msg = EmailParser.parse(raw)
        return await self._run_analysis(msg, mode)

    async def analyze_thread(
        self,
        thread_id: str,
        mode: AnalysisMode = AnalysisMode.SUMMARIZE,
    ) -> list[AnalysisResult]:
        log.info("Fetching thread %s", thread_id)
        messages = await self._mcp.get_thread(thread_id)
        results: list[AnalysisResult] = []
        for raw in messages:
            msg = EmailParser.parse(raw)
            results.append(await self._run_analysis(msg, mode))
        return results

    async def search_and_analyze(
        self,
        query: str,
        mode: AnalysisMode = AnalysisMode.SUMMARIZE,
        max_results: int = 5,
    ) -> list[AnalysisResult]:
        log.info("Searching for %r (max=%d)", query, max_results)
        stubs = await self._mcp.list_messages(query=query, max_results=max_results)
        results: list[AnalysisResult] = []
        for stub in stubs:
            try:
                result = await self.analyze_message(stub["id"], mode)
                results.append(result)
            except (GmailMCPError, OllamaError) as exc:
                log.warning("Skipping message %s: %s", stub["id"], exc)
        return results

    # ── private ─────────────────────────────────────────────────────────────

    async def _run_analysis(
        self, msg: EmailMessage, mode: AnalysisMode
    ) -> AnalysisResult:
        result = AnalysisResult(
            mode=mode,
            message_id=msg.message_id,
            subject=msg.subject,
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )

        if mode in (AnalysisMode.SUMMARIZE, AnalysisMode.FULL):
            result.summary = await self._summarize(msg)
            result.llm_response = result.summary

        if mode in (AnalysisMode.HEADERS, AnalysisMode.FULL):
            result.header_report = HeaderAnalyzer.analyze(msg)
            result.warnings.extend(result.header_report.get("warnings", []))

        if mode in (AnalysisMode.MIME, AnalysisMode.FULL):
            result.mime_report = MIMEAnalyzer.analyze(msg)
            for m in result.mime_report:
                result.warnings.extend(m.get("warnings", []))

        if mode in (AnalysisMode.TROUBLESHOOT, AnalysisMode.FULL):
            ts = await self._troubleshooter.analyze(msg)
            result.troubleshoot_report = ts
            result.warnings.extend(ts.get("automated_warnings", []))
            if mode == AnalysisMode.TROUBLESHOOT:
                result.llm_response = ts.get("llm_analysis", "")

        return result

    async def _summarize(self, msg: EmailMessage) -> str:
        prompt = textwrap.dedent(f"""
            Summarise the following email in 3-5 sentences.
            Include the main topic, key action items, and any deadlines mentioned.

            Subject: {msg.subject}
            From: {msg.sender}
            To: {', '.join(msg.recipients[:3])}
            Date: {msg.date}

            Body:
            {msg.body_preview}
        """).strip()
        return await self._ollama.generate(prompt, system=SYSTEM_ANALYST)


# ─── CLI ─────────────────────────────────────────────────────────────────────

async def _cli_main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Analyze Gmail messages via Ollama (llama3.2:1b)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search sub-command
    search_cmd = sub.add_parser("search", help="Search and analyze messages")
    search_cmd.add_argument("query", help="Gmail search query, e.g. 'from:boss@example.com'")
    search_cmd.add_argument("--max", type=int, default=5)
    search_cmd.add_argument(
        "--mode",
        choices=[m.value for m in AnalysisMode],
        default=AnalysisMode.SUMMARIZE.value,
    )

    # message sub-command
    msg_cmd = sub.add_parser("message", help="Analyze a single message by ID")
    msg_cmd.add_argument("message_id")
    msg_cmd.add_argument(
        "--mode",
        choices=[m.value for m in AnalysisMode],
        default=AnalysisMode.FULL.value,
    )

    # thread sub-command
    thread_cmd = sub.add_parser("thread", help="Analyze a full thread")
    thread_cmd.add_argument("thread_id")
    thread_cmd.add_argument(
        "--mode",
        choices=[m.value for m in AnalysisMode],
        default=AnalysisMode.SUMMARIZE.value,
    )

    args = parser.parse_args(argv)

    mcp = GmailMCPClient()
    ollama = OllamaClient()
    analyzer = GmailAnalyzer(mcp, ollama)

    try:
        if args.command == "search":
            results = await analyzer.search_and_analyze(
                args.query,
                mode=AnalysisMode(args.mode),
                max_results=args.max,
            )
            for r in results:
                _print_result(r)

        elif args.command == "message":
            result = await analyzer.analyze_message(
                args.message_id, mode=AnalysisMode(args.mode)
            )
            _print_result(result)

        elif args.command == "thread":
            results = await analyzer.analyze_thread(
                args.thread_id, mode=AnalysisMode(args.mode)
            )
            for r in results:
                _print_result(r)

    except GmailMCPError as exc:
        log.error("Gmail MCP error: %s", exc)
        return 1
    except OllamaError as exc:
        log.error("Ollama error: %s", exc)
        return 1
    finally:
        await mcp.close()
        await ollama.close()

    return 0


def _print_result(result: AnalysisResult) -> None:
    sep = "─" * 60
    print(f"\n{sep}")
    print(f"  {result.subject}")
    print(f"  Message ID : {result.message_id}")
    print(f"  Mode       : {result.mode.value}")
    print(f"  Analyzed   : {result.timestamp}")
    print(sep)

    if result.summary:
        print("\n📧 Summary\n")
        print(textwrap.indent(result.summary, "  "))

    if result.header_report:
        auth = result.header_report.get("authentication", {})
        print("\n🔐 Authentication\n")
        for k, v in auth.items():
            icon = "✅" if v == "pass" else ("⚠️ " if v == "absent" else "❌")
            print(f"  {icon} {k.upper()}: {v}")

    if result.mime_report:
        print(f"\n📎 MIME structure ({len(result.mime_report)} parts)\n")
        for part in result.mime_report:
            indent = "  " * (part["depth"] + 1)
            attachment = " [attachment]" if part["is_attachment"] else ""
            print(f"{indent}{part['mime_type']}{attachment} ({part['size_bytes']} bytes)")

    if result.troubleshoot_report and result.troubleshoot_report.get("llm_analysis"):
        print("\n🔧 Troubleshooting\n")
        print(textwrap.indent(result.troubleshoot_report["llm_analysis"], "  "))

    if result.warnings:
        print("\n⚠️  Warnings\n")
        for w in result.warnings:
            print(f"  • {w}")


def main() -> None:
    import asyncio
    sys.exit(asyncio.run(_cli_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
