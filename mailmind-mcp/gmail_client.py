"""
Gmail API client — handles OAuth2 token refresh and wraps common operations.

All methods return plain Python dicts/lists so tool functions can stay free
of Google-library types.
"""
from __future__ import annotations

import base64
import email
import json
import os
from pathlib import Path
from typing import Any

import structlog
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings, get_settings

log = structlog.get_logger(__name__)


class GmailClient:
    """Authenticated Gmail API client with automatic token refresh."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._cfg = (settings or get_settings()).gmail
        self._service: Any = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _build_service(self) -> Any:
        """Build (or re-use) an authenticated Gmail API service object."""
        if self._service:
            return self._service

        creds: Credentials | None = None
        token_path = Path(self._cfg.token_file)
        creds_path = Path(self._cfg.credentials_file)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(token_path), self._cfg.scopes
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not creds_path.exists():
                    raise FileNotFoundError(
                        f"Gmail credentials file not found: {creds_path}"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(creds_path), self._cfg.scopes
                )
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        log.info("gmail_client_authenticated")
        return self._service

    # ------------------------------------------------------------------
    # Message helpers
    # ------------------------------------------------------------------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def list_messages(
        self,
        query: str = "",
        max_results: int = 20,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """List messages matching *query*.

        Returns a dict with keys ``messages`` (list of id/threadId stubs)
        and optionally ``nextPageToken``.
        """
        svc = self._build_service()
        params: dict[str, Any] = {
            "userId": "me",
            "maxResults": max_results,
            "q": query,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            return svc.users().messages().list(**params).execute()
        except HttpError as exc:
            log.error("gmail_list_messages_error", error=str(exc))
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def get_message(
        self, message_id: str, fmt: str = "full"
    ) -> dict[str, Any]:
        """Fetch a single message by ID.

        Args:
            message_id: Gmail message ID.
            fmt: One of ``"full"``, ``"metadata"``, ``"minimal"``, ``"raw"``.
        """
        svc = self._build_service()
        try:
            return (
                svc.users()
                .messages()
                .get(userId="me", id=message_id, format=fmt)
                .execute()
            )
        except HttpError as exc:
            log.error("gmail_get_message_error", id=message_id, error=str(exc))
            raise

    def get_message_body(self, message_id: str) -> str:
        """Return decoded plain-text body of a message (best-effort)."""
        msg = self.get_message(message_id, fmt="full")
        return _extract_body(msg)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def modify_labels(
        self,
        message_id: str,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add/remove labels from a message."""
        svc = self._build_service()
        body: dict[str, Any] = {
            "addLabelIds": add_labels or [],
            "removeLabelIds": remove_labels or [],
        }
        try:
            return (
                svc.users()
                .messages()
                .modify(userId="me", id=message_id, body=body)
                .execute()
            )
        except HttpError as exc:
            log.error("gmail_modify_labels_error", id=message_id, error=str(exc))
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def trash_message(self, message_id: str) -> dict[str, Any]:
        """Move a message to the trash."""
        svc = self._build_service()
        try:
            return (
                svc.users().messages().trash(userId="me", id=message_id).execute()
            )
        except HttpError as exc:
            log.error("gmail_trash_error", id=message_id, error=str(exc))
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def delete_message(self, message_id: str) -> None:
        """Permanently delete a message (non-recoverable)."""
        svc = self._build_service()
        try:
            svc.users().messages().delete(userId="me", id=message_id).execute()
        except HttpError as exc:
            log.error("gmail_delete_error", id=message_id, error=str(exc))
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def list_labels(self) -> list[dict[str, Any]]:
        """Return all labels for the authenticated account."""
        svc = self._build_service()
        try:
            result = svc.users().labels().list(userId="me").execute()
            return result.get("labels", [])
        except HttpError as exc:
            log.error("gmail_list_labels_error", error=str(exc))
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def create_label(self, name: str) -> dict[str, Any]:
        """Create a new Gmail label."""
        svc = self._build_service()
        try:
            return (
                svc.users()
                .labels()
                .create(userId="me", body={"name": name})
                .execute()
            )
        except HttpError as exc:
            log.error("gmail_create_label_error", name=name, error=str(exc))
            raise

    def get_headers(self, message_id: str) -> dict[str, str]:
        """Return a flat dict of header name → value for *message_id*."""
        msg = self.get_message(message_id, fmt="metadata")
        headers: dict[str, str] = {}
        for h in msg.get("payload", {}).get("headers", []):
            headers[h["name"].lower()] = h["value"]
        return headers


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _extract_body(message: dict[str, Any]) -> str:
    """Extract plain-text body from a Gmail message dict."""
    payload = message.get("payload", {})
    return _walk_parts(payload)


def _walk_parts(part: dict[str, Any]) -> str:
    """Recursively walk MIME parts to find text/plain content."""
    mime = part.get("mimeType", "")
    if mime == "text/plain":
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for sub in part.get("parts", []):
        result = _walk_parts(sub)
        if result:
            return result
    return ""
