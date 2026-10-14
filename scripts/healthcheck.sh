#!/usr/bin/env bash
# Calls /health and logs a line on failure.
# Cron (every minute):  * * * * * /opt/server-pulse/scripts/healthcheck.sh
set -u

URL="${HEALTH_URL:-http://localhost/health}"
LOG="${HEALTH_LOG:-$HOME/healthcheck.log}"

body=$(curl -fsS --max-time 5 "$URL" 2>&1)
status=$?

if [ "$status" -ne 0 ] || [ "$body" != "ok" ]; then
  echo "$(date -u +%FT%TZ) FAIL url=$URL curl_exit=$status body=${body:0:80}" >> "$LOG"
  exit 1
fi
