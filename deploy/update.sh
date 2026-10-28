#!/bin/sh
# Deploy the latest version: pull, rebuild, restart. Migrations run automatically when the app starts.
#   sudo /opt/printerpool/deploy/update.sh
set -eu

cd "$(dirname "$0")/.."
git pull --ff-only
docker compose up -d --build  # COMPOSE_PROFILES in .env decides whether Caddy runs
docker image prune -f > /dev/null  # drop the previous build's images so they don't fill the disk
# Pick up any changes to the scheduled jobs.
sed "s|/opt/printerpool|$(pwd)|g" deploy/crontab.example > /etc/cron.d/printpool
docker compose ps
