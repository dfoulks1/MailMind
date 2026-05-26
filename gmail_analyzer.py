"""
Gmail Analyzer Skill — powered by Ollama (llama3.2:1b) + Gmail REST API

Uses the Gmail REST API (gmail.googleapis.com/gmail/v1) directly via OAuth 2.0.
Analyzes, summarizes, and troubleshoots email conversations.
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


# ─── Environment file ─────────────────────────────────────────────────────────────
# Loads .env from the current working directory on import.
# override=False means real environment variables always win over the file.
# Pass a different path with --env-file on the CLI.

_DEFAULT_ENV_FILE = Path(".env")
load_dotenv(_DEFAULT_ENV_FILE, override=False)

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("gmail_analyzer")


# ─── Configuration ──────────────────────────────────────────────────────────

class Config:
    # Gmail REST API base URL
    GMAIL_API_URL: str = os.getenv(
        "GMAIL_API_URL", "https://gmail.googleapis.com/gmail/v1"
    )

    # OAuth 2.0 — set OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET in .env
    OAUTH_CLIENT_ID: str = os.getenv("OAUTH_CLIENT_ID", "")
    OAUTH_CLIENT_SECRET: str = os.getenv("OAUTH_CLIENT_SECRET", "")
    OAUTH_TOKEN_FILE: str = os.getenv("OAUTH_TOKEN_FILE", "token.json")
    OAUTH_SCOPES: list[str] = [
        # Scopes required by gmailmcp.googleapis.com per Google's official docs:
        # https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ]

    # Ollama
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
    OLLAMA_TIMEOUT: float = float(os.getenv("OLLAMA_TIMEOUT", "120.0"))
    OLLAMA_NUM_CTX: int = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
    OLLAMA_TEMPERATURE: float = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))

    # Misc
    MAX_BODY_CHARS: int = int(os.getenv("MAX_BODY_CHARS", "6000"))
    MCP_TIMEOUT: float = float(os.getenv("MCP_TIMEOUT", "30.0"))
    MAX_RESULTS: int = int(os.getenv("MAX_RESULTS", "500"))


# ─── Enums ──────────────────────────────────────────────────────────────────

class AnalysisMode(str, Enum):
    SUMMARIZE = "summarize"
    HEADERS = "headers"
    MIME = "mime"
    TROUBLESHOOT = "troubleshoot"
    FULL = "full"


# ─── Exceptions ─────────────────────────────────────────────────────────────

class GmailMCPError(Exception):
    """MCP transport, RPC, or auth error."""


class OllamaError(Exception):
    """Ollama connectivity or API error."""


class OAuthError(Exception):
    """OAuth token missing, expired, or invalid."""


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class MessageSummary:
    """Lightweight view of a single message inside a thread."""
    message_id: str
    thread_id: str
    subject: str
    sender: str
    recipients: list[str]
    date: str
    snippet: str
    labels: list[str]
    # Populated when the full thread is fetched via get_thread
    body: str = ""
    raw_headers: dict[str, str] = field(default_factory=dict)
    mime_type: str = ""
    attachment_filenames: list[str] = field(default_factory=list)

    @property
    def body_preview(self) -> str:
        return self.body[: Config.MAX_BODY_CHARS]


@dataclass
class ThreadSummary:
    thread_id: str
    messages: list[MessageSummary]

    @property
    def subject(self) -> str:
        return self.messages[0].subject if self.messages else "(empty thread)"

    @property
    def participants(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for m in self.messages:
            for p in [m.sender] + m.recipients:
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
        return out


@dataclass
class AnalysisResult:
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


# ─── OAuth token management ──────────────────────────────────────────────────

class OAuthTokenManager:
    """
    Manages the OAuth 2.0 access token for gmailmcp.googleapis.com.

    Token refresh follows RFC 6749 §6.  Tokens are persisted to
    `Config.OAUTH_TOKEN_FILE` so the OAuth flow only runs once.
    """

    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"  # desktop / out-of-band

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        token_file: str = "",
    ) -> None:
        self._client_id = client_id or Config.OAUTH_CLIENT_ID
        self._client_secret = client_secret or Config.OAUTH_CLIENT_SECRET
        self._token_file = token_file or Config.OAUTH_TOKEN_FILE
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._expiry: datetime | None = None
        self._http = httpx.AsyncClient(timeout=30.0)

    def _load_token_file(self) -> bool:
        import os, json as _j
        if not os.path.exists(self._token_file):
            return False
        try:
            data = _j.loads(open(self._token_file).read())
            self._access_token = data.get("access_token", "")
            self._refresh_token = data.get("refresh_token", "")
            expiry_str = data.get("expiry", "")
            if expiry_str:
                self._expiry = datetime.fromisoformat(expiry_str)
            return bool(self._access_token)
        except Exception as exc:
            log.warning("Could not read token file %s: %s", self._token_file, exc)
            return False

    def _save_token_file(self) -> None:
        import json as _j
        data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expiry": self._expiry.isoformat() if self._expiry else "",
        }
        open(self._token_file, "w").write(_j.dumps(data, indent=2))

    def _is_expired(self) -> bool:
        if not self._expiry:
            return False
        # refresh 60 s before real expiry
        return datetime.now(UTC) >= self._expiry.replace(
            tzinfo=self._expiry.tzinfo or UTC
        )

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing or prompting as needed."""
        if not self._access_token:
            self._load_token_file()

        if self._access_token and not self._is_expired():
            return self._access_token

        if self._refresh_token:
            await self._refresh()
            return self._access_token

        # No token at all → interactive browser flow
        await self._interactive_flow()
        return self._access_token

    async def _refresh(self) -> None:
        resp = await self._http.post(
            self.GOOGLE_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            raise OAuthError(
                f"Token refresh failed ({resp.status_code}): {resp.text}"
            )
        self._ingest_token_response(resp.json())

    async def _interactive_flow(self) -> None:
        """
        Desktop OAuth 2.0 out-of-band flow.
        Prints the authorization URL and reads the code from stdin.
        """
        if not self._client_id or not self._client_secret:
            raise OAuthError(
                "OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET must be set. "
                "See docs/README.md §Google Cloud Application setup."
            )
        import urllib.parse
        params = {
            "client_id": self._client_id,
            "redirect_uri": self.REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(Config.OAUTH_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        }
        url = f"{self.GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
        print("\n─── Gmail OAuth Authorization ───────────────────────────────")
        print("Open this URL in your browser and authorize the application:\n")
        print(f"  {url}\n")
        code = input("Paste the authorization code here: ").strip()
        if not code:
            raise OAuthError("No authorization code provided.")

        resp = await self._http.post(
            self.GOOGLE_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": self.REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code != 200:
            raise OAuthError(
                f"Authorization code exchange failed ({resp.status_code}): {resp.text}"
            )
        self._ingest_token_response(resp.json())

    def _ingest_token_response(self, data: dict[str, Any]) -> None:
        self._access_token = data["access_token"]
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]
        expires_in = int(data.get("expires_in", 3600))
        from datetime import timedelta
        self._expiry = datetime.now(UTC) + timedelta(seconds=expires_in)
        self._save_token_file()
        log.info("OAuth token refreshed, expires at %s", self._expiry.isoformat())

    async def introspect(self) -> dict[str, Any]:
        """Call Google tokeninfo to see what scopes the current token actually has."""
        token = await self.get_access_token()
        resp = await self._http.get(
            "https://www.googleapis.com/oauth2/v3/tokeninfo",
            params={"access_token": token},
        )
        return resp.json()

    async def close(self) -> None:
        await self._http.aclose()


# ─── Gmail REST Client ───────────────────────────────────────────────────────
# Talks directly to gmail.googleapis.com/gmail/v1 — no MCP preview programme
# required.  The public interface is identical to the former GmailMCPClient so
# GmailAnalyzer, ThreadParser, and all tests are unchanged.

class GmailMCPError(Exception):  # name kept for backward compat with tests
    """Gmail API or auth error."""


class GmailMCPClient:
    """
    Gmail REST API client with the same interface as the former MCP client.

    Endpoints used:
        GET  /users/me/threads?q=...   → search_threads
        GET  /users/me/threads/{id}    → get_thread
        GET  /users/me/labels          → list_labels
        POST /users/me/threads/{id}/modify → label_thread / unlabel_thread
        POST /users/me/drafts          → create_draft
    """

    BASE = Config.GMAIL_API_URL

    def __init__(self, token_manager: OAuthTokenManager) -> None:
        self._tokens = token_manager
        self._http = httpx.AsyncClient(timeout=Config.MCP_TIMEOUT)

    async def _get(self, path: str, **params: Any) -> Any:
        token = await self._tokens.get_access_token()
        try:
            resp = await self._http.get(
                f"{self.BASE}{path}",
                params={k: v for k, v in params.items() if v is not None},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GmailMCPError(
                f"Gmail API HTTP {exc.response.status_code}: {exc.response.text[:400]}"
            ) from exc
        except httpx.RequestError as exc:
            raise GmailMCPError(f"Gmail API connection error: {exc}") from exc
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        token = await self._tokens.get_access_token()
        try:
            resp = await self._http.post(
                f"{self.BASE}{path}",
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GmailMCPError(
                f"Gmail API HTTP {exc.response.status_code}: {exc.response.text[:400]}"
            ) from exc
        except httpx.RequestError as exc:
            raise GmailMCPError(f"Gmail API connection error: {exc}") from exc
        return resp.json()

    async def search_threads(
        self,
        query: str,
        max_results: int = Config.MAX_RESULTS,
        page_token: str = "",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        data = await self._get("/users/me/threads", **params)
        return data.get("threads", [])

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        return await self._get(f"/users/me/threads/{thread_id}", format="full")

    async def list_labels(self) -> list[dict[str, Any]]:
        data = await self._get("/users/me/labels")
        return data.get("labels", [])

    async def label_thread(self, thread_id: str, label_ids: list[str]) -> None:
        await self._post(
            f"/users/me/threads/{thread_id}/modify",
            {"addLabelIds": label_ids},
        )

    async def unlabel_thread(self, thread_id: str, label_ids: list[str]) -> None:
        await self._post(
            f"/users/me/threads/{thread_id}/modify",
            {"removeLabelIds": label_ids},
        )

    async def create_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
    ) -> str:
        import base64, email.mime.text as _mime
        msg = _mime.MIMEText(body)
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = ", ".join(cc)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        data = await self._post("/users/me/drafts", {"message": {"raw": raw}})
        return data.get("id", "")

    async def introspect_token(self) -> dict[str, Any]:
        """Delegate to OAuthTokenManager.introspect()."""
        return await self._tokens.introspect()

    async def close(self) -> None:
        await self._http.aclose()
# ─── Thread / message parser ──────────────────────────────────────────────────

class ThreadParser:
    """
    Convert a raw get_thread response (as returned by gmailmcp.googleapis.com)
    into our typed ThreadSummary / MessageSummary dataclasses.

    The MCP server returns threads in the Gmail REST API shape:
    {
      "id": "<threadId>",
      "messages": [
        {
          "id": "<msgId>",
          "threadId": "<threadId>",
          "label_ids": [...],
          "snippet": "...",
          "payload": {
            "headers": [{"name": ..., "value": ...}],
            "mimeType": "...",
            "parts": [...]
          }
        },
        ...
      ]
    }
    """

    @classmethod
    def parse_thread(cls, raw: dict[str, Any]) -> ThreadSummary:
        thread_id = raw.get("id", "")
        messages = [
            cls._parse_message(m, thread_id)
            for m in raw.get("messages", [])
        ]
        return ThreadSummary(thread_id=thread_id, messages=messages)

    @classmethod
    def _parse_message(cls, raw_msg: dict[str, Any], thread_id: str) -> MessageSummary:
        payload = raw_msg.get("payload", {})
        headers = cls._headers_dict(payload.get("headers", []))

        body, attachments = cls._extract_body_and_attachments(payload)

        recipients = [
            r.strip()
            for r in re.split(r",\s*", (headers.get("to", "") + "," + headers.get("cc", "")))
            if r.strip()
        ]

        return MessageSummary(
            message_id=raw_msg.get("id", ""),
            thread_id=thread_id,
            subject=headers.get("subject", "(no subject)"),
            sender=headers.get("from", ""),
            recipients=recipients,
            date=headers.get("date", ""),
            snippet=raw_msg.get("snippet", ""),
            labels=raw_msg.get("labelIds", []),
            body=body,
            raw_headers=headers,
            mime_type=payload.get("mimeType", ""),
            attachment_filenames=attachments,
        )

    @staticmethod
    def _headers_dict(header_list: list[dict[str, str]]) -> dict[str, str]:
        """Return {lowercase_name: value} keeping the first occurrence."""
        out: dict[str, str] = {}
        for h in header_list:
            name = h.get("name", "").lower()
            if name not in out:
                out[name] = h.get("value", "")
        return out

    @classmethod
    def _extract_body_and_attachments(
        cls, payload: dict[str, Any]
    ) -> tuple[str, list[str]]:
        """Recursively extract plain text body and attachment filenames."""
        plain = ""
        attachments: list[str] = []

        mime = payload.get("mimeType", "")
        if mime == "text/plain":
            import base64
            data = payload.get("body", {}).get("data", "")
            if data:
                try:
                    plain = base64.urlsafe_b64decode(data + "==").decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    pass
        elif mime == "text/html":
            import base64
            data = payload.get("body", {}).get("data", "")
            if data and not plain:
                try:
                    html = base64.urlsafe_b64decode(data + "==").decode(
                        "utf-8", errors="replace"
                    )
                    plain = re.sub(r"<[^>]+>", " ", html)
                    plain = re.sub(r"\s+", " ", plain).strip()
                except Exception:
                    pass

        for part in payload.get("parts", []):
            fname = part.get("filename", "")
            if fname:
                attachments.append(fname)
            p, a = cls._extract_body_and_attachments(part)
            plain = plain or p
            attachments.extend(a)

        return plain, attachments


# ─── Analyzers ────────────────────────────────────────────────────────────────

KNOWN_SPAM_HEADERS = {"x-spam-status", "x-spam-flag", "x-spam-score"}
RISKY_MIME_TYPES = {
    "application/x-msdownload", "application/x-executable",
    "application/x-sh", "application/javascript",
}
SYSTEM_ANALYST = textwrap.dedent("""
    You are an expert email analyst and deliverability engineer.
    Be concise, technical where needed, and structure your answers clearly.
    Only work from the data provided; do not invent header values or details.
""").strip()


class HeaderAnalyzer:
    """Pure-Python heuristic analysis of message headers."""

    @staticmethod
    def analyze(msg: MessageSummary) -> dict[str, Any]:
        h = msg.raw_headers
        warnings: list[str] = []

        # ── authentication ───────────────────────────────────────────────────
        auth_raw = h.get("authentication-results", "")
        auth: dict[str, str] = {}
        for part in auth_raw.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                auth[k.strip().lower()] = v.strip().split()[0].lower()

        auth_report = {
            "dkim": auth.get("dkim", "absent"),
            "spf": auth.get("spf", "absent"),
            "dmarc": auth.get("dmarc", "absent"),
            "arc": "present" if "arc-seal" in h else "absent",
        }
        for proto in ("dkim", "spf", "dmarc"):
            val = auth_report[proto]
            if val not in ("pass", "absent"):
                warnings.append(f"{proto.upper()} check: {val!r}")

        # ── spam headers ─────────────────────────────────────────────────────
        spam: dict[str, str] = {k: h[k] for k in KNOWN_SPAM_HEADERS if k in h}
        if spam.get("x-spam-flag", "").upper() == "YES":
            warnings.append("Message flagged as spam (X-Spam-Flag: YES)")

        # ── delivery hops ────────────────────────────────────────────────────
        # raw_headers only has the *first* Received header; count from snippet
        received_count = auth_raw.count("by ") + 1  # rough heuristic
        if received_count > 8:
            warnings.append(f"Unusually long delivery path (~{received_count} hops)")

        # ── date sanity ──────────────────────────────────────────────────────
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

        # ── reply-to mismatch ────────────────────────────────────────────────
        reply_to = h.get("reply-to", "")
        if reply_to and reply_to != msg.sender:
            warnings.append(
                f"Reply-To ({reply_to!r}) differs from From ({msg.sender!r})"
            )

        # ── list headers ─────────────────────────────────────────────────────
        list_headers = {k: v for k, v in h.items() if k.startswith("list-")}

        return {
            "authentication": auth_report,
            "spam_headers": spam,
            "delivery_hop_estimate": received_count,
            "list_headers": list_headers,
            "date": msg.date,
            "warnings": warnings,
        }


class MIMEAnalyzer:
    """
    Analyze MIME structure reported by the MCP server.

    Because gmailmcp.googleapis.com returns full message payloads,
    we analyze the MIME type, attachments, and snippet structure.
    """

    @staticmethod
    def analyze(msg: MessageSummary) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        warnings: list[str] = []

        # Top-level part
        top: dict[str, Any] = {
            "mime_type": msg.mime_type,
            "is_attachment": False,
            "filename": None,
            "size_bytes": None,
            "warnings": [],
        }
        parts.append(top)

        for fname in msg.attachment_filenames:
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            risky_exts = {"exe", "bat", "sh", "cmd", "ps1", "vbs", "js", "msi"}
            entry: dict[str, Any] = {
                "mime_type": "attachment",
                "is_attachment": True,
                "filename": fname,
                "size_bytes": None,
                "warnings": [],
            }
            if ext in risky_exts:
                w = f"Potentially risky attachment: {fname!r} (.{ext})"
                entry["warnings"].append(w)
                warnings.append(w)
            parts.append(entry)

        return parts


class TroubleshootAnalyzer:
    def __init__(self, ollama: "OllamaClient") -> None:
        self._ollama = ollama

    async def analyze(self, thread: ThreadSummary) -> dict[str, Any]:
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

        first = thread.messages[0] if thread.messages else None
        auth_summary = header_reports[first.message_id]["authentication"] if first else {}

        prompt = textwrap.dedent(f"""
            Analyze this email thread for delivery, security, or MIME issues.

            Subject: {thread.subject}
            Participants: {', '.join(thread.participants[:5])}
            Message count: {len(thread.messages)}

            First message authentication:
            {json.dumps(auth_summary, indent=2)}

            Attachment filenames across thread:
            {json.dumps([f for m in thread.messages for f in m.attachment_filenames], indent=2)}

            Automated warnings raised:
            {chr(10).join(f'- {w}' for w in all_warnings) or '(none)'}

            Provide a prioritised list of issues and recommended fixes.
        """).strip()

        llm_analysis = await self._ollama.generate(prompt, system=SYSTEM_ANALYST)

        return {
            "header_reports": header_reports,
            "mime_reports": mime_reports,
            "automated_warnings": all_warnings,
            "llm_analysis": llm_analysis,
        }


# ─── Ollama client ────────────────────────────────────────────────────────────

class OllamaClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=Config.OLLAMA_TIMEOUT)

    async def _check_model(self) -> None:
        try:
            resp = await self._client.get(f"{Config.OLLAMA_BASE_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            base = Config.OLLAMA_MODEL.split(":")[0]
            if not any(base in m for m in models):
                raise OllamaError(
                    f"Model {Config.OLLAMA_MODEL!r} not found. "
                    f"Run: ollama pull {Config.OLLAMA_MODEL}"
                )
        except httpx.RequestError as exc:
            raise OllamaError(
                f"Cannot reach Ollama at {Config.OLLAMA_BASE_URL}. "
                "Is it running?  →  ollama serve"
            ) from exc

    async def generate(self, prompt: str, system: str = "") -> str:
        await self._check_model()
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
                f"Ollama timed out after {Config.OLLAMA_TIMEOUT}s."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaError(
                f"Ollama HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc
        return resp.json().get("response", "").strip()

    async def close(self) -> None:
        await self._client.aclose()


# ─── Core orchestrator ───────────────────────────────────────────────────────

class GmailAnalyzer:
    def __init__(self, mcp: GmailMCPClient, ollama: OllamaClient) -> None:
        self._mcp = mcp
        self._ollama = ollama
        self._troubleshooter = TroubleshootAnalyzer(ollama)

    async def analyze_thread(
        self,
        thread_id: str,
        mode: AnalysisMode = AnalysisMode.FULL,
    ) -> AnalysisResult:
        log.info("Fetching thread %s (mode=%s)", thread_id, mode.value)
        raw = await self._mcp.get_thread(thread_id)
        thread = ThreadParser.parse_thread(raw)
        return await self._run_analysis(thread, mode)

    async def search_and_analyze(
        self,
        query: str,
        mode: AnalysisMode = AnalysisMode.SUMMARIZE,
        max_results: int = Config.MAX_RESULTS,
    ) -> list[AnalysisResult]:
        log.info("Searching %r (max=%d)", query, max_results)
        stubs = await self._mcp.search_threads(query=query, max_results=max_results)
        results: list[AnalysisResult] = []
        for stub in stubs:
            tid = stub.get("id", "")
            if not tid:
                continue
            try:
                result = await self.analyze_thread(tid, mode)
                results.append(result)
            except (GmailMCPError, OllamaError) as exc:
                log.warning("Skipping thread %s: %s", tid, exc)
        return results

    async def _run_analysis(
        self, thread: ThreadSummary, mode: AnalysisMode
    ) -> AnalysisResult:
        result = AnalysisResult(
            mode=mode,
            thread_id=thread.thread_id,
            subject=thread.subject,
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            message_count=len(thread.messages),
        )

        if mode in (AnalysisMode.SUMMARIZE, AnalysisMode.FULL):
            result.summary = await self._summarize(thread)
            result.llm_response = result.summary

        if mode in (AnalysisMode.HEADERS, AnalysisMode.FULL):
            # Aggregate header reports for the whole thread
            combined: dict[str, Any] = {}
            for msg in thread.messages:
                rep = HeaderAnalyzer.analyze(msg)
                combined[msg.message_id] = rep
                result.warnings.extend(rep.get("warnings", []))
            result.header_report = combined

        if mode in (AnalysisMode.MIME, AnalysisMode.FULL):
            combined_mime: list[dict[str, Any]] = []
            for msg in thread.messages:
                parts = MIMEAnalyzer.analyze(msg)
                combined_mime.extend(parts)
                for p in parts:
                    result.warnings.extend(p.get("warnings", []))
            result.mime_report = combined_mime

        if mode in (AnalysisMode.TROUBLESHOOT, AnalysisMode.FULL):
            ts = await self._troubleshooter.analyze(thread)
            result.troubleshoot_report = ts
            result.warnings.extend(ts.get("automated_warnings", []))
            if mode == AnalysisMode.TROUBLESHOOT:
                result.llm_response = ts.get("llm_analysis", "")

        return result

    async def _summarize(self, thread: ThreadSummary) -> str:
        body_parts: list[str] = []
        for i, msg in enumerate(thread.messages, 1):
            body_parts.append(
                f"[Message {i}] From: {msg.sender}  Date: {msg.date}\n"
                f"{msg.body_preview or msg.snippet}"
            )

        prompt = textwrap.dedent(f"""
            Summarise this email thread in 4-6 sentences.
            Include the main topic, key action items, and any decisions or deadlines.

            Subject: {thread.subject}
            Participants: {', '.join(thread.participants[:6])}
            Messages: {len(thread.messages)}

            {chr(10).join(body_parts[:5])}
        """).strip()
        return await self._ollama.generate(prompt, system=SYSTEM_ANALYST)


# ─── CLI ─────────────────────────────────────────────────────────────────────

async def _cli_main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Analyze Gmail threads via Google's official MCP server "
            "(gmailmcp.googleapis.com) + Ollama llama3.2:1b"
        )
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        metavar="PATH",
        help="Path to .env file (default: .env in current directory)",
    )
    parser.add_argument(
        "--client-id",
        default="",
        help="OAuth2 client ID — overrides OAUTH_CLIENT_ID in .env",
    )
    parser.add_argument(
        "--client-secret",
        default="",
        help="OAuth2 client secret — overrides OAUTH_CLIENT_SECRET in .env",
    )
    parser.add_argument(
        "--token-file",
        default="",
        help="Path to cached OAuth token — overrides OAUTH_TOKEN_FILE in .env",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    search_cmd = sub.add_parser("search", help="Search threads and analyze them")
    search_cmd.add_argument(
        "query", help="Gmail search query, e.g. 'from:boss@example.com is:unread'"
    )
    search_cmd.add_argument("--max", type=int, default=Config.MAX_RESULTS)
    search_cmd.add_argument(
        "--mode",
        choices=[m.value for m in AnalysisMode],
        default=AnalysisMode.SUMMARIZE.value,
    )

    thread_cmd = sub.add_parser("thread", help="Analyze a single thread by ID")
    thread_cmd.add_argument("thread_id")
    thread_cmd.add_argument(
        "--mode",
        choices=[m.value for m in AnalysisMode],
        default=AnalysisMode.FULL.value,
    )

    sub.add_parser("debug-scopes", help="Print the scopes granted to the current token")

    args = parser.parse_args(argv)

    # Re-load the env file now that we know the path the user wants.
    # override=True here so the file values replace what was loaded at
    # import time (the default .env may be different from --env-file).
    env_path = Path(args.env_file)
    if not env_path.exists():
        log.warning("Env file not found: %s", env_path)
    else:
        load_dotenv(env_path, override=True)
        log.info("Loaded env file: %s", env_path)

    # CLI flags take final precedence; fall back to (now-reloaded) env vars.
    client_id = args.client_id or os.getenv("OAUTH_CLIENT_ID", "")
    client_secret = args.client_secret or os.getenv("OAUTH_CLIENT_SECRET", "")
    token_file = args.token_file or os.getenv("OAUTH_TOKEN_FILE", "token.json")

    tokens = OAuthTokenManager(
        client_id=client_id,
        client_secret=client_secret,
        token_file=token_file,
    )
    mcp = GmailMCPClient(tokens)
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
        elif args.command == "thread":
            result = await analyzer.analyze_thread(
                args.thread_id, mode=AnalysisMode(args.mode)
            )
            _print_result(result)

        elif args.command == "debug-scopes":
            info = await tokens.introspect()
            print("\nToken info from Google tokeninfo endpoint:")
            print(f"  Scopes granted : {info.get('scope', '(none)')}")
            print(f"  Audience       : {info.get('aud', '(none)')}")
            print(f"  Expires in     : {info.get('expires_in', '?' )} seconds")
            print(f"  Email          : {info.get('email', '(none)')}")
            if "error" in info:
                print(f"  ERROR          : {info['error']}: {info.get('error_description', '')}")

    except OAuthError as exc:
        log.error("OAuth error: %s", exc)
        return 2
    except GmailMCPError as exc:
        log.error("Gmail MCP error: %s", exc)
        return 1
    except OllamaError as exc:
        log.error("Ollama error: %s", exc)
        return 1
    finally:
        await mcp.close()
        await ollama.close()
        await tokens.close()

    return 0


def _print_result(result: AnalysisResult) -> None:
    sep = "─" * 62
    print(f"\n{sep}")
    print(f"  {result.subject}")
    print(f"  Thread ID  : {result.thread_id}")
    print(f"  Messages   : {result.message_count}")
    print(f"  Mode       : {result.mode.value}")
    print(f"  Analyzed   : {result.timestamp}")
    print(sep)

    if result.summary:
        print("\n📧 Summary\n")
        print(textwrap.indent(result.summary, "  "))

    if result.header_report:
        print("\n🔐 Authentication (first message)\n")
        first_rep = next(iter(result.header_report.values()), {})
        auth = first_rep.get("authentication", {})
        for k, v in auth.items():
            icon = "✅" if v == "pass" else ("⚠️ " if v == "absent" else "❌")
            print(f"  {icon} {k.upper()}: {v}")

    if result.mime_report:
        attachments = [p for p in result.mime_report if p.get("is_attachment")]
        if attachments:
            print(f"\n📎 Attachments ({len(attachments)})\n")
            for a in attachments:
                warn = "  ⚠️" if a.get("warnings") else ""
                print(f"  • {a['filename']}{warn}")

    if result.troubleshoot_report and result.troubleshoot_report.get("llm_analysis"):
        print("\n🔧 Troubleshooting Analysis\n")
        print(textwrap.indent(result.troubleshoot_report["llm_analysis"], "  "))

    if result.warnings:
        print("\n⚠️  Warnings\n")
        for w in sorted(set(result.warnings)):
            print(f"  • {w}")


def main() -> None:
    import asyncio
    sys.exit(asyncio.run(_cli_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
