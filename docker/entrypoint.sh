#!/bin/bash
# entrypoint.sh — Container entrypoint
# Starts nginx, runs initial data update, sets up weekly cron

set -euo pipefail

LOG_FILE="/var/log/llmpricing-update.log"

# Ensure log file exists
touch "$LOG_FILE"

# Run initial data fetch (first boot or restart)
echo "[entrypoint] Running initial data update..."
bash /app/update.sh

# Set up weekly cron: Sunday at 02:00 UTC
echo "[entrypoint] Setting up weekly cron (Sunday 02:00 UTC)..."
echo "0 2 * * 0 root bash /app/update.sh > /var/log/llmpricing-cron.log 2>&1" > /etc/crontabs/root

# Start cron daemon
crond -b -l 2
echo "[entrypoint] Cron daemon started."

# Start nginx in foreground
echo "[entrypoint] Starting nginx..."
nginx -g 'daemon off;'
