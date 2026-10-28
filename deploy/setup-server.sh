#!/usr/bin/env bash
# One-time (and safely re-runnable) setup of a fresh Ubuntu 24.04 server for PrintPool.
#
#   git clone https://github.com/lemon-gith/printerpool /opt/printerpool
#   sudo /opt/printerpool/deploy/setup-server.sh print.example.ac.uk
#
# Point the domain's DNS at this server first, so Caddy can get an HTTPS certificate.
# Re-running is safe: packages are updated, and existing .env / jobs.env / keys are never overwritten.
# See docs/DEPLOY.md for the whole checklist, including the steps that happen outside this server.
set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DOMAIN=${1:-}

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
random_secret() { head -c 96 /dev/urandom | base64 | tr -d '/+=\n' | head -c "$1"; }

# Set KEY=value in an env file, replacing the line if it exists and appending it if not.
set_env() {
    local file=$1 key=$2 value=$3
    if grep -q "^${key}=" "$file"; then
        # Use a delimiter that can't appear in our values.
        sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

write_app_env() {
    local env_file=$REPO_DIR/.env
    if [ -f "$env_file" ]; then
        echo "$env_file already exists; leaving it as it is."
        return
    fi
    cp "$REPO_DIR/.env.example" "$env_file"
    chmod 600 "$env_file"
    set_env "$env_file" DJANGO_SECRET_KEY "$(random_secret 64)"
    set_env "$env_file" POSTGRES_PASSWORD "$(random_secret 32)"
    set_env "$env_file" DJANGO_DEBUG 0
    set_env "$env_file" DJANGO_ALLOWED_HOSTS "$DOMAIN"
    set_env "$env_file" DJANGO_CSRF_TRUSTED_ORIGINS "https://$DOMAIN"
    set_env "$env_file" DJANGO_SITE_URL "https://$DOMAIN"
    set_env "$env_file" SITE_DOMAIN "$DOMAIN"
    set_env "$env_file" DJANGO_SECURE 1
    # Only reachable through Caddy, which lets the app trust Caddy's record of visitor IPs.
    set_env "$env_file" APP_PORT 127.0.0.1:8000
    set_env "$env_file" DJANGO_TRUSTED_PROXY_COUNT 1
    echo "Created $env_file with a new secret key and database password."
}

write_jobs_env() {
    local jobs_file=$REPO_DIR/deploy/jobs.env
    if [ -f "$jobs_file" ]; then
        echo "$jobs_file already exists; leaving it as it is."
        return
    fi
    cp "$REPO_DIR/deploy/jobs.env.example" "$jobs_file"
    chmod 600 "$jobs_file"
    echo "Created $jobs_file (fill in the off-site backup and monitoring settings later)."
}

# The crontab template uses /opt/printerpool; point it at wherever the repo actually is.
cron_file_contents() {
    sed "s|/opt/printerpool|$REPO_DIR|g" "$REPO_DIR/deploy/crontab.example"
}

main() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Please run as root (sudo)." >&2
        exit 1
    fi
    if [ -z "$DOMAIN" ]; then
        echo "Usage: $0 <domain>    e.g. $0 print.example.ac.uk" >&2
        exit 1
    fi
    if ! grep -qs 'VERSION_ID="24.04"' /etc/os-release; then
        warn "this script is written for Ubuntu 24.04; continuing, but check each step."
    fi

    say "Updating packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get upgrade -yq
    apt-get install -yq ca-certificates curl git ufw unattended-upgrades

    say "Turning on automatic security updates"
    cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

    say "Installing Docker"
    if ! command -v docker > /dev/null; then
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        . /etc/os-release
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update -q
        apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    else
        echo "Docker is already installed."
    fi
    systemctl enable --now docker

    say "Configuring the firewall (SSH, HTTP, HTTPS only)"
    ufw allow OpenSSH
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw allow 443/udp  # HTTP/3
    ufw --force enable
    # Note: Docker bypasses ufw for published ports. That's fine here because the only ports published are
    # Caddy's 80/443 and the app on 127.0.0.1; also add a Hetzner Cloud Firewall as an outer layer.

    say "Hardening SSH (key logins only)"
    if [ -s /root/.ssh/authorized_keys ] || compgen -G "/home/*/.ssh/authorized_keys" > /dev/null; then
        cat > /etc/ssh/sshd_config.d/10-printpool.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
        if sshd -t; then
            systemctl reload ssh || systemctl reload sshd
            echo "Password logins disabled; SSH keys only."
        else
            rm -f /etc/ssh/sshd_config.d/10-printpool.conf
            warn "SSH config test failed, so SSH was left unchanged."
        fi
    else
        warn "no SSH keys found for any user, so password logins were left on to avoid locking you out."
    fi

    say "Writing configuration"
    write_app_env
    write_jobs_env

    say "Creating the off-site backup key"
    if [ ! -f /root/.ssh/printpool_backup ]; then
        mkdir -p /root/.ssh && chmod 700 /root/.ssh
        ssh-keygen -q -t ed25519 -N "" -C "printpool-backup@$DOMAIN" -f /root/.ssh/printpool_backup
    fi
    echo "Public key for your backup server (see docs/DEPLOY.md):"
    cat /root/.ssh/printpool_backup.pub

    say "Installing scheduled jobs and log rotation"
    cron_file_contents > /etc/cron.d/printpool
    chmod 644 /etc/cron.d/printpool
    cat > /etc/logrotate.d/printpool <<'CONF'
/var/log/printpool-*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
CONF

    say "Starting PrintPool"
    cd "$REPO_DIR"
    docker compose --profile proxy up -d --build
    echo -n "Waiting for the app to report healthy"
    for _ in $(seq 1 60); do
        status=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app)" 2>/dev/null || true)
        [ "$status" = "healthy" ] && break
        echo -n "."
        sleep 2
    done
    echo
    if [ "${status:-}" = "healthy" ]; then
        echo "The app is healthy."
    else
        warn "the app isn't healthy yet. Check: docker compose logs app"
    fi

    say "Done. Next steps"
    cat <<EOF
1. Create your admin account:
     cd $REPO_DIR && docker compose exec app python manage.py createsuperuser
2. Visit https://$DOMAIN (the certificate can take a minute on first start).
3. Fill in $REPO_DIR/deploy/jobs.env (off-site backups, monitoring URLs) and email settings in
   $REPO_DIR/.env, then run: cd $REPO_DIR && docker compose --profile proxy up -d
4. Test the jobs once by hand:
     $REPO_DIR/deploy/backup.sh && $REPO_DIR/deploy/offsite.sh
The full checklist is in docs/DEPLOY.md.
EOF
}

# Allow the helper functions to be sourced (e.g. for testing) without running the setup.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
