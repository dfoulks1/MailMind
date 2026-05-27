# Getting Started

This guide walks you from a fresh checkout to a running MailMind service.

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | Managed with `uv` or `pyenv` |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Ollama | latest | [ollama.com](https://ollama.com) — must be running locally |
| Google account | — | The Gmail inbox you want to index |

---

## Step 1 — Install

```bash
git clone https://github.com/you/mailmind
cd mailmind

# Create a virtual environment and install all dependencies
uv sync
```

If you prefer plain pip:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## Step 2 — Create a Google Cloud project

MailMind accesses Gmail through Google's official REST API using OAuth 2.0.
You need to create credentials once.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and
   create a new project (or select an existing one).

2. Navigate to **APIs & Services → Library**, search for **Gmail API**, and
   click **Enable**.

3. Navigate to **APIs & Services → Credentials** and click
   **Create Credentials → OAuth client ID**.

4. Choose **Desktop app** as the application type. Give it any name.

5. Click **Download JSON**. This file contains your `client_id` and
   `client_secret`.

6. Find the values in the downloaded JSON:
   ```json
   {
     "installed": {
       "client_id": "123456789-abc.apps.googleusercontent.com",
       "client_secret": "GOCSPX-..."
     }
   }
   ```

> **Note on OAuth consent screen:** Google will mark your app as
> "unverified" unless you submit it for review. For personal use this is fine
> — you can add your own Google account as a test user under
> **OAuth consent screen → Test users**.

---

## Step 3 — Configure

```bash
cp .env.example .env
```

Open `.env` and fill in at minimum:

```bash
OAUTH_CLIENT_ID=123456789-abc.apps.googleusercontent.com
OAUTH_CLIENT_SECRET=GOCSPX-your-secret-here
```

All other settings have sensible defaults. See
[configuration.md](configuration.md) for the full reference.

---

## Step 4 — Pull an Ollama model

```bash
# Start Ollama if it is not already running
ollama serve &

# Pull the default model (or any model you prefer)
ollama pull llama3.2:1b
```

If you want to use a different model, set `OLLAMA_MODEL` in `.env` before
running `mailmind`.

---

## Step 5 — Authorise (one-time)

```bash
mailmind auth
```

This prints a URL. Open it in your browser, sign in with your Google account,
grant the requested permissions, and paste the authorisation code back into
the terminal.

```
─── MailMind OAuth Authorization ───────────────────────────
Open this URL in your browser and authorize the application:

  https://accounts.google.com/o/oauth2/v2/auth?...

Paste the authorization code here: 4/0Adeu5BW...

✓ Authorization successful.  Token saved to 'token.json'.
  Access token (first 16 chars): ya29.a0AfH6SM...
```

The token is saved to `token.json` (or the path set in `OAUTH_TOKEN_FILE`).
You will not need to repeat this step unless the token is revoked or deleted.

To confirm which scopes were granted:

```bash
mailmind debug-scopes
```

---

## Step 6 — Start the service

```bash
mailmind
```

Output:

```
INFO  Loaded env file: .env
INFO  RagStore opened at mailmind.db
INFO  Scheduler: cron trigger '0 */6 * * *'
INFO  IngestionScheduler started.
INFO  MailMind service started.
INFO  Uvicorn running on http://127.0.0.1:8765
```

The first ingest run fires immediately on startup. Depending on the size of
your inbox and the `GMAIL_MAX_RESULTS` setting, it may take a few minutes.
You can watch progress in the log output.

---

## Step 7 — Verify

```bash
# Check liveness
curl http://127.0.0.1:8765/health
# {"status": "ok"}

# Check ingest status and store size
curl http://127.0.0.1:8765/status | python -m json.tool

# Run a test query
curl -s http://127.0.0.1:8765/query \
  -H "Content-Type: application/json" \
  -d '{"query": "invoice payment", "top_k": 3}' \
  | python -m json.tool
```

The interactive API explorer is available at
`http://127.0.0.1:8765/docs`.

---

## One-shot query mode (no service required)

If you just want to query the local store from the command line without
starting the HTTP server:

```bash
mailmind query "AWS invoice March" --top-k 5
```

Prints JSON to stdout.

---

## Keeping the service running

For persistent background operation, see [deployment.md](deployment.md) for
systemd and macOS launchd setup instructions.
