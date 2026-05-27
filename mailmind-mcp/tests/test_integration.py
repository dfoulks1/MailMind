"""
Integration tests.

These tests exercise the glue between components without hitting real
external services.  They verify:
  - Config loading and env interpolation
  - MIME body extraction from Gmail payloads
  - Server tool registry completeness
"""
from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


def test_default_settings_loads() -> None:
    """Settings should load with all defaults when no config file exists."""
    from mailmind_mcp.config import Settings

    s = Settings()
    assert s.gmail.token_file == "token.json"
    assert s.mcp.transport == "stdio"
    assert s.search.default_top_k == 10


def test_env_interpolation(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables should override YAML defaults."""
    import yaml
    from pathlib import Path

    cfg_file = tmp_path / "config.yaml"  # type: ignore[operator]
    cfg_file.write_text(
        yaml.dump(
            {
                "gmail": {"credentials_file": "${MY_CREDS:fallback.json}", "token_file": "tok.json", "scopes": []},
                "cache": {"mongo_uri": "mongodb://localhost", "mongo_db": "db", "mongo_collection": "col", "redis_url": "redis://localhost"},
                "rag": {"index_dir": "./idx", "ollama_url": "http://localhost:11434", "ollama_model": "llama3.2:1b"},
                "mcp": {"transport": "stdio"},
                "search": {},
            }
        )
    )
    monkeypatch.setenv("MY_CREDS", "real_creds.json")
    # Clear lru_cache so our env change is picked up.
    from mailmind_mcp.config import get_settings
    get_settings.cache_clear()

    settings = get_settings(config_path=str(cfg_file))
    assert settings.gmail.credentials_file == "real_creds.json"
    get_settings.cache_clear()


def test_env_interpolation_fallback(tmp_path: object) -> None:
    """Unset env vars should fall back to the YAML default value."""
    import yaml
    from pathlib import Path

    cfg_file = tmp_path / "config.yaml"  # type: ignore[operator]
    cfg_file.write_text(
        yaml.dump(
            {
                "gmail": {"credentials_file": "${MISSING_VAR:my_default.json}", "token_file": "tok.json", "scopes": []},
                "cache": {"mongo_uri": "mongodb://localhost", "mongo_db": "db", "mongo_collection": "col", "redis_url": "redis://localhost"},
                "rag": {"index_dir": "./idx", "ollama_url": "http://localhost:11434", "ollama_model": "llama3.2:1b"},
                "mcp": {"transport": "stdio"},
                "search": {},
            }
        )
    )
    os.environ.pop("MISSING_VAR", None)
    from mailmind_mcp.config import get_settings
    get_settings.cache_clear()

    settings = get_settings(config_path=str(cfg_file))
    assert settings.gmail.credentials_file == "my_default.json"
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Gmail body extraction tests
# ---------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_extract_plain_text_body() -> None:
    from mailmind_mcp.gmail_client import _extract_body

    msg = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64("Hello, world!")},
            "parts": [],
        }
    }
    assert _extract_body(msg) == "Hello, world!"


def test_extract_body_from_multipart() -> None:
    from mailmind_mcp.gmail_client import _extract_body

    msg = {
        "payload": {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64("Plain part")},
                    "parts": [],
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64("<b>HTML part</b>")},
                    "parts": [],
                },
            ],
        }
    }
    assert _extract_body(msg) == "Plain part"


def test_extract_body_empty() -> None:
    from mailmind_mcp.gmail_client import _extract_body

    msg = {"payload": {"mimeType": "text/html", "body": {}, "parts": []}}
    assert _extract_body(msg) == ""


# ---------------------------------------------------------------------------
# Server tool registry tests
# ---------------------------------------------------------------------------


def test_all_tools_registered() -> None:
    """Every expected tool name should be present in TOOL_REGISTRY."""
    from mailmind_mcp.server import TOOL_REGISTRY

    expected = {
        "search_gmail",
        "get_email",
        "get_email_headers",
        "list_labels",
        "create_label",
        "add_label",
        "remove_label",
        "mark_read",
        "mark_unread",
        "trash_email",
        "delete_email",
        "ingest_emails",
        "search_emails",
        "summarize_email",
        "ask_emails",
        "refresh_rag",
        "cache_stats",
    }
    assert expected.issubset(set(TOOL_REGISTRY.keys()))


def test_all_tools_have_schemas() -> None:
    """Every registered tool must have a valid JSON Schema dict."""
    from mailmind_mcp.server import TOOL_REGISTRY

    for name, (fn, schema) in TOOL_REGISTRY.items():
        assert isinstance(schema, dict), f"{name} schema is not a dict"
        assert schema.get("type") == "object", f"{name} schema missing 'type: object'"
        assert "properties" in schema, f"{name} schema missing 'properties'"
        assert callable(fn), f"{name} function is not callable"


def test_all_tools_have_docstrings() -> None:
    """Every registered tool function must have a docstring."""
    from mailmind_mcp.server import TOOL_REGISTRY

    for name, (fn, _) in TOOL_REGISTRY.items():
        assert fn.__doc__, f"{name} is missing a docstring"
