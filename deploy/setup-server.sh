#!/usr/bin/env bash
# Deploy or update PrintPool. Everything runs in Docker: this changes nothing on the machine outside this folder.
#
#   cp printpool.example.toml printpool.toml   # first time: then edit it
#   deploy/setup-server.sh                     # first deploy, after editing printpool.toml, and after git pull
#
# Optional, first deploy only: DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD create the first admin
# (who must change that password at first login).
# Needs Docker with the compose plugin, usable by your user (or run with sudo). Windows: deploy/setup-server.ps1.
set -euo pipefail

cd "$(dirname "$0")/.."

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

docker compose version > /dev/null 2>&1 || fail "Docker with the compose plugin is needed (docker compose version failed)."
docker info > /dev/null 2>&1 || fail "can't talk to Docker. Is it running, and can your user use it (or run with sudo)?"
[ -f printpool.toml ] || fail "printpool.toml not found. Copy printpool.example.toml to printpool.toml and edit it first."

say "Checking printpool.toml"
# Rendered in a throwaway container, so the only thing this machine needs is Docker. Run as your user so the
# files it writes (.env, .generated/, compose.override.yaml, the backups folder) stay yours.
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work python:3.13-slim \
    python -I deploy/render_config.py || fail "fix printpool.toml (or .env) and run this again."

say "Building and starting"
docker compose up -d --build --remove-orphans

say "Waiting for everything to be healthy"
for service in app worker gateway; do
    printf '  %-8s' "$service"
    status=
    for _ in $(seq 1 90); do
        id=$(docker compose ps -q "$service")
        status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || true)
        [ "$status" = healthy ] && break
        sleep 2
    done
    if [ "$status" = healthy ]; then
        echo "healthy"
    else
        echo "$status"
        docker compose logs --tail 30 "$service"
        fail "$service didn't become healthy (logs above)."
    fi
done

if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    say "Creating the first admin account"
    docker compose exec -T -e DJANGO_SUPERUSER_USERNAME -e DJANGO_SUPERUSER_PASSWORD \
        app python manage.py create_initial_admin
fi

say "Running"
docker compose ps --format '  {{.Service}}: {{.Status}}'
cat <<'EOF'

Next:
  - First time? Create your admin:  docker compose exec app python manage.py createsuperuser
  - Logs:                           docker compose logs -f
  - Back up now / restore:          deploy/backup.sh   /   deploy/restore.sh <backup.zip>
  - After git pull or editing printpool.toml, run deploy/setup-server.sh again.
EOF
