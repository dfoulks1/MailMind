"""
mailmind.oauth — OAuth 2.0 token management for the Gmail API.

``OAuthTokenManager`` handles the full desktop out-of-band flow, token
persistence to disk, and silent refresh via the refresh-token grant
(RFC 6749 §6).  It is injected into ``GmailClient`` at construction time.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from mailmind.config import Settings
from mailmind.models import OAuthError

log = logging.getLogger(__name__)


class OAuthTokenManager:
    """
    Manages the OAuth 2.0 access/refresh token lifecycle for the Gmail API.

    Token persistence
    -----------------
    Tokens are persisted to ``settings.oauth_token_file`` (JSON) after each
    successful exchange or refresh.  On startup the file is read; if the token
    is still valid it is returned immediately.  If it is within the expiry
    buffer the refresh-token grant runs automatically.  If no token exists at
    all the interactive desktop out-of-band flow is triggered.

    Thread safety
    -------------
    Not thread-safe; designed for use within a single asyncio event loop.

    First-run
    ---------
    On first run ``get_access_token()`` prints an authorization URL, reads
    the code from stdin, and performs the initial exchange.  Tokens are then
    written to disk for all subsequent runs.
    """

    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
    REDIRECT_URI     = "urn:ietf:wg:oauth:2.0:oob"
    # Refresh this many seconds before actual expiry to absorb clock skew.
    _EXPIRY_BUFFER_SECS = 60

    def __init__(self, settings: Settings) -> None:
        self._settings      = settings
        self._access_token  = ""
        self._refresh_token = ""
        self._expiry: datetime | None = None
        self._http = httpx.AsyncClient(timeout=30.0)

    # ── token file I/O ────────────────────────────────────────────────────────

    def _load_token_file(self) -> bool:
        """
        Read cached credentials from disk.

        Returns:
            ``True`` if an access token was successfully loaded.
        """
        path = self._settings.oauth_token_file
        if not os.path.exists(path):
            return False
        try:
            with open(path) as fh:
                data = json.load(fh)
            self._access_token  = data.get("access_token",  "")
            self._refresh_token = data.get("refresh_token", "")
            expiry_str = data.get("expiry", "")
            if expiry_str:
                self._expiry = datetime.fromisoformat(expiry_str)
            return bool(self._access_token)
        except Exception as exc:
            log.warning("Could not read token file %s: %s", path, exc)
            return False

    def _save_token_file(self) -> None:
        """Persist current credentials to disk, overwriting any previous file."""
        with open(self._settings.oauth_token_file, "w") as fh:
            json.dump(
                {
                    "access_token":  self._access_token,
                    "refresh_token": self._refresh_token,
                    "expiry":        self._expiry.isoformat() if self._expiry else "",
                },
                fh,
                indent=2,
            )

    # ── expiry check ──────────────────────────────────────────────────────────

    def _is_expired(self) -> bool:
        """
        Return ``True`` if the token is expired or within the refresh buffer.

        A token with no recorded expiry is treated as still valid.
        """
        if not self._expiry:
            return False
        expiry = self._expiry
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return datetime.now(UTC) >= expiry - timedelta(seconds=self._EXPIRY_BUFFER_SECS)

    # ── public API ────────────────────────────────────────────────────────────

    async def get_access_token(self) -> str:
        """
        Return a valid Bearer token, running a refresh or interactive flow
        if necessary.

        Raises:
            OAuthError: If the token cannot be obtained or refreshed.
        """
        if not self._access_token:
            self._load_token_file()

        if self._access_token and not self._is_expired():
            return self._access_token

        if self._refresh_token:
            await self._refresh()
            return self._access_token

        await self._interactive_flow()
        return self._access_token

    async def introspect(self) -> dict[str, Any]:
        """
        Call Google's tokeninfo endpoint to inspect the current token.

        Returns:
            Dict with ``scope``, ``aud``, ``expires_in``, ``email`` fields.
        """
        token = await self.get_access_token()
        resp = await self._http.get(
            "https://www.googleapis.com/oauth2/v3/tokeninfo",
            params={"access_token": token},
        )
        return dict(resp.json())

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    # ── private OAuth flows ───────────────────────────────────────────────────

    async def _refresh(self) -> None:
        """Exchange the refresh token for a new access token (RFC 6749 §6)."""
        resp = await self._http.post(
            self.GOOGLE_TOKEN_URL,
            data={
                "client_id":     self._settings.oauth_client_id,
                "client_secret": self._settings.oauth_client_secret,
                "refresh_token": self._refresh_token,
                "grant_type":    "refresh_token",
            },
        )
        if resp.status_code != 200:
            raise OAuthError(
                f"Token refresh failed ({resp.status_code}): {resp.text}"
            )
        self._apply_token_response(resp.json())

    async def _interactive_flow(self) -> None:
        """
        Desktop OAuth 2.0 out-of-band flow.

        Prints the authorization URL and reads the authorization code from
        stdin.  Only used on first run or after token revocation.

        Raises:
            OAuthError: If credentials are not configured or no code is given.
        """
        s = self._settings
        if not s.oauth_client_id or not s.oauth_client_secret:
            raise OAuthError(
                "OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET must be set in .env. "
                "See README.md for Google Cloud project setup."
            )
        params = {
            "client_id":     s.oauth_client_id,
            "redirect_uri":  self.REDIRECT_URI,
            "response_type": "code",
            "scope":         " ".join(s.oauth_scopes),
            "access_type":   "offline",
            "prompt":        "consent",
        }
        url = f"{self.GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
        print("\n─── MailMind OAuth Authorization ───────────────────────────")
        print("Open this URL in your browser and authorize the application:\n")
        print(f"  {url}\n")
        code = input("Paste the authorization code here: ").strip()
        if not code:
            raise OAuthError("No authorization code provided.")

        resp = await self._http.post(
            self.GOOGLE_TOKEN_URL,
            data={
                "client_id":     s.oauth_client_id,
                "client_secret": s.oauth_client_secret,
                "code":          code,
                "redirect_uri":  self.REDIRECT_URI,
                "grant_type":    "authorization_code",
            },
        )
        if resp.status_code != 200:
            raise OAuthError(
                f"Authorization code exchange failed ({resp.status_code}): {resp.text}"
            )
        self._apply_token_response(resp.json())

    def _apply_token_response(self, data: dict[str, Any]) -> None:
        """
        Store tokens from a Google token endpoint response and persist them.

        The ``refresh_token`` field is absent on non-first refreshes; the
        existing refresh token is preserved in that case.
        """
        self._access_token = data["access_token"]
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]
        expires_in = int(data.get("expires_in", 3600))
        self._expiry = datetime.now(UTC) + timedelta(seconds=expires_in)
        self._save_token_file()
        log.info("OAuth token refreshed, expires %s", self._expiry.isoformat())
