# gmail-mcp-server

A production-ready **Model Context Protocol (MCP) Server** that connects to the Gmail API, exposing an inbox summarization tool that any MCP-compatible LLM client (Ollama, Claude Desktop, etc.) can call.

---

## Features

- 📬 **`summarize_inbox` tool** — aggregates emails from the last N days into a structured JSON report
- 🧵 **Thread clustering** — groups similar subjects together to reduce noise
- 🚨 **Urgency detection** — flags emails with keywords like "action required", "deadline", etc.
- ✅ **Actionable item extraction** — surfaces emails that need a reply or decision
- 🔒 **Read-only OAuth2** — uses `gmail.readonly` scope; never modifies your inbox
- 🛡️ **Rate limiting** — prevents Gmail API quota exhaustion
- 🤐 **Privacy-first** — email body content is never logged or written to stdout

---

## Prerequisites

| Requirement | Version |
|---|---|
| Node.js | ≥ 18.0.0 |
| A Google account | — |
| Google Cloud project with Gmail API enabled | — |

---

## 1. Google Cloud Setup (one-time)

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or select an existing one).
3. Enable the **Gmail API**: *APIs & Services → Library → Gmail API → Enable*.
4. Create credentials: *APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID*.
   - Application type: **Desktop app**
   - Name: `gmail-mcp-server` (or anything)
5. Download the credentials JSON — you only need `client_id` and `client_secret`.
6. Under *OAuth consent screen*, add your Google account as a **Test user** (required while the app is in "Testing" status).

---

## 2. Installation

```bash
git clone https://github.com/dfoulks1/gmail-analyzer.git
cd gmail-analyzer
npm install
```

---

## 3. Configuration

```bash
cp .env.example .env
```

Edit `.env`:

```env
GMAIL_CLIENT_ID=your-client-id.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=your-client-secret
GMAIL_REFRESH_TOKEN=           # leave blank for now — filled in step 4
```

---

## 4. Generate a Refresh Token (one-time)

```bash
npm run auth
```

This will:
1. Print an authorization URL — open it in your browser.
2. Sign in with your Google account and grant Gmail read access.
3. Paste the returned code back into the terminal.
4. Print your `GMAIL_REFRESH_TOKEN` — copy it into `.env`.

---

## 5. Start the Server

```bash
npm start
```

The server listens on **stdio** (stdin/stdout) — this is the MCP standard transport. All operational logs go to **stderr** so they never interfere with the protocol.

---

## 6. Connect to an LLM Client

### Ollama (via Open WebUI or compatible client)

Add to your MCP client configuration:

```json
{
  "mcpServers": {
    "gmail": {
      "command": "node",
      "args": ["/absolute/path/to/gmail-analyzer/src/mcp-server.js"],
      "env": {
        "GMAIL_CLIENT_ID": "...",
        "GMAIL_CLIENT_SECRET": "...",
        "GMAIL_REFRESH_TOKEN": "..."
      }
    }
  }
}
```

> **Tip:** Use absolute paths. Relative paths depend on the working directory of the MCP host, which varies.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your OS:

```json
{
  "mcpServers": {
    "gmail": {
      "command": "node",
      "args": ["/absolute/path/to/gmail-analyzer/src/mcp-server.js"]
    }
  }
}
```

Restart Claude Desktop. The `summarize_inbox` tool will appear in the tool list.

---

## Tool Reference

### `summarize_inbox`

Aggregates recent emails into a structured summary report.

**Arguments:**

| Argument | Type | Default | Description |
|---|---|---|---|
| `daysBack` | number | `1` | How many days back to scan (1–30) |
| `priority` | string | `"all"` | `"all"` \| `"unread"` \| `"high"` (Important/Starred) |

**Example prompt to your LLM:**
> "Use the summarize_inbox tool with daysBack=3 and priority=unread, then give me a concise overview of what needs my attention."

**Sample output (truncated):**

```json
{
  "generatedAt": "2024-01-15T10:30:00.000Z",
  "daysBack": 1,
  "priority": "all",
  "totalEmails": 42,
  "unreadCount": 12,
  "urgentItems": [
    {
      "subject": "Action Required: Renew your subscription",
      "from": "billing@example.com",
      "date": "Mon, 15 Jan 2024 09:00:00 +0000",
      "isUnread": true,
      "preview": "Your subscription expires in 3 days..."
    }
  ],
  "threadClusters": [
    {
      "topic": "weekly team standup",
      "count": 5,
      "senders": ["Alice", "Bob", "Carol"],
      "hasUrgent": false,
      "hasActionable": true
    }
  ],
  "summary": "Found 42 email(s) over the last 1 day(s). 12 unread, 3 urgent, 8 actionable."
}
```

---

## Security Notes

- The server uses **`gmail.readonly`** scope only — it cannot send, delete, or modify emails.
- Email **body content** is never fetched, stored, or logged — only subject, sender, date, and Gmail's auto-generated snippet (≤200 chars) are used.
- Credentials are read from environment variables — never hardcoded.
- `.env` and `config.json` are excluded from git via `.gitignore`.

---

## Rate Limiting

Default: **20 API calls per 10 seconds** (well within Gmail's 250 quota units/second free tier).

To adjust, edit the `RateLimiter` instantiation in `src/mcp-server.js`:

```js
const rateLimiter = new RateLimiter({ maxRequests: 30, windowMs: 10_000 });
```

---

## Project Structure

```
gmail-analyzer/
├── src/
│   ├── mcp-server.js          # MCP server entrypoint + tool registry
│   ├── auth/
│   │   ├── oauth.js            # OAuth2 client builder
│   │   └── get-refresh-token.js # One-time token generator CLI
│   ├── gmail/
│   │   ├── fetcher.js          # Gmail API calls (metadata only)
│   │   └── summarizer.js       # Report builder + clustering logic
│   └── utils/
│       └── rate-limiter.js     # Sliding-window rate limiter
├── .env.example                # Environment variable template
├── .gitignore
├── config.example.json         # Optional user settings
└── package.json
```

---

To run with ollmcp use `--mcp-server /path/to/gmail-analyzer/src/mcp-server.js`

## License

MIT
