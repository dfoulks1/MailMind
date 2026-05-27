"""
mailmind.gmail — Gmail REST API client and message parser.

``GmailClient`` wraps the Gmail REST API with typed methods for the
operations MailMind needs.  ``ThreadParser`` converts raw API responses
into the typed dataclasses defined in ``mailmind.models``.
"""

from __future__ import annotations

import base64
import email.mime.text
import logging
import re
from typing import Any

import httpx

from mailmind.config import Settings
from mailmind.models import GmailError, MessageSummary, ThreadSummary
from mailmind.oauth import OAuthTokenManager

log = logging.getLogger(__name__)


class GmailClient:
    """
    Async Gmail REST API client.

    All network errors are translated into ``GmailError`` so callers have a
    single exception type regardless of the underlying cause.

    Endpoints used
    --------------
    ``GET  /users/me/threads``              → :meth:`search_threads`
    ``GET  /users/me/threads/{id}``         → :meth:`get_thread`
    ``GET  /users/me/labels``               → :meth:`list_labels`
    ``POST /users/me/threads/{id}/modify``  → :meth:`label_thread` / :meth:`unlabel_thread`
    ``POST /users/me/drafts``               → :meth:`create_draft`
    """

    def __init__(self, settings: Settings, token_manager: OAuthTokenManager) -> None:
        self._settings = settings
        self._tokens   = token_manager
        self._http     = httpx.AsyncClient(timeout=settings.gmail_api_timeout)

    @property
    def _base(self) -> str:
        return self._settings.gmail_api_url

    # ── private HTTP helpers ──────────────────────────────────────────────────

    async def _get(self, path: str, **params: Any) -> Any:
        """
        Issue an authenticated GET request.

        Args:
            path:     Path relative to the Gmail API base URL.
            **params: Query-string parameters; ``None`` values are omitted.

        Raises:
            GmailError: On any HTTP or network error.
        """
        token = await self._tokens.get_access_token()
        try:
            resp = await self._http.get(
                f"{self._base}{path}",
                params={k: v for k, v in params.items() if v is not None},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GmailError(
                f"Gmail API HTTP {exc.response.status_code}: {exc.response.text[:400]}"
            ) from exc
        except httpx.RequestError as exc:
            raise GmailError(f"Gmail API connection error: {exc}") from exc
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        """
        Issue an authenticated POST request with a JSON body.

        Raises:
            GmailError: On any HTTP or network error.
        """
        token = await self._tokens.get_access_token()
        try:
            resp = await self._http.post(
                f"{self._base}{path}",
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GmailError(
                f"Gmail API HTTP {exc.response.status_code}: {exc.response.text[:400]}"
            ) from exc
        except httpx.RequestError as exc:
            raise GmailError(f"Gmail API connection error: {exc}") from exc
        return resp.json()

    # ── public API ────────────────────────────────────────────────────────────

    async def search_threads(
        self,
        query: str,
        max_results: int | None = None,
        page_token: str = "",
    ) -> list[dict[str, Any]]:
        """
        Search for threads matching a Gmail query string.

        Args:
            query:       Standard Gmail search query (e.g. ``is:unread``).
            max_results: Maximum thread stubs to return (API hard cap: 500).
                         Defaults to ``settings.gmail_max_results``.
            page_token:  Opaque pagination cursor from a previous response.

        Returns:
            List of thread stub dicts, each containing at least ``{"id": str}``.
        """
        limit = max_results if max_results is not None else self._settings.gmail_max_results
        params: dict[str, Any] = {"q": query, "maxResults": limit}
        if page_token:
            params["pageToken"] = page_token
        data = await self._get("/users/me/threads", **params)
        return list(data.get("threads", []))

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        """
        Fetch a full thread including all message payloads and headers.

        Args:
            thread_id: Gmail thread ID.
        """
        return dict(await self._get(f"/users/me/threads/{thread_id}", format="full"))

    async def list_labels(self) -> list[dict[str, Any]]:
        """Return all labels for the authenticated user's mailbox."""
        data = await self._get("/users/me/labels")
        return list(data.get("labels", []))

    async def label_thread(self, thread_id: str, label_ids: list[str]) -> None:
        """Add one or more labels to every message in a thread."""
        await self._post(
            f"/users/me/threads/{thread_id}/modify",
            {"addLabelIds": label_ids},
        )

    async def unlabel_thread(self, thread_id: str, label_ids: list[str]) -> None:
        """Remove one or more labels from every message in a thread."""
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
        """
        Create a draft message in the authenticated user's mailbox.

        Args:
            to:      Recipient email addresses.
            subject: Subject line.
            body:    Plain-text message body.
            cc:      Optional CC addresses.

        Returns:
            Draft ID assigned by Gmail.
        """
        msg = email.mime.text.MIMEText(body)
        msg["To"]      = ", ".join(to)
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = ", ".join(cc)
        raw  = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        data = await self._post("/users/me/drafts", {"message": {"raw": raw}})
        return str(data.get("id", ""))

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()


class ThreadParser:
    """
    Converts raw Gmail REST API thread responses into typed dataclasses.

    All methods are static/class methods; this class is never instantiated.

    Expected input shape (``messages.get`` with ``format=full``)::

        {
          "id": "<threadId>",
          "messages": [
            {
              "id": "<msgId>",
              "threadId": "<threadId>",
              "labelIds": [...],
              "snippet": "...",
              "payload": {
                "mimeType": "...",
                "headers": [{"name": ..., "value": ...}],
                "parts": [...]
              }
            }
          ]
        }
    """

    @classmethod
    def parse_thread(cls, raw: dict[str, Any]) -> ThreadSummary:
        """
        Parse a raw Gmail thread resource into a ``ThreadSummary``.

        Args:
            raw: Gmail REST API thread resource dict.
        """
        thread_id = raw.get("id", "")
        messages  = [
            cls._parse_message(m, thread_id) for m in raw.get("messages", [])
        ]
        return ThreadSummary(thread_id=thread_id, messages=messages)

    @classmethod
    def _parse_message(cls, raw_msg: dict[str, Any], thread_id: str) -> MessageSummary:
        """Parse a single message resource."""
        payload  = raw_msg.get("payload", {})
        headers  = cls._headers_dict(payload.get("headers", []))
        body, attachments = cls._extract_body_and_attachments(payload)

        to_cc      = f"{headers.get('to', '')},{headers.get('cc', '')}"
        recipients = [r.strip() for r in re.split(r",\s*", to_cc) if r.strip()]

        return MessageSummary(
            message_id          = raw_msg.get("id", ""),
            thread_id           = thread_id,
            subject             = headers.get("subject", "(no subject)"),
            sender              = headers.get("from", ""),
            recipients          = recipients,
            date                = headers.get("date", ""),
            snippet             = raw_msg.get("snippet", ""),
            labels              = raw_msg.get("labelIds", []),
            body                = body,
            raw_headers         = headers,
            mime_type           = payload.get("mimeType", ""),
            attachment_filenames = attachments,
        )

    @staticmethod
    def _headers_dict(header_list: list[dict[str, str]]) -> dict[str, str]:
        """
        Normalise a Gmail header list to a lowercase-key dict.

        Only the first occurrence of each header name is kept (RFC 2822
        semantics for unique headers such as Subject and From).
        """
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
        """
        Recursively extract the plain-text body and attachment filenames.

        ``text/plain`` parts are preferred; ``text/html`` parts are tag-stripped
        and used as a fallback.
        """
        plain       = ""
        attachments: list[str] = []

        mime = payload.get("mimeType", "")
        if mime == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                try:
                    plain = base64.urlsafe_b64decode(data + "==").decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    pass
        elif mime == "text/html" and not plain:
            data = payload.get("body", {}).get("data", "")
            if data:
                try:
                    html  = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
                except Exception:
                    pass

        for part in payload.get("parts", []):
            fname = part.get("filename", "")
            if fname:
                attachments.append(fname)
            child_body, child_att = cls._extract_body_and_attachments(part)
            plain = plain or child_body
            attachments.extend(child_att)

        return plain, attachments
