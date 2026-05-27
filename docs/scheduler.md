# Scheduler

MailMind uses [APScheduler](https://apscheduler.readthedocs.io/) to run a
background Gmail ingest job at a configurable cadence. No message broker,
Redis instance, or separate worker process is required — the scheduler runs
on the same asyncio event loop as the HTTP server.

---

## How it works

When `MailMindService.start()` is called:

1. `IngestionScheduler.start()` registers a single job with APScheduler.
2. The job is set to fire **immediately** (`next_run_time=now`) so the store
   is populated on first startup without waiting for the first scheduled tick.
3. After that, the job fires according to the configured trigger.

On each run the job:

1. Calls `GmailClient.search_threads(default_query)` to get a list of thread stubs.
2. Fetches each full thread.
3. Passes every individual message to `RagStore.ingest_email()`.
4. Returns a summary dict with counts of new, updated, and errored records.

Individual thread failures are caught and counted — they do not abort the
rest of the batch.

---

## Schedule modes

### Cron trigger (recommended)

Set `INGEST_CRON` to a standard five-field cron expression.
This takes precedence over `INGEST_INTERVAL_MINUTES` when both are set.

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-6, Sun=0)
│ │ │ │ │
* * * * *
```

**Examples**

| Expression | Meaning |
|------------|---------|
| `0 */6 * * *` | Every 6 hours (00:00, 06:00, 12:00, 18:00) |
| `0 7 * * *` | Every day at 07:00 |
| `0 7 * * 1-5` | Weekdays at 07:00 |
| `*/30 * * * *` | Every 30 minutes |
| `0 7,12,18 * * *` | Three times a day: 07:00, 12:00, 18:00 |
| `0 0 1 * *` | First day of every month at midnight |

```bash
# .env
INGEST_CRON=0 7 * * 1-5
```

### Interval trigger (fallback)

When `INGEST_CRON` is empty, the scheduler fires every
`INGEST_INTERVAL_MINUTES` minutes.

```bash
# .env
INGEST_CRON=                  # empty — use interval mode
INGEST_INTERVAL_MINUTES=120   # every 2 hours
```

### Disabling the scheduler

Set `SCHEDULER_ENABLED=false` to run MailMind as a pure query service
without any background ingest. You can still trigger ingestion manually
via `POST /ingest` or `mailmind query`.

```bash
# .env
SCHEDULER_ENABLED=false
```

Or with a CLI flag (does not persist):

```bash
mailmind --no-scheduler
```

---

## Manual trigger

An immediate out-of-schedule ingest can be triggered without restarting the
service:

```bash
# Trigger the default query
curl -X POST http://127.0.0.1:8765/ingest \
  -H "Content-Type: application/json" \
  -d '{}'

# Trigger a one-off query override
curl -X POST http://127.0.0.1:8765/ingest \
  -H "Content-Type: application/json" \
  -d '{"query": "from:finance@company.com after:2024-06-01"}'
```

The response blocks until the run completes and returns the full summary.
The `query` override applies only to that single run; the configured default
is restored immediately afterwards.

---

## Tuning ingest performance

### Reduce per-run time

- Tighten `GMAIL_DEFAULT_QUERY` to fetch fewer threads.
  Add a date filter: `after:2024-01-01` or `newer_than:30d`.
- Reduce `GMAIL_MAX_RESULTS` (default: `200`).

### Reduce quota usage

The Gmail API has a default quota of 1,000,000,000 units/day with read
operations costing 5 units each. For a typical inbox the default settings
are well within limits. If you index a very large mailbox frequently, consider:

- Narrowing `GMAIL_DEFAULT_QUERY` to unread messages only: `is:unread`
- Running ingest less frequently (daily instead of every 6 hours)

### First ingest of a large inbox

On first startup, all matching threads are fetched. For large inboxes this
can take several minutes. The ingest is resumable — if the service is
restarted, already-indexed emails are updated (not duplicated), and only
new ones are inserted.

---

## Observing the scheduler

The `/status` endpoint reports when the last ingest ran and when the next
one is scheduled:

```bash
curl -s http://127.0.0.1:8765/status | python -m json.tool
```

```json
{
  "last_ingest": {
    "started_at":  "2024-06-01T07:00:02Z",
    "finished_at": "2024-06-01T07:01:45Z",
    "elapsed_s":   103.4,
    "fetched":     87,
    "new":         12,
    "updated":     75,
    "errors":      0,
    "query":       "category:inbox -category:trash"
  },
  "next_ingest": "2024-06-02T07:00:00Z"
}
```

The service log also emits a summary line at `INFO` level after each run:

```
INFO  Ingest job complete — fetched=87 new=12 updated=75 errors=0 elapsed=103.4s
```
