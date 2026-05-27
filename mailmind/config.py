"""
mailmind.config — centralised runtime configuration.

All settings are read from environment variables (or a .env file loaded by
the service entry point).  ``Settings`` is a plain dataclass — no
class-level ``os.getenv`` calls — so it can be instantiated with explicit
values in tests without touching ``os.environ``.

Usage::

    from mailmind.config import Settings
    cfg = Settings.from_env()   # production: reads os.environ
    cfg = Settings()             # tests: uses defaults, no env access
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    """
    Runtime configuration for MailMind.

    Instantiate via ``Settings.from_env()`` in production.  In tests,
    construct directly with the values you need to override.

    Gmail API
    ---------
    gmail_api_url       Base URL for the Gmail REST API.
    oauth_client_id     OAuth 2.0 Desktop-app client ID.
    oauth_client_secret OAuth 2.0 client secret.
    oauth_token_file    Path to the cached access/refresh token JSON.
    oauth_scopes        List of OAuth scopes to request.

    Ollama
    ------
    ollama_base_url     Local Ollama server base URL.
    ollama_model        Model tag (e.g. ``llama3.2:1b``).
    ollama_timeout      HTTP timeout in seconds.
    ollama_num_ctx      Context-window token count.
    ollama_temperature  Sampling temperature.

    Gmail fetch
    -----------
    gmail_default_query Default Gmail search query used by the scheduler.
    gmail_max_results   Maximum threads fetched per scheduled ingest run.
    gmail_max_body_chars Body characters forwarded to the LLM per message.
    gmail_api_timeout   HTTP timeout for Gmail API calls in seconds.

    RAG store
    ---------
    rag_db_path         Path to the SQLite database file.
    rag_chunk_size      Word count target per text chunk.
    rag_chunk_overlap   Shared words between consecutive chunks.

    Scheduler
    ---------
    ingest_cron         Cron expression for scheduled ingest
                        (e.g. ``"0 */6 * * *"`` — every 6 hours).
                        If set, takes precedence over ``ingest_interval_minutes``.
    ingest_interval_minutes  Fallback interval (default 360 = 6 hours).
                        Ignored when ``ingest_cron`` is set.
    scheduler_enabled   Set to ``"false"`` to disable background scheduling.

    HTTP service
    ------------
    service_host        Bind host for the FastAPI service (default ``127.0.0.1``).
    service_port        Bind port (default ``8765``).
    service_log_level   Uvicorn log level.
    """

    # ── Gmail API ─────────────────────────────────────────────────────────────
    gmail_api_url: str = "https://gmail.googleapis.com/gmail/v1"
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_file: str = "token.json"
    oauth_scopes: list[str] = field(default_factory=lambda: [
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ])

    # ── Ollama ────────────────────────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:1b"
    ollama_timeout: float = 120.0
    ollama_num_ctx: int = 4096
    ollama_temperature: float = 0.2

    # ── Gmail fetch ───────────────────────────────────────────────────────────
    gmail_default_query: str = "category:inbox -category:trash"
    gmail_max_results: int = 200
    gmail_max_body_chars: int = 6000
    gmail_api_timeout: float = 30.0

    # ── RAG store ─────────────────────────────────────────────────────────────
    rag_db_path: str = "mailmind.db"
    rag_chunk_size: int = 400
    rag_chunk_overlap: int = 40

    # ── Scheduler ─────────────────────────────────────────────────────────────
    ingest_cron: str = ""               # e.g. "0 */6 * * *"
    ingest_interval_minutes: int = 360  # fallback when ingest_cron is empty
    scheduler_enabled: bool = True

    # ── HTTP service ──────────────────────────────────────────────────────────
    service_host: str = "127.0.0.1"
    service_port: int = 8765
    service_log_level: str = "info"

    @classmethod
    def from_env(cls) -> Settings:
        """
        Construct a ``Settings`` instance from environment variables.

        Every attribute has a corresponding env var formed by uppercasing the
        field name (e.g. ``rag_db_path`` → ``RAG_DB_PATH``).  Missing vars
        fall back to the dataclass defaults.
        """
        def _str(key: str, default: str) -> str:
            return os.getenv(key, default)

        def _int(key: str, default: int) -> int:
            return int(os.getenv(key, str(default)))

        def _float(key: str, default: float) -> float:
            return float(os.getenv(key, str(default)))

        def _bool(key: str, default: bool) -> bool:
            return os.getenv(key, str(default)).lower() not in ("0", "false", "no")

        return cls(
            gmail_api_url          = _str("GMAIL_API_URL",           cls.gmail_api_url),
            oauth_client_id        = _str("OAUTH_CLIENT_ID",         ""),
            oauth_client_secret    = _str("OAUTH_CLIENT_SECRET",     ""),
            oauth_token_file       = _str("OAUTH_TOKEN_FILE",        cls.oauth_token_file),
            ollama_base_url        = _str("OLLAMA_BASE_URL",         cls.ollama_base_url),
            ollama_model           = _str("OLLAMA_MODEL",            cls.ollama_model),
            ollama_timeout         = _float("OLLAMA_TIMEOUT",        cls.ollama_timeout),
            ollama_num_ctx         = _int("OLLAMA_NUM_CTX",          cls.ollama_num_ctx),
            ollama_temperature     = _float("OLLAMA_TEMPERATURE",    cls.ollama_temperature),
            gmail_default_query    = _str("GMAIL_DEFAULT_QUERY",     cls.gmail_default_query),
            gmail_max_results      = _int("GMAIL_MAX_RESULTS",       cls.gmail_max_results),
            gmail_max_body_chars   = _int("GMAIL_MAX_BODY_CHARS",    cls.gmail_max_body_chars),
            gmail_api_timeout      = _float("GMAIL_API_TIMEOUT",     cls.gmail_api_timeout),
            rag_db_path            = _str("RAG_DB_PATH",             cls.rag_db_path),
            rag_chunk_size         = _int("RAG_CHUNK_SIZE",          cls.rag_chunk_size),
            rag_chunk_overlap      = _int("RAG_CHUNK_OVERLAP",       cls.rag_chunk_overlap),
            ingest_cron            = _str("INGEST_CRON",             ""),
            ingest_interval_minutes = _int("INGEST_INTERVAL_MINUTES", cls.ingest_interval_minutes),
            scheduler_enabled      = _bool("SCHEDULER_ENABLED",      cls.scheduler_enabled),
            service_host           = _str("SERVICE_HOST",            cls.service_host),
            service_port           = _int("SERVICE_PORT",            cls.service_port),
            service_log_level      = _str("SERVICE_LOG_LEVEL",       cls.service_log_level),
        )
