"""
mailmind.service — MailMind background service.

This module is the top-level entry point.  It wires every other module
together and exposes an HTTP API that Ollama models and the Node.js MCP
server can call over localhost.

Architecture
------------
::

    MailMindService
      ├── Settings          (from .env)
      ├── OAuthTokenManager (Gmail OAuth 2.0)
      ├── GmailClient       (Gmail REST API)
      ├── OllamaClient      (local Ollama server)
      ├── RagStore          (SQLite BM25 store — opened once, held for lifetime)
      ├── GmailAnalyzer     (orchestrates Gmail + Ollama analysis)
      └── IngestionScheduler (APScheduler — cron / interval ingest)

HTTP API
--------
All endpoints accept and return JSON.

``GET  /health``          Liveness check.
``GET  /status``          Service state: last ingest, store stats, next run.
``GET  /models``          Available Ollama models.
``POST /query``           RAG semantic query → ranked chunks.
``POST /analyze``         Fetch + analyze a Gmail thread on demand.
``POST /ingest``          Trigger an immediate out-of-schedule ingest run.
``POST /refresh``         Re-index the RAG store.

CLI
---
::

    mailmind [--env-file PATH] [--host HOST] [--port PORT]
             [--no-scheduler] [--log-level LEVEL]

    mailmind auth          # run the one-time OAuth flow
    mailmind debug-scopes  # print token scopes
    mailmind query TEXT    # one-shot RAG query, prints JSON
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mailmind.analysis import GmailAnalyzer
from mailmind.config import Settings
from mailmind.gmail import GmailClient
from mailmind.models import AnalysisMode, OAuthError, OllamaError, GmailError
from mailmind.oauth import OAuthTokenManager
from mailmind.ollama import OllamaClient
from mailmind.rag import RagStore, iso_to_timestamp
from mailmind.scheduler import IngestionScheduler

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic request / response models
# ─────────────────────────────────────────────────────────────────────────────


class QueryRequest(BaseModel):
    """Request body for ``POST /query``."""

    query: str
    top_k: int = 3


class AnalyzeRequest(BaseModel):
    """Request body for ``POST /analyze``."""

    thread_id: str
    mode: str = AnalysisMode.SUMMARIZE.value


class IngestRequest(BaseModel):
    """Optional body for ``POST /ingest`` (all fields have defaults)."""

    query: str | None = None  # override the default query for this run only


class RefreshRequest(BaseModel):
    """Request body for ``POST /refresh``."""

    since: str | None = None   # ISO 8601
    until: str | None = None   # ISO 8601
    full_reindex: bool = False
    dry_run: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# MailMindService
# ─────────────────────────────────────────────────────────────────────────────


class MailMindService:
    """
    Top-level service object.

    Owns all long-lived resources: the database connection, HTTP clients, and
    the background scheduler.  Designed to be created once per process and
    shut down cleanly on SIGTERM / SIGINT via the FastAPI lifespan hook.

    Usage::

        svc = MailMindService(settings)
        await svc.start()
        # ... serve requests ...
        await svc.stop()
    """

    def __init__(self, settings: Settings) -> None:
        self._settings  = settings

        self.tokens     = OAuthTokenManager(settings)
        self.gmail      = GmailClient(settings, self.tokens)
        self.ollama     = OllamaClient(settings)
        self.store      = RagStore(settings)
        self.analyzer   = GmailAnalyzer(settings, self.gmail, self.ollama)
        self.scheduler  = IngestionScheduler(settings, self.gmail, self.store)

        self._started_at: datetime | None = None

    async def start(self) -> None:
        """Open the database, warm up clients, and start the background scheduler."""
        self._started_at = datetime.now(UTC)
        self.store.open()
        log.info("RagStore opened at %s", self._settings.rag_db_path)
        await self.scheduler.start()
        log.info("MailMind service started.")

    async def stop(self) -> None:
        """Gracefully shut down: stop the scheduler, close HTTP clients and DB."""
        await self.scheduler.stop()
        await self.gmail.close()
        await self.ollama.close()
        await self.tokens.close()
        self.store.close()
        log.info("MailMind service stopped.")

    @property
    def uptime_seconds(self) -> float | None:
        if self._started_at is None:
            return None
        return (datetime.now(UTC) - self._started_at).total_seconds()


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application factory
# ─────────────────────────────────────────────────────────────────────────────


def create_app(settings: Settings) -> FastAPI:
    """
    Build and return the FastAPI application.

    The ``MailMindService`` instance is attached to ``app.state.svc`` inside
    the lifespan context so every request handler can access it via
    ``request.app.state.svc``.

    Args:
        settings: Runtime configuration.  Pass ``Settings.from_env()`` in
                  production; pass a test-specific ``Settings()`` in tests.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = MailMindService(settings)
        app.state.svc = svc
        await svc.start()
        try:
            yield
        finally:
            await svc.stop()

    app = FastAPI(
        title       = "MailMind",
        description = "Background Gmail RAG service with scheduled sync and Ollama integration.",
        version     = "1.0.0",
        lifespan    = lifespan,
    )

    # ── /health ───────────────────────────────────────────────────────────────

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        """Liveness probe. Returns 200 as long as the process is alive."""
        return {"status": "ok"}

    # ── /status ───────────────────────────────────────────────────────────────

    @app.get("/status", tags=["ops"])
    async def status() -> dict[str, Any]:
        """
        Service health and activity snapshot.

        Returns scheduler state, last ingest summary, store statistics,
        and service uptime.
        """
        svc: MailMindService = app.state.svc
        next_run = svc.scheduler.next_run_time()
        return {
            "status":      "running",
            "uptime_s":    svc.uptime_seconds,
            "store_stats": svc.store.stats(),
            "last_ingest": svc.scheduler.last_result or None,
            "next_ingest": next_run.isoformat() if next_run else None,
            "scheduler_enabled": settings.scheduler_enabled,
            "model":       settings.ollama_model,
        }

    # ── /models ───────────────────────────────────────────────────────────────

    @app.get("/models", tags=["ollama"])
    async def list_models() -> dict[str, Any]:
        """List all models currently available on the local Ollama server."""
        svc: MailMindService = app.state.svc
        try:
            models = await svc.ollama.list_models()
            return {"models": models, "configured": settings.ollama_model}
        except OllamaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    # ── /query ────────────────────────────────────────────────────────────────

    @app.post("/query", tags=["rag"])
    async def query_rag(req: QueryRequest) -> dict[str, Any]:
        """
        Query the local RAG store and return the most relevant email chunks.

        The response is compatible with the ``summarize_email.js`` MCP tool
        contract::

            {"chunks": [{"text": str, "score": float, "meta": {...}}]}

        This is the primary endpoint for Ollama models that need to retrieve
        context about emails before generating a response.
        """
        svc: MailMindService = app.state.svc
        if not req.query.strip():
            raise HTTPException(status_code=422, detail="Query must not be empty.")
        chunks = svc.store.query(req.query, top_k=req.top_k)
        return {"query": req.query, "top_k": req.top_k, "chunks": chunks}

    # ── /analyze ──────────────────────────────────────────────────────────────

    @app.post("/analyze", tags=["analysis"])
    async def analyze_thread(req: AnalyzeRequest) -> dict[str, Any]:
        """
        Fetch a Gmail thread by ID and run a full analysis pass.

        Accepts any ``AnalysisMode`` value: ``summarize``, ``headers``,
        ``mime``, ``troubleshoot``, or ``full``.

        The analysis result is also persisted to the RAG store so subsequent
        ``/query`` calls can surface it.
        """
        svc: MailMindService = app.state.svc
        try:
            mode   = AnalysisMode(req.mode)
        except ValueError:
            valid = [m.value for m in AnalysisMode]
            raise HTTPException(
                status_code=422,
                detail=f"Invalid mode {req.mode!r}. Valid: {valid}",
            ) from None
        try:
            result = await svc.analyzer.analyze_thread(req.thread_id, mode)
        except GmailError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except OllamaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except OAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        return {
            "thread_id":     result.thread_id,
            "subject":       result.subject,
            "mode":          result.mode.value,
            "timestamp":     result.timestamp,
            "message_count": result.message_count,
            "summary":       result.summary,
            "llm_response":  result.llm_response,
            "warnings":      result.warnings,
            "header_report": result.header_report,
            "mime_report":   result.mime_report,
        }

    # ── /ingest ───────────────────────────────────────────────────────────────

    @app.post("/ingest", tags=["scheduler"])
    async def trigger_ingest(req: IngestRequest) -> dict[str, Any]:
        """
        Trigger an immediate out-of-schedule ingest run.

        If ``query`` is supplied in the request body it overrides the
        ``gmail_default_query`` setting for this run only.  Returns the ingest
        summary immediately (the run is awaited synchronously so the response
        reflects the completed result).
        """
        svc: MailMindService = app.state.svc
        if req.query:
            # Temporarily patch the query for this single run.
            original = svc.scheduler._settings.gmail_default_query
            svc.scheduler._settings.gmail_default_query = req.query
            try:
                result = await svc.scheduler.run_once()
            finally:
                svc.scheduler._settings.gmail_default_query = original
        else:
            result = await svc.scheduler.run_once()
        return result

    # ── /refresh ──────────────────────────────────────────────────────────────

    @app.post("/refresh", tags=["rag"])
    async def refresh_rag(req: RefreshRequest) -> dict[str, Any]:
        """
        Re-index the RAG store without re-fetching emails from Gmail.

        Scope the operation with ISO 8601 ``since`` / ``until`` timestamps,
        or set ``full_reindex=true`` to rebuild the entire corpus.
        Use ``dry_run=true`` to count affected emails without making changes.
        """
        import time as _time
        svc: MailMindService = app.state.svc
        since_ts = iso_to_timestamp(req.since)
        until_ts = iso_to_timestamp(req.until)
        t0       = _time.monotonic()

        if req.full_reindex:
            count  = svc.store.full_reindex(dry_run=req.dry_run)
            detail = "Full corpus re-index" + (" (dry run)" if req.dry_run else "")
        else:
            count  = svc.store.reindex_range(since_ts, until_ts, dry_run=req.dry_run)
            detail = (
                f"Incremental re-index [{req.since or 'beginning'}"
                f" → {req.until or 'now'}]"
                + (" (dry run)" if req.dry_run else "")
            )

        return {
            "reindexed":   count,
            "status":      "ok",
            "duration_ms": int((time_monotonic := _time.monotonic() - t0) * 1000),
            "detail":      detail,
            "store_stats": svc.store.stats(),
        }

    return app


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog        = "mailmind",
        description = "MailMind background Gmail RAG service.",
    )
    parser.add_argument(
        "--env-file", default=".env", metavar="PATH",
        help="Path to .env file (default: .env in current directory).",
    )
    parser.add_argument(
        "--host", default=None,
        help="Bind host (overrides SERVICE_HOST in .env).",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Bind port (overrides SERVICE_PORT in .env).",
    )
    parser.add_argument(
        "--no-scheduler", action="store_true",
        help="Disable the background ingest scheduler.",
    )
    parser.add_argument(
        "--log-level", default=None,
        choices=["debug", "info", "warning", "error"],
        help="Uvicorn log level (overrides SERVICE_LOG_LEVEL in .env).",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("auth",         help="Run the one-time OAuth authorization flow.")
    sub.add_parser("debug-scopes", help="Print scopes granted to the current token.")

    q_cmd = sub.add_parser("query", help="One-shot RAG query, prints JSON.")
    q_cmd.add_argument("text", help="Query text.")
    q_cmd.add_argument("--top-k", type=int, default=3)

    return parser


async def _run_auth(settings: Settings) -> None:
    """Interactive OAuth authorization flow."""
    mgr = OAuthTokenManager(settings)
    try:
        token = await mgr.get_access_token()
        log.info("Authorization successful; token saved to %s", settings.oauth_token_file)
        print(f"\n✓ Authorization successful.  Token saved to {settings.oauth_token_file!r}.")
        print(f"  Access token (first 16 chars): {token[:16]}...")
    finally:
        await mgr.close()


async def _run_debug_scopes(settings: Settings) -> None:
    """Print OAuth scopes for the current token."""
    mgr = OAuthTokenManager(settings)
    try:
        info = await mgr.introspect()
        print("\nToken info from Google tokeninfo endpoint:")
        print(f"  Scopes granted : {info.get('scope', '(none)')}")
        print(f"  Audience       : {info.get('aud', '(none)')}")
        print(f"  Expires in     : {info.get('expires_in', '?')} seconds")
        print(f"  Email          : {info.get('email', '(none)')}")
        if "error" in info:
            print(f"  ERROR          : {info['error']}: {info.get('error_description', '')}")
    finally:
        await mgr.close()


async def _run_query(settings: Settings, text: str, top_k: int) -> None:
    """One-shot RAG query."""
    import json
    with RagStore(settings) as store:
        results = store.query(text, top_k=top_k)
    print(json.dumps({"query": text, "chunks": results}, indent=2))


def cli_entry() -> None:
    """
    Console-script entry point registered as ``mailmind`` in pyproject.toml.

    Loads ``.env``, applies CLI overrides, then either:
    * runs an async sub-command (``auth``, ``debug-scopes``, ``query``), or
    * starts the Uvicorn HTTP server (default when no sub-command is given).
    """
    import uvicorn

    parser  = _build_arg_parser()
    args    = parser.parse_args()

    # Load env file (override=False so real env vars always win).
    env_path = Path(args.env_file)
    if env_path.exists():
        load_dotenv(env_path, override=False)
        logging.basicConfig(level=logging.INFO)
        log.info("Loaded env file: %s", env_path)
    else:
        logging.basicConfig(level=logging.INFO)
        if args.env_file != ".env":
            log.warning("Env file not found: %s", env_path)

    settings = Settings.from_env()

    # Apply CLI overrides.
    if args.host:
        settings.service_host = args.host
    if args.port:
        settings.service_port = args.port
    if args.no_scheduler:
        settings.scheduler_enabled = False
    if args.log_level:
        settings.service_log_level = args.log_level

    # Sub-command routing.
    if args.command == "auth":
        asyncio.run(_run_auth(settings))
        return
    if args.command == "debug-scopes":
        asyncio.run(_run_debug_scopes(settings))
        return
    if args.command == "query":
        asyncio.run(_run_query(settings, args.text, args.top_k))
        return

    # Default: start the HTTP service.
    app = create_app(settings)
    uvicorn.run(
        app,
        host      = settings.service_host,
        port      = settings.service_port,
        log_level = settings.service_log_level,
    )


if __name__ == "__main__":
    cli_entry()
