#!/usr/bin/env bash
# One-time (and safely re-runnable) setup of a fresh Ubuntu 24.04 server for PrintPool.
#
#   git clone https://github.com/lemon-gith/printerpool /opt/printerpool
#
#   # Either: public server with Caddy and Let's Encrypt (DNS must already point at this server)
#   sudo /opt/printerpool/deploy/setup-server.sh print.example.ac.uk
#
#   # Or: behind a Cloudflare Tunnel, with no inbound ports needed
#   sudo CLOUDFLARE_TUNNEL_TOKEN=... /opt/printerpool/deploy/setup-server.sh --cloudflare-tunnel print.example.com
#
# Optional environment variables (kept out of the command line so they don't show up in `ps` or history):
#   CLOUDFLARE_TUNNEL_TOKEN      install cloudflared and connect it to that tunnel (tunnel mode)
#   DJANGO_SUPERUSER_USERNAME    create this admin account if it doesn't exist yet...
#   DJANGO_SUPERUSER_PASSWORD    ...with this password, which must be changed at first login
#
# Re-running is safe: packages are updated, and secrets, keys and your own settings are never overwritten.
# Only the settings that follow from the mode (proxy, ports, IP handling) are reapplied, so re-running with
# the other mode switches the server over.
# See docs/DEPLOY.md for the whole checklist, including the steps that happen outside this server.
set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MODE=caddy
DOMAIN=

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
        echo "$env_file already exists; keeping its secrets and settings."
    else
        cp "$REPO_DIR/.env.example" "$env_file"
        chmod 600 "$env_file"
        set_env "$env_file" DJANGO_SECRET_KEY "$(random_secret 64)"
        set_env "$env_file" POSTGRES_PASSWORD "$(random_secret 32)"
        set_env "$env_file" DJANGO_DEBUG 0
        set_env "$env_file" DJANGO_ALLOWED_HOSTS "$DOMAIN"
        set_env "$env_file" DJANGO_CSRF_TRUSTED_ORIGINS "https://$DOMAIN"
        set_env "$env_file" DJANGO_SITE_URL "https://$DOMAIN"
        echo "Created $env_file with a new secret key and database password."
    fi
    apply_mode_settings "$env_file"
}

# Settings that follow directly from the mode. In both modes visitors use HTTPS and the app port is only
# reachable from this machine; what differs is who terminates HTTPS and where visitor IPs come from.
apply_mode_settings() {
    local env_file=$1
    set_env "$env_file" DJANGO_SECURE 1
    set_env "$env_file" APP_PORT 127.0.0.1:8000
    if [ "$MODE" = tunnel ]; then
        set_env "$env_file" COMPOSE_PROFILES ""                         # no Caddy
        set_env "$env_file" DJANGO_TRUSTED_PROXY_COUNT 0
        set_env "$env_file" DJANGO_CLIENT_IP_HEADER CF-Connecting-IP    # set by Cloudflare's edge
        set_env "$env_file" MAX_REQUEST_UPLOAD_MB 100                   # Cloudflare free plan's request limit
        echo "Applied Cloudflare Tunnel settings: no Caddy; visitor IPs from CF-Connecting-IP."
    else
        set_env "$env_file" COMPOSE_PROFILES proxy                      # start Caddy
        set_env "$env_file" SITE_DOMAIN "$DOMAIN"
        set_env "$env_file" DJANGO_TRUSTED_PROXY_COUNT 1
        set_env "$env_file" DJANGO_CLIENT_IP_HEADER ""
        echo "Applied Caddy settings: Caddy serves HTTPS for $DOMAIN; visitor IPs from X-Forwarded-For."
    fi
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

parse_args() {
    while [ $# -gt 0 ]; do
        case $1 in
            --cloudflare-tunnel) MODE=tunnel ;;
            -h | --help) usage; exit 0 ;;
            -*) echo "Unknown option: $1" >&2; usage; exit 1 ;;
            *) DOMAIN=$1 ;;
        esac
        shift
    done
    if [ -z "$DOMAIN" ]; then
        usage
        exit 1
    fi
}

usage() {
    cat >&2 <<EOF
Usage: $0 [--cloudflare-tunnel] <domain>
  e.g. $0 print.example.ac.uk
       CLOUDFLARE_TUNNEL_TOKEN=... $0 --cloudflare-tunnel print.example.com
EOF
}

configure_firewall() {
    ufw allow OpenSSH  # harmless if the Hetzner firewall blocks SSH; avoids lockout if you open it later
    if [ "$MODE" = tunnel ]; then
        say "Configuring the firewall (no web ports: traffic arrives through the tunnel)"
        for rule in 80/tcp 443/tcp 443/udp; do ufw delete allow "$rule" > /dev/null 2>&1 || true; done
    else
        say "Configuring the firewall (SSH, HTTP, HTTPS)"
        ufw allow 80/tcp
        ufw allow 443/tcp
        ufw allow 443/udp  # HTTP/3
    fi
    ufw --force enable
    # Note: Docker bypasses ufw for published ports. That's fine here because the only ports published are
    # Caddy's 80/443 (Caddy mode) and the app on 127.0.0.1; the Hetzner Cloud Firewall is the outer layer.
}

install_cloudflared() {
    if [ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
        if ! systemctl is-active --quiet cloudflared 2> /dev/null; then
            warn "CLOUDFLARE_TUNNEL_TOKEN isn't set and cloudflared isn't running; install it yourself (docs/DEPLOY.md)."
        fi
        return
    fi
    say "Installing cloudflared"
    if ! command -v cloudflared > /dev/null; then
        install -m 0755 -d /usr/share/keyrings
        curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
        echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
            > /etc/apt/sources.list.d/cloudflared.list
        apt-get update -q
        apt-get install -yq cloudflared
    fi
    if systemctl is-active --quiet cloudflared 2> /dev/null; then
        echo "cloudflared is already running as a service; leaving it connected to its current tunnel."
    else
        cloudflared service install "$CLOUDFLARE_TUNNEL_TOKEN"
        echo "cloudflared is installed as a service and connected to your tunnel."
    fi
}

wait_until_healthy() {
    local status=
    echo -n "Waiting for the app to report healthy"
    for _ in $(seq 1 60); do
        status=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app)" 2> /dev/null || true)
        [ "$status" = "healthy" ] && break
        echo -n "."
        sleep 2
    done
    echo
    if [ "$status" = "healthy" ]; then
        echo "The app is healthy."
    else
        warn "the app isn't healthy yet. Check: docker compose logs app"
        return 1
    fi
}

main() {
    parse_args "$@"
    if [ "$(id -u)" -ne 0 ]; then
        echo "Please run as root (sudo)." >&2
        exit 1
    fi
    if ! grep -qs 'VERSION_ID="24.04"' /etc/os-release; then
        warn "this script is written for Ubuntu 24.04; continuing, but check each step."
    fi
    echo "Setting up PrintPool for $DOMAIN ($([ "$MODE" = tunnel ] && echo "Cloudflare Tunnel" || echo "Caddy + Let's Encrypt") mode)."

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

    configure_firewall

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
    if [ "$MODE" = tunnel ]; then
        docker compose --profile proxy rm -sf caddy > /dev/null 2>&1 || true  # in case Caddy mode ran before
    fi
    docker compose up -d --build  # COMPOSE_PROFILES in .env decides whether Caddy starts
    wait_until_healthy || true

    if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
        say "Creating the first admin account"
        docker compose exec -T -e DJANGO_SUPERUSER_USERNAME -e DJANGO_SUPERUSER_PASSWORD \
            app python manage.py create_initial_admin
    fi

    if [ "$MODE" = tunnel ]; then
        install_cloudflared
    fi

    say "Done. Next steps"
    local step=1
    if [ -z "${DJANGO_SUPERUSER_USERNAME:-}" ]; then
        echo "$step. Create your admin account:"
        echo "     cd $REPO_DIR && docker compose exec app python manage.py createsuperuser"
        step=$((step + 1))
    fi
    if [ "$MODE" = tunnel ]; then
        echo "$step. In Cloudflare, give the tunnel a public hostname $DOMAIN with service http://localhost:8000,"
        echo "   then visit https://$DOMAIN."
    else
        echo "$step. Visit https://$DOMAIN (the certificate can take a minute on first start)."
    fi
    step=$((step + 1))
    cat <<EOF
$step. Fill in $REPO_DIR/deploy/jobs.env (off-site backups, monitoring URLs) and email settings in
   $REPO_DIR/.env, then run: cd $REPO_DIR && docker compose up -d
$((step + 1)). Test the jobs once by hand:
     $REPO_DIR/deploy/backup.sh && $REPO_DIR/deploy/offsite.sh
The full checklist is in docs/DEPLOY.md.
EOF
}

# Allow the helper functions to be sourced (e.g. for testing) without running the setup.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
