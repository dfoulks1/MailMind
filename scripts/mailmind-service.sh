#!/usr/bin/env bash
# mailmind-service.sh — wrapper script for running MailMind as a system service.
#
# Usage:
#   ./scripts/mailmind-service.sh [--env-file /path/to/.env]
#
# Systemd unit example (save to /etc/systemd/system/mailmind.service):
#
#   [Unit]
#   Description=MailMind background Gmail RAG service
#   After=network-online.target
#   Wants=network-online.target
#
#   [Service]
#   Type=simple
#   User=<your-user>
#   WorkingDirectory=/opt/mailmind
#   ExecStart=/opt/mailmind/scripts/mailmind-service.sh --env-file /opt/mailmind/.env
#   Restart=on-failure
#   RestartSec=10
#   StandardOutput=journal
#   StandardError=journal
#
#   [Install]
#   WantedBy=multi-user.target
#
# After saving, enable with:
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now mailmind
#
# macOS launchd plist example (~Library/LaunchAgents/com.mailmind.plist):
#
#   <?xml version="1.0" encoding="UTF-8"?>
#   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
#   <plist version="1.0"><dict>
#     <key>Label</key>         <string>com.mailmind</string>
#     <key>ProgramArguments</key>
#     <array>
#       <string>/opt/mailmind/scripts/mailmind-service.sh</string>
#       <string>--env-file</string>
#       <string>/opt/mailmind/.env</string>
#     </array>
#     <key>RunAtLoad</key>     <true/>
#     <key>KeepAlive</key>     <true/>
#     <key>WorkingDirectory</key> <string>/opt/mailmind</string>
#     <key>StandardOutPath</key>  <string>/tmp/mailmind.out</string>
#     <key>StandardErrorPath</key> <string>/tmp/mailmind.err</string>
#   </dict></plist>
#
# Load with:
#   launchctl load ~/Library/LaunchAgents/com.mailmind.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

ENV_FILE="${REPO_ROOT}/.env"

# Parse --env-file flag.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file)
            ENV_FILE="$2"
            shift 2
            ;;
        *)
            echo "Unknown flag: $1" >&2
            exit 1
            ;;
    esac
done

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file not found: $ENV_FILE" >&2
    exit 1
fi

# Activate uv-managed virtual environment if present.
UV_VENV="${REPO_ROOT}/.venv"
if [[ -d "$UV_VENV" ]]; then
    # shellcheck source=/dev/null
    source "${UV_VENV}/bin/activate"
fi

echo "Starting MailMind service (env: $ENV_FILE)..."
exec mailmind --env-file "$ENV_FILE"
