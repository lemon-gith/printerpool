#!/bin/sh
# Weekly off-site copy: sends the newest local dump (from backup.sh) to another machine.
#
# Two transports, use either or both (configure in deploy/jobs.env):
#   SSH   - OFFSITE_SSH_TARGET=user@host:/path   + OFFSITE_SSH_KEY=/path/to/private_key
#   HTTPS - OFFSITE_HTTP_URL=https://host/upload  + OFFSITE_API_KEY=...   (file is PUT to $URL/<name>)
set -eu

cd "$(dirname "$0")/.."
. deploy/monitor.sh
monitor_job "${OFFSITE_PING_URL:-}"

BACKUP_DIR=${BACKUP_DIR:-/var/backups/printpool}
OFFSITE_SSH_TARGET=${OFFSITE_SSH_TARGET:-}
OFFSITE_SSH_KEY=${OFFSITE_SSH_KEY:-}
OFFSITE_SSH_PORT=${OFFSITE_SSH_PORT:-22}
OFFSITE_HTTP_URL=${OFFSITE_HTTP_URL:-}
OFFSITE_API_KEY=${OFFSITE_API_KEY:-}

if [ -z "$OFFSITE_SSH_TARGET" ] && [ -z "$OFFSITE_HTTP_URL" ]; then
    echo "offsite: set OFFSITE_SSH_TARGET and/or OFFSITE_HTTP_URL in deploy/jobs.env" >&2
    exit 1
fi

latest=$(ls -1t "$BACKUP_DIR"/printpool-*.dump 2>/dev/null | head -n 1)
if [ -z "$latest" ]; then
    echo "offsite: no dumps found in $BACKUP_DIR (has backup.sh run?)" >&2
    exit 1
fi
name=$(basename "$latest")

# Re-verify before sending, so a file corrupted on disk isn't shipped as the off-site copy.
(cd "$BACKUP_DIR" && sha256sum -c --quiet "$name.sha256")

if [ -n "$OFFSITE_SSH_TARGET" ]; then
    : "${OFFSITE_SSH_KEY:?offsite: OFFSITE_SSH_KEY must point at the private key}"
    # BatchMode: never prompt for a password. accept-new: trust the host on first contact, then pin it.
    scp -q -i "$OFFSITE_SSH_KEY" -P "$OFFSITE_SSH_PORT" \
        -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        "$latest" "$latest.sha256" "$OFFSITE_SSH_TARGET/"
    echo "$(date -u +%FT%TZ) offsite ok (ssh): $name -> $OFFSITE_SSH_TARGET"
fi

if [ -n "$OFFSITE_HTTP_URL" ]; then
    : "${OFFSITE_API_KEY:?offsite: OFFSITE_API_KEY must be set for HTTP uploads}"
    for f in "$latest" "$latest.sha256"; do
        # Key is passed via a header file on stdin so it never appears in the process list.
        printf 'Authorization: Bearer %s\n' "$OFFSITE_API_KEY" | curl --fail --silent --show-error \
            --retry 3 --retry-delay 10 -H @- --upload-file "$f" "${OFFSITE_HTTP_URL%/}/$(basename "$f")" > /dev/null
    done
    echo "$(date -u +%FT%TZ) offsite ok (https): $name -> $OFFSITE_HTTP_URL"
fi
