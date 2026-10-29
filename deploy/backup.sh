#!/usr/bin/env bash
# Make a backup now: a zip of the database (and uploaded files, if configured), AES-256 encrypted if configured.
# Uses [backups] in printpool.toml for everything you don't override here.
#
#   deploy/backup.sh                       into the [backups.location] from printpool.toml
#   deploy/backup.sh --to ~/printpool-bak  into another folder on this machine
#   options: --keep N  --include-files | --no-include-files  --encrypt | --no-encrypt
#
# Needs PrintPool running (deploy/setup-server.sh). To run it on a schedule from cron, see deploy/SCHEDULING.md,
# or let the worker do it ([backups] enabled = true in printpool.toml).
set -euo pipefail
cd "$(dirname "$0")/.."

to=
args=()
while [ $# -gt 0 ]; do
    case $1 in
        --to) to=${2:?--to needs a folder}; shift ;;
        --to=*) to=${1#--to=} ;;
        -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) args+=("$1") ;;
    esac
    shift
done

if [ -z "$to" ]; then
    # The worker already has the configured backup location mounted at /backups.
    docker compose exec -T worker python manage.py backup ${args[@]+"${args[@]}"}
else
    mkdir -p "$to"
    to=$(cd "$to" && pwd)
    docker compose run --rm --no-deps -T -v "$to:/backup-out" worker \
        python manage.py backup --output /backup-out ${args[@]+"${args[@]}"}
fi
