"""Tests for mailmind.service — FastAPI HTTP endpoints."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mailmind.config import Settings
from mailmind.models import GmailError, OAuthError, OllamaError
from mailmind.service import MailMindService, create_app
from tests.conftest import RAW_THREAD


# ── Test app factory ───────────────────────────────────────────────────────────


@pytest.fixture
def svc(settings: Settings) -> MailMindService:
    """A MailMindService with all dependencies mocked."""
    service = MailMindService.__new__(MailMindService)
    service._settings  = settings
    service._started_at = None

    store = MagicMock()
    store.query.return_value = [
        {"text": "invoice chunk", "score": 1.5, "meta": {"id": "m1"}}
    ]
    store.stats.return_value = {"emails": 10, "chunks": 40, "term_entries": 300}
    store.full_reindex.return_value = 10
    store.reindex_range.return_value = 3

    ollama = AsyncMock()
    ollama.list_models.return_value = ["llama3.2:1b", "mistral"]

    analyzer = AsyncMock()
    analyzer.analyze_thread.return_value = MagicMock(
        thread_id="thread_001",
        subject="Test Subject",
        mode=MagicMock(value="summarize"),
        timestamp="2024-01-01T00:00:00Z",
        message_count=1,
        summary="Test summary",
        llm_response="Test summary",
        warnings=[],
        header_report={},
        mime_report=[],
        troubleshoot_report={},
    )

    scheduler = AsyncMock()
    scheduler.last_result  = {}
    # next_run_time() is a synchronous method — override with a plain MagicMock
    # so it returns None directly instead of a coroutine.
    scheduler.next_run_time = MagicMock(return_value=None)
    scheduler.run_once.return_value = {
        "fetched": 5, "new": 3, "updated": 2, "errors": 0,
        "elapsed_s": 1.2, "started_at": "2024-01-01T00:00:00Z",
        "finished_at": "2024-01-01T00:00:01Z", "query": "category:inbox",
    }

    service.store     = store
    service.ollama    = ollama
    service.analyzer  = analyzer
    service.scheduler = scheduler
    return service


@pytest.fixture
def client(svc: MailMindService, settings: Settings) -> TestClient:
    """TestClient with the service injected directly (no real lifespan)."""
    app = create_app(settings)
    # Bypass lifespan startup/shutdown — inject the mock service directly.
    app.state.svc = svc
    return TestClient(app, raise_server_exceptions=True)


# ── /health ────────────────────────────────────────────────────────────────────


class TestHealth:
    def test_returns_200(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200

    def test_returns_ok(self, client: TestClient) -> None:
        assert client.get("/health").json() == {"status": "ok"}


# ── /status ────────────────────────────────────────────────────────────────────


class TestStatus:
    def test_returns_200(self, client: TestClient) -> None:
        assert client.get("/status").status_code == 200

    def test_contains_store_stats(self, client: TestClient) -> None:
        body = client.get("/status").json()
        assert "store_stats" in body
        assert body["store_stats"]["emails"] == 10

    def test_contains_model(self, client: TestClient) -> None:
        body = client.get("/status").json()
        assert "model" in body


# ── /models ────────────────────────────────────────────────────────────────────


class TestModels:
    def test_returns_model_list(self, client: TestClient) -> None:
        body = client.get("/models").json()
        assert "llama3.2:1b" in body["models"]

    def test_ollama_error_returns_503(
        self, svc: MailMindService, settings: Settings
    ) -> None:
        svc.ollama.list_models.side_effect = OllamaError("offline")
        app = create_app(settings)
        app.state.svc = svc
        c = TestClient(app, raise_server_exceptions=False)
        assert c.get("/models").status_code == 503


# ── /query ─────────────────────────────────────────────────────────────────────


class TestQuery:
    def test_returns_chunks(self, client: TestClient) -> None:
        body = client.post("/query", json={"query": "invoice payment"}).json()
        assert len(body["chunks"]) == 1
        assert body["chunks"][0]["text"] == "invoice chunk"

    def test_empty_query_returns_422(self, client: TestClient) -> None:
        assert client.post("/query", json={"query": ""}).status_code == 422

    def test_top_k_passed_to_store(
        self, client: TestClient, svc: MailMindService
    ) -> None:
        client.post("/query", json={"query": "test", "top_k": 5})
        svc.store.query.assert_called_once_with("test", top_k=5)


# ── /analyze ───────────────────────────────────────────────────────────────────


class TestAnalyze:
    def test_returns_analysis(self, client: TestClient) -> None:
        body = client.post(
            "/analyze", json={"thread_id": "thread_001", "mode": "summarize"}
        ).json()
        assert body["thread_id"] == "thread_001"
        assert body["summary"]   == "Test summary"

    def test_invalid_mode_returns_422(self, client: TestClient) -> None:
        assert (
            client.post("/analyze", json={"thread_id": "t1", "mode": "invalid"}).status_code
            == 422
        )

    def test_gmail_error_returns_502(
        self, svc: MailMindService, settings: Settings
    ) -> None:
        svc.analyzer.analyze_thread.side_effect = GmailError("not found")
        app = create_app(settings)
        app.state.svc = svc
        c = TestClient(app, raise_server_exceptions=False)
        assert c.post("/analyze", json={"thread_id": "t1", "mode": "summarize"}).status_code == 502

    def test_ollama_error_returns_503(
        self, svc: MailMindService, settings: Settings
    ) -> None:
        svc.analyzer.analyze_thread.side_effect = OllamaError("model gone")
        app = create_app(settings)
        app.state.svc = svc
        c = TestClient(app, raise_server_exceptions=False)
        assert c.post("/analyze", json={"thread_id": "t1", "mode": "summarize"}).status_code == 503

    def test_oauth_error_returns_401(
        self, svc: MailMindService, settings: Settings
    ) -> None:
        svc.analyzer.analyze_thread.side_effect = OAuthError("expired")
        app = create_app(settings)
        app.state.svc = svc
        c = TestClient(app, raise_server_exceptions=False)
        assert c.post("/analyze", json={"thread_id": "t1", "mode": "summarize"}).status_code == 401


# ── /ingest ────────────────────────────────────────────────────────────────────


class TestIngest:
    def test_returns_ingest_summary(self, client: TestClient) -> None:
        body = client.post("/ingest", json={}).json()
        assert body["fetched"]  == 5
        assert body["new"]      == 3
        assert body["errors"]   == 0

    def test_run_once_called(
        self, client: TestClient, svc: MailMindService
    ) -> None:
        client.post("/ingest", json={})
        svc.scheduler.run_once.assert_called_once()


# ── /refresh ───────────────────────────────────────────────────────────────────


class TestRefresh:
    def test_full_reindex(self, client: TestClient, svc: MailMindService) -> None:
        body = client.post("/refresh", json={"full_reindex": True}).json()
        assert body["status"] == "ok"
        svc.store.full_reindex.assert_called_once_with(dry_run=False)

    def test_dry_run(self, client: TestClient, svc: MailMindService) -> None:
        client.post("/refresh", json={"full_reindex": True, "dry_run": True})
        svc.store.full_reindex.assert_called_once_with(dry_run=True)

    def test_incremental_refresh(self, client: TestClient, svc: MailMindService) -> None:
        client.post("/refresh", json={
            "since": "2024-01-01T00:00:00Z",
            "until": "2024-06-01T00:00:00Z",
        })
        svc.store.reindex_range.assert_called_once()

    def test_response_includes_store_stats(self, client: TestClient) -> None:
        body = client.post("/refresh", json={}).json()
        assert "store_stats" in body
