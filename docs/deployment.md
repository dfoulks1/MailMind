# Deployment

This page covers running MailMind as a persistent background service on
Linux (systemd) and macOS (launchd), plus Docker, and security hardening
recommendations.

---

## Linux — systemd

### 1. Install the package

```bash
# Create a dedicated directory
sudo mkdir -p /opt/mailmind
sudo chown $USER /opt/mailmind

# Copy the project
cp -r . /opt/mailmind/
cd /opt/mailmind

# Create a virtual environment with uv
uv sync
```

### 2. Configure

```bash
cp .env.example .env
# Fill in OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, and any other settings
chmod 600 .env

# Run the one-time OAuth flow (requires an interactive terminal)
/opt/mailmind/.venv/bin/mailmind auth
chmod 600 token.json
```

### 3. Create the systemd unit

Save to `/etc/systemd/system/mailmind.service`:

```ini
[Unit]
Description=MailMind background Gmail RAG service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=alice
Group=alice
WorkingDirectory=/opt/mailmind

# The shell wrapper activates the venv and sets --env-file
ExecStart=/opt/mailmind/scripts/mailmind-service.sh --env-file /opt/mailmind/.env

# Restart automatically on crash; wait 10 s before retrying
Restart=on-failure
RestartSec=10

# Write logs to the systemd journal
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mailmind

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/mailmind

[Install]
WantedBy=multi-user.target
```

### 4. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mailmind
```

### 5. Check status and logs

```bash
# Service status
sudo systemctl status mailmind

# Live log stream
sudo journalctl -fu mailmind

# Last 100 lines
sudo journalctl -u mailmind -n 100
```

### Useful commands

```bash
# Restart after config change
sudo systemctl restart mailmind

# Stop the service
sudo systemctl stop mailmind

# Disable auto-start
sudo systemctl disable mailmind
```

---

## macOS — launchd

### 1. Install

```bash
mkdir -p ~/Applications/mailmind
cp -r /path/to/mailmind ~/Applications/mailmind/
cd ~/Applications/mailmind
uv sync
cp .env.example .env
# Fill in credentials
mailmind auth
```

### 2. Create the plist

Save to `~/Library/LaunchAgents/com.mailmind.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mailmind</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/alice/Applications/mailmind/scripts/mailmind-service.sh</string>
    <string>--env-file</string>
    <string>/Users/alice/Applications/mailmind/.env</string>
  </array>

  <!-- Start immediately when loaded and restart if it exits -->
  <key>RunAtLoad</key>  <true/>
  <key>KeepAlive</key>  <true/>

  <key>WorkingDirectory</key>
  <string>/Users/alice/Applications/mailmind</string>

  <!-- Log files -->
  <key>StandardOutPath</key>
  <string>/Users/alice/Library/Logs/mailmind.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/alice/Library/Logs/mailmind.log</string>
</dict>
</plist>
```

### 3. Load the agent

```bash
launchctl load ~/Library/LaunchAgents/com.mailmind.plist
```

### 4. Check status and logs

```bash
# Status
launchctl list | grep mailmind

# Logs
tail -f ~/Library/Logs/mailmind.log
```

### Useful commands

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.mailmind.plist

# Restart
launchctl unload ~/Library/LaunchAgents/com.mailmind.plist
launchctl load   ~/Library/LaunchAgents/com.mailmind.plist
```

---

## Docker

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Install dependencies
COPY pyproject.toml .
RUN uv sync --no-dev

# Copy source
COPY mailmind/ mailmind/
COPY scripts/  scripts/

# Non-root user
RUN useradd -m mailmind && chown -R mailmind /app
USER mailmind

EXPOSE 8765

CMD ["uv", "run", "mailmind", "--host", "0.0.0.0"]
```

### docker-compose.yml

```yaml
services:
  mailmind:
    build: .
    restart: unless-stopped
    ports:
      - "127.0.0.1:8765:8765"   # bind to localhost only
    volumes:
      - ./token.json:/app/token.json:ro
      - mailmind-db:/app/data
    env_file: .env
    environment:
      RAG_DB_PATH: /app/data/mailmind.db
      SERVICE_HOST: "0.0.0.0"   # bind inside container

volumes:
  mailmind-db:
```

### Running with Docker

```bash
# First: run auth interactively (requires TTY)
docker run -it --rm \
  -v $(pwd)/token.json:/app/token.json \
  --env-file .env \
  mailmind mailmind auth

# Then start the service
docker compose up -d

# Logs
docker compose logs -f mailmind
```

> **Important:** The OAuth flow requires an interactive terminal. Run
> `mailmind auth` on the host before using Docker, then mount
> `token.json` into the container as shown above.

---

## Security hardening

### File permissions

```bash
# Credentials must not be world-readable
chmod 600 .env token.json
chmod 700 /opt/mailmind
```

### Network binding

The default `SERVICE_HOST=127.0.0.1` restricts the API to localhost.
Only change this if you genuinely need remote access, and if you do, add
authentication (an API gateway, nginx with auth, or a VPN).

### Token rotation

OAuth access tokens expire after 1 hour. MailMind refreshes them
automatically using the stored refresh token (RFC 6749 §6). The refresh
token itself does not expire unless you revoke it in your Google account's
[security settings](https://myaccount.google.com/security).

To force re-authorisation:

```bash
rm token.json
mailmind auth
```

### Principle of least privilege

MailMind requests `gmail.readonly` and `gmail.compose` scopes. If you do
not use the draft creation feature, remove `gmail.compose` from
`oauth_scopes` in `config.py` before the first `mailmind auth` run.

The systemd unit above includes several hardening directives:
- `NoNewPrivileges=true` — prevents privilege escalation
- `PrivateTmp=true` — isolated `/tmp`
- `ProtectSystem=strict` — read-only filesystem except `ReadWritePaths`

### Database backup

`mailmind.db` is a standard SQLite file. Back it up with:

```bash
# Safe hot backup (no service interruption needed)
sqlite3 /opt/mailmind/mailmind.db ".backup '/backup/mailmind-$(date +%Y%m%d).db'"
```

The database can be deleted and rebuilt by running `POST /ingest` — all
content comes from Gmail, which is the source of truth.

---

## Health checks

For load balancers or container orchestrators, use `GET /health`:

```bash
# Returns 200 {"status": "ok"} while the process is running
curl -sf http://127.0.0.1:8765/health
```

Docker health check:

```yaml
healthcheck:
  test: ["CMD", "curl", "-sf", "http://127.0.0.1:8765/health"]
  interval: 30s
  timeout: 5s
  retries: 3
```
