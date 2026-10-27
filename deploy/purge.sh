#!/bin/sh
# Hourly: delete stored model files whose requests have expired (see README, "File expiry").
# Run from cron on the host (see deploy/crontab.example). Settings come from deploy/jobs.env.
set -eu

cd "$(dirname "$0")/.."
. deploy/monitor.sh
monitor_job "${PURGE_PING_URL:-}"

docker compose exec -T app python manage.py purge_expired_files
