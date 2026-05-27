"""
mailmind.scheduler — background Gmail ingestion scheduler.

``IngestionScheduler`` wraps APScheduler's ``AsyncIOScheduler`` to run
scheduled ingest-and-index jobs without a message broker or external process.

Schedule modes
--------------
**Cron** (``INGEST_CRON`` is set in ``.env``):
    Full cron expression, e.g. ``"0 */6 * * *"`` (every 6 hours).
    Evaluated by APScheduler's ``CronTrigger``.

**Interval** (fallback when ``INGEST_CRON`` is empty):
    Runs every ``INGEST_INTERVAL_MINUTES`` minutes (default: 360 = 6 h).
    Also fires once at service startup so the store is populated immediately.

What one ingest run does
------------------------
1. Call ``GmailClient.search_threads`` with the configured default query.
2. For each thread stub, fetch the full thread.
3. Extract message bodies into ``ingest_email`` records.
4. Persist to ``RagStore``.
5. Log a summary (new / updated counts, elapsed time).

The scheduler holds no persistent state between runs; it is safe to restart
the service at any time.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from mailmind.config import Settings
from mailmind.gmail import GmailClient, ThreadParser
from mailmind.models import GmailError, OAuthError
from mailmind.rag import RagStore

log = logging.getLogger(__name__)


class IngestionScheduler:
    """
    Background Gmail → RAG ingest scheduler.

    Lifecycle::

        scheduler = IngestionScheduler(settings, gmail_client, rag_store)
        await scheduler.start()      # begins background scheduling
        ...                          # service runs
        await scheduler.stop()       # drains pending jobs, shuts down

    The scheduler shares the ``GmailClient`` and ``RagStore`` instances with
    the HTTP service layer so there is exactly one live Gmail session and one
    open database connection per service process.

    Manual trigger
    --------------
    Call ``await scheduler.run_once()`` to trigger an immediate ingest outside
    the scheduled cadence (e.g. from the ``POST /ingest`` HTTP endpoint).
    """

    JOB_ID = "gmail_ingest"

    def __init__(
        self,
        settings:  Settings,
        gmail:     GmailClient,
        rag_store: RagStore,
    ) -> None:
        self._settings  = settings
        self._gmail     = gmail
        self._rag_store = rag_store
        self._scheduler = AsyncIOScheduler()
        self._last_run:    datetime | None = None
        self._last_result: dict[str, Any]  = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Register the ingest job and start the scheduler.

        If ``ingest_cron`` is set in ``Settings``, a ``CronTrigger`` is used.
        Otherwise an ``IntervalTrigger`` fires every ``ingest_interval_minutes``
        minutes.  A ``next_run_time=now`` argument ensures the first run happens
        immediately on startup rather than waiting for the first tick.
        """
        if not self._settings.scheduler_enabled:
            log.info("Scheduler disabled via SCHEDULER_ENABLED=false.")
            return

        if self._settings.ingest_cron:
            parts   = self._settings.ingest_cron.split()
            trigger = CronTrigger(
                minute      = parts[0] if len(parts) > 0 else "*",
                hour        = parts[1] if len(parts) > 1 else "*",
                day         = parts[2] if len(parts) > 2 else "*",
                month       = parts[3] if len(parts) > 3 else "*",
                day_of_week = parts[4] if len(parts) > 4 else "*",
            )
            log.info("Scheduler: cron trigger %r", self._settings.ingest_cron)
        else:
            trigger = IntervalTrigger(minutes=self._settings.ingest_interval_minutes)
            log.info(
                "Scheduler: interval trigger every %d minutes",
                self._settings.ingest_interval_minutes,
            )

        self._scheduler.add_job(
            self._ingest_job,
            trigger       = trigger,
            id            = self.JOB_ID,
            name          = "Gmail → RAG ingest",
            replace_existing = True,
            next_run_time = datetime.now(UTC),   # fire immediately on startup
        )
        self._scheduler.start()
        log.info("IngestionScheduler started.")

    async def stop(self) -> None:
        """Gracefully shut down the scheduler, waiting for running jobs to finish."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
            log.info("IngestionScheduler stopped.")

    # ── public API ────────────────────────────────────────────────────────────

    async def run_once(self) -> dict[str, Any]:
        """
        Trigger an immediate ingest run outside the scheduled cadence.

        Returns:
            The same result dict that the scheduled job produces::

                {
                  "started_at":  ISO8601 string,
                  "finished_at": ISO8601 string,
                  "elapsed_s":   float,
                  "fetched":     int,
                  "new":         int,
                  "updated":     int,
                  "errors":      int,
                  "query":       str,
                }
        """
        return await self._ingest_job()

    @property
    def last_run(self) -> datetime | None:
        """Timestamp of the most recent completed ingest run, or ``None``."""
        return self._last_run

    @property
    def last_result(self) -> dict[str, Any]:
        """Result dict from the most recent completed run, or ``{}``."""
        return self._last_result

    def next_run_time(self) -> datetime | None:
        """Return the scheduled next run time, or ``None`` if not scheduled."""
        job = self._scheduler.get_job(self.JOB_ID)
        return job.next_run_time if job else None

    # ── internal job ─────────────────────────────────────────────────────────

    async def _ingest_job(self) -> dict[str, Any]:
        """
        Core ingest-and-index routine executed on each scheduled tick.

        Fetches threads matching the default query, parses each message into
        an ingest record, and persists it to the RAG store.  Individual thread
        failures are caught and counted; they do not abort the batch.

        Returns:
            Summary dict (see :meth:`run_once` docstring for shape).
        """
        query      = self._settings.gmail_default_query
        started_at = datetime.now(UTC)
        t0         = time.monotonic()

        log.info("Ingest job starting — query: %r", query)

        fetched = new = updated = errors = 0

        try:
            stubs = await self._gmail.search_threads(query)
        except (GmailError, OAuthError) as exc:
            log.error("Ingest job: failed to search threads — %s", exc)
            result = self._make_result(started_at, t0, 0, 0, 0, 1, query)
            self._record_result(result)
            return result

        for stub in stubs:
            tid = stub.get("id", "")
            if not tid:
                continue
            try:
                raw    = await self._gmail.get_thread(tid)
                thread = ThreadParser.parse_thread(raw)
                for msg in thread.messages:
                    record = {
                        "id":       msg.message_id,
                        "threadId": msg.thread_id,
                        "headers":  msg.raw_headers,
                        "body":     msg.body,
                    }
                    is_new = self._rag_store.ingest_email(record)
                    fetched += 1
                    if is_new:
                        new += 1
                    else:
                        updated += 1
            except (GmailError, OAuthError) as exc:
                log.warning("Ingest job: skipping thread %s — %s", tid, exc)
                errors += 1

        result = self._make_result(started_at, t0, fetched, new, updated, errors, query)
        self._record_result(result)
        log.info(
            "Ingest job complete — fetched=%d new=%d updated=%d errors=%d elapsed=%.1fs",
            fetched, new, updated, errors, result["elapsed_s"],
        )
        return result

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _make_result(
        started_at: datetime,
        t0: float,
        fetched: int,
        new: int,
        updated: int,
        errors: int,
        query: str,
    ) -> dict[str, Any]:
        finished_at = datetime.now(UTC)
        return {
            "started_at":  started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "elapsed_s":   round(time.monotonic() - t0, 2),
            "fetched":     fetched,
            "new":         new,
            "updated":     updated,
            "errors":      errors,
            "query":       query,
        }

    def _record_result(self, result: dict[str, Any]) -> None:
        self._last_run    = datetime.now(UTC)
        self._last_result = result
