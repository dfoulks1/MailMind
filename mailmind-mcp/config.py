"""
Configuration loader for Mailmind MCP Server.

Reads config/config.yaml, expands ${ENV_VAR:default} placeholders,
and exposes a validated Settings object.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings

# ---------------------------------------------------------------------------
# YAML env-interpolation helpers
# ---------------------------------------------------------------------------
_ENV_RE = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def _expand_env(value: str) -> str:
    """Replace ``${VAR:default}`` patterns with env values or defaults."""

    def _replace(m: re.Match[str]) -> str:
        var, default = m.group(1), m.group(2) or ""
        return os.environ.get(var, default)

    return _ENV_RE.sub(_replace, value)


def _expand_dict(obj: Any) -> Any:  # noqa: ANN401
    """Recursively expand env placeholders in a parsed YAML structure."""
    if isinstance(obj, dict):
        return {k: _expand_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_dict(i) for i in obj]
    if isinstance(obj, str):
        return _expand_env(obj)
    return obj


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class GmailConfig(BaseModel):
    credentials_file: str = "credentials.json"
    token_file: str = "token.json"
    scopes: list[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.labels",
    ]


class CacheConfig(BaseModel):
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "mailmind"
    mongo_collection: str = "emails"
    redis_url: str = "redis://localhost:6379/0"


class RAGConfig(BaseModel):
    index_dir: str = "./data/rag_index"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:1b"
    max_ingest_batch: int = 50


class MCPConfig(BaseModel):
    transport: str = "stdio"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    @field_validator("transport")
    @classmethod
    def _valid_transport(cls, v: str) -> str:
        if v not in {"stdio", "sse"}:
            raise ValueError("transport must be 'stdio' or 'sse'")
        return v


class SearchConfig(BaseModel):
    default_top_k: int = 10
    min_similarity: float = 0.5


class Settings(BaseModel):
    gmail: GmailConfig = GmailConfig()
    cache: CacheConfig = CacheConfig()
    rag: RAGConfig = RAGConfig()
    mcp: MCPConfig = MCPConfig()
    search: SearchConfig = SearchConfig()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


@lru_cache(maxsize=1)
def get_settings(config_path: str | None = None) -> Settings:
    """Load and cache Settings from YAML (with env-var interpolation)."""
    path = Path(config_path) if config_path else _CONFIG_PATH
    if not path.exists():
        return Settings()
    raw = yaml.safe_load(path.read_text())
    expanded = _expand_dict(raw)
    return Settings.model_validate(expanded)
