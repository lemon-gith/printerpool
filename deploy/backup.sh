#!/bin/sh
# Nightly database backup: dumps Postgres to $BACKUP_DIR on this server and prunes old dumps.
#
# Run from cron on the host (see deploy/crontab.example). Settings come from deploy/jobs.env.
# Only the database is backed up: uploaded model files expire within a month anyway.
set -eu

cd "$(dirname "$0")/.."
. deploy/monitor.sh
monitor_job "${BACKUP_PING_URL:-}"

BACKUP_DIR=${BACKUP_DIR:-/var/backups/printpool}
BACKUP_KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

name="printpool-$(date -u +%Y%m%dT%H%M%SZ).dump"
tmp="$BACKUP_DIR/.$name.partial"

# Custom format (-Fc) is compressed and restores with pg_restore. Credentials come from the db container's env.
# Check the dump is readable before keeping it, so a truncated file never replaces a good one.
if ! docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$tmp" \
    || ! docker compose exec -T db pg_restore --list < "$tmp" > /dev/null; then
    rm -f "$tmp"
    echo "$(date -u +%FT%TZ) backup FAILED" >&2
    exit 1
fi

chmod 600 "$tmp"
mv "$tmp" "$BACKUP_DIR/$name"
(cd "$BACKUP_DIR" && sha256sum "$name" > "$name.sha256")

find "$BACKUP_DIR" -name 'printpool-*.dump*' -type f -mtime +"$BACKUP_KEEP_DAYS" -delete

echo "$(date -u +%FT%TZ) backup ok: $BACKUP_DIR/$name ($(du -h "$BACKUP_DIR/$name" | cut -f1))"
