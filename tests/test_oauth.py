"""Tests for mailmind.oauth.OAuthTokenManager."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from mailmind.config import Settings
from mailmind.models import OAuthError
from mailmind.oauth import OAuthTokenManager


def _mgr(settings: Settings) -> OAuthTokenManager:
    return OAuthTokenManager(settings)


class TestOAuthTokenManager:
    async def test_no_credentials_raises(self, settings: Settings) -> None:
        s = Settings(oauth_client_id="", oauth_client_secret="")
        with pytest.raises(OAuthError, match="OAUTH_CLIENT_ID"):
            await _mgr(s).get_access_token()

    @respx.mock
    async def test_expired_token_is_refreshed(
        self, settings: Settings, tmp_path: Any
    ) -> None:
        token_file = str(tmp_path / "token.json")
        with open(token_file, "w") as f:
            json.dump(
                {"access_token": "old", "refresh_token": "r",
                 "expiry": "2000-01-01T00:00:00+00:00"},
                f,
            )
        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "new_tok", "expires_in": 3600}
            )
        )
        s = Settings(
            oauth_client_id="cid", oauth_client_secret="csec",
            oauth_token_file=token_file,
        )
        assert await _mgr(s).get_access_token() == "new_tok"

    async def test_valid_cached_token_returned(
        self, settings: Settings, tmp_path: Any
    ) -> None:
        token_file = str(tmp_path / "token.json")
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        with open(token_file, "w") as f:
            json.dump(
                {"access_token": "cached", "refresh_token": "", "expiry": future}, f
            )
        s = Settings(
            oauth_client_id="cid", oauth_client_secret="csec",
            oauth_token_file=token_file,
        )
        assert await _mgr(s).get_access_token() == "cached"

    @respx.mock
    async def test_failed_refresh_raises(
        self, settings: Settings, tmp_path: Any
    ) -> None:
        token_file = str(tmp_path / "token.json")
        with open(token_file, "w") as f:
            json.dump(
                {"access_token": "x", "refresh_token": "r",
                 "expiry": "2000-01-01T00:00:00+00:00"},
                f,
            )
        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        s = Settings(
            oauth_client_id="cid", oauth_client_secret="csec",
            oauth_token_file=token_file,
        )
        with pytest.raises(OAuthError, match="refresh failed"):
            await _mgr(s).get_access_token()
