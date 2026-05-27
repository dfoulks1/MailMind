"""Tests for mailmind.scheduler.IngestionScheduler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mailmind.config import Settings
from mailmind.gmail import GmailClient, ThreadParser
from mailmind.models import GmailError
from mailmind.rag import RagStore
from mailmind.scheduler import IngestionScheduler
from tests.conftest import RAW_THREAD


@pytest.fixture
def store(settings: Settings) -> RagStore:
    s = RagStore(settings)
    s.open()
    yield s
    s.close()


@pytest.fixture
def scheduler(settings: Settings, mock_gmail: AsyncMock, store: RagStore) -> IngestionScheduler:
    return IngestionScheduler(settings, mock_gmail, store)


class TestIngestionScheduler:
    async def test_run_once_returns_summary(
        self, scheduler: IngestionScheduler, mock_gmail: AsyncMock
    ) -> None:
        result = await scheduler.run_once()
        assert "fetched"    in result
        assert "new"        in result
        assert "updated"    in result
        assert "errors"     in result
        assert "elapsed_s"  in result
        assert "started_at" in result

    async def test_new_emails_incremented(
        self, scheduler: IngestionScheduler, store: RagStore
    ) -> None:
        result = await scheduler.run_once()
        assert result["fetched"] >= 1
        assert result["new"]     >= 1

    async def test_repeated_run_counts_as_updated(
        self, scheduler: IngestionScheduler, store: RagStore
    ) -> None:
        await scheduler.run_once()
        result2 = await scheduler.run_once()
        assert result2["updated"] >= 1

    async def test_gmail_error_counted_not_raised(
        self, settings: Settings, store: RagStore
    ) -> None:
        gmail = AsyncMock(spec=GmailClient)
        gmail.search_threads.return_value = [{"id": "t1"}]
        gmail.get_thread.side_effect      = GmailError("network error")
        sched = IngestionScheduler(settings, gmail, store)
        result = await sched.run_once()
        assert result["errors"] == 1

    async def test_search_failure_returns_error_result(
        self, settings: Settings, store: RagStore
    ) -> None:
        gmail = AsyncMock(spec=GmailClient)
        gmail.search_threads.side_effect = GmailError("auth failed")
        sched  = IngestionScheduler(settings, gmail, store)
        result = await sched.run_once()
        assert result["errors"]  == 1
        assert result["fetched"] == 0

    async def test_last_run_set_after_run(
        self, scheduler: IngestionScheduler
    ) -> None:
        assert scheduler.last_run is None
        await scheduler.run_once()
        assert scheduler.last_run is not None

    async def test_last_result_set_after_run(
        self, scheduler: IngestionScheduler
    ) -> None:
        await scheduler.run_once()
        assert scheduler.last_result != {}

    async def test_start_disabled_when_scheduler_disabled(
        self, settings: Settings, mock_gmail: AsyncMock, store: RagStore
    ) -> None:
        s = Settings(scheduler_enabled=False, rag_db_path=":memory:")
        sched = IngestionScheduler(s, mock_gmail, store)
        await sched.start()   # should be a no-op
        assert not sched._scheduler.running

    async def test_start_and_stop_with_interval_trigger(
        self, settings: Settings, mock_gmail: AsyncMock, store: RagStore
    ) -> None:
        """Scheduler should start cleanly, register the job, and stop without error."""
        s = Settings(
            scheduler_enabled=True,
            ingest_cron="",
            ingest_interval_minutes=60,
            rag_db_path=":memory:",
        )
        sched = IngestionScheduler(s, mock_gmail, store)
        with patch.object(sched, "_ingest_job", new_callable=AsyncMock):
            await sched.start()
            # Job must be registered while the scheduler is running.
            assert sched._scheduler.get_job(IngestionScheduler.JOB_ID) is not None
            # stop() must not raise — whether APScheduler sets .running=False
            # synchronously depends on the event loop; we only assert no exception.
            await sched.stop()

    async def test_start_with_cron_trigger(
        self, settings: Settings, mock_gmail: AsyncMock, store: RagStore
    ) -> None:
        s = Settings(
            scheduler_enabled=True,
            ingest_cron="0 6 * * *",
            rag_db_path=":memory:",
        )
        sched = IngestionScheduler(s, mock_gmail, store)
        with patch.object(sched, "_ingest_job", new_callable=AsyncMock):
            await sched.start()
            job = sched._scheduler.get_job(IngestionScheduler.JOB_ID)
            assert job is not None
            await sched.stop()
