#!/usr/bin/env bash
# Minimal setup for a personal server you look after yourself, behind your own Cloudflare Tunnel.
#
#   deploy/personal-setup.sh print.example.com
#
# Unlike setup-server.sh, this installs nothing and changes nothing outside this folder: no packages, firewall,
# SSH settings, cron or log rotation. It needs Docker (with the compose plugin) already installed, and works
# without root as long as your user can run docker.
#
# It writes .env (on first run: fresh secret key and database password; every run: tunnel settings), then
# starts the containers with the app on 127.0.0.1:8000 for cloudflared to connect to. If
# deploy/cloudflared/config.yml exists, cloudflared is started as a container too (deploy/cloudflared/README.md).
#
# Optional: DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD create the first admin account.
# Safe to re-run, e.g. after changing .env or adding/removing deploy/cloudflared/config.yml.
set -euo pipefail

# Reuse setup-server.sh's helpers (.env generation, health wait); sourcing it doesn't run its setup.
. "$(dirname "${BASH_SOURCE[0]}")/setup-server.sh"
MODE=tunnel
DOMAIN=${1:-}

if [ -z "$DOMAIN" ] || [ "${DOMAIN#-}" != "$DOMAIN" ]; then
    echo "Usage: $0 <domain>    e.g. $0 print.example.com" >&2
    exit 1
fi
if ! docker compose version > /dev/null 2>&1; then
    echo "Docker with the compose plugin is needed (docker compose version failed)." >&2
    exit 1
fi
if ! docker info > /dev/null 2>&1; then
    echo "Can't talk to Docker. Is it running, and can your user use it (or run this with sudo)?" >&2
    exit 1
fi

cd "$REPO_DIR"

say "Writing configuration"
write_app_env
write_jobs_env

use_container=false
if [ -f deploy/cloudflared/config.yml ]; then
    use_container=true
    set_env .env COMPOSE_PROFILES cloudflared
    echo "Found deploy/cloudflared/config.yml: cloudflared will run as a container."
    if grep -Eq 'service:[[:space:]]*https?://(localhost|127\.0\.0\.1)' deploy/cloudflared/config.yml; then
        warn "config.yml points at localhost, which inside the container is cloudflared itself; use http://app:8000."
    fi
    if ! compgen -G "deploy/cloudflared/*.json" > /dev/null; then
        warn "no credentials .json in deploy/cloudflared/; cloudflared won't be able to connect."
    fi
fi

say "Starting PrintPool"
if [ "$use_container" = false ]; then
    # In case an earlier run started the cloudflared container and its config has since been removed.
    docker compose --profile cloudflared rm -sf cloudflared > /dev/null 2>&1 || true
fi
docker compose up -d --build
if ! wait_until_healthy; then
    docker compose logs --tail 30 app
    exit 1
fi

if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    say "Creating the first admin account"
    docker compose exec -T -e DJANGO_SUPERUSER_USERNAME -e DJANGO_SUPERUSER_PASSWORD \
        app python manage.py create_initial_admin
fi

say "Done"
docker compose ps --format '  {{.Service}}: {{.Status}}'
echo
if [ "$use_container" = true ]; then
    echo "cloudflared is running as a container (logs: docker compose logs cloudflared)."
else
    echo "Point your cloudflared ingress for $DOMAIN at http://localhost:8000, then reload cloudflared."
fi
if [ -z "${DJANGO_SUPERUSER_USERNAME:-}" ]; then
    echo "Create your admin account:  cd $REPO_DIR && docker compose exec app python manage.py createsuperuser"
fi
cat <<EOF
Then visit https://$DOMAIN.

Nothing was scheduled. If you want expired files cleaned up and nightly database dumps, add these to your
own crontab (crontab -e), with BACKUP_DIR set to somewhere you can write in deploy/jobs.env:
$(cron_file_contents | grep -E '^[0-9]' | grep -v offsite | sed -E 's/  root  /  /; s|^|  |')
EOF
