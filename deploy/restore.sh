#!/usr/bin/env bash
# Restore a backup made by backup.sh or the worker. This REPLACES the current database (and uploaded files, if the
# backup has them). The app and worker are stopped while it runs, then started again.
#
#   deploy/restore.sh backups/printpool-20261029-033000.zip
#   deploy/restore.sh <zip> --no-files    restore only the database
#
# The password comes from BACKUP_PASSWORD in .env. For a backup made with a different password, set it for this
# run: BACKUP_PASSWORD='...' deploy/restore.sh <zip>   (or leave it empty in .env to be asked).
set -euo pipefail
cd "$(dirname "$0")/.."

[ $# -ge 1 ] || { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
zip=$1; shift
[ -f "$zip" ] || { echo "error: $zip not found" >&2; exit 1; }
folder=$(cd "$(dirname "$zip")" && pwd)
name=$(basename "$zip")

printf 'This replaces everything in the PrintPool database with %s.\nType "restore" to continue: ' "$name"
read -r answer
[ "$answer" = restore ] || { echo "Cancelled."; exit 1; }

password_env=()
[ -n "${BACKUP_PASSWORD:-}" ] && password_env=(-e BACKUP_PASSWORD)

docker compose up -d db
docker compose stop app worker
status=0
docker compose run --rm --no-deps ${password_env[@]+"${password_env[@]}"} -v "$folder:/restore:ro" worker \
    python manage.py restore_backup "/restore/$name" --yes "$@" || status=$?
docker compose start app worker
exit $status
