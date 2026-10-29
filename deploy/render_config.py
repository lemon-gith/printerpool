"""Turn printpool.toml into the files Docker Compose uses. Run by deploy/setup-server.sh / .ps1.

Reads   printpool.toml   (deployment settings, see printpool.example.toml)
        .env             (secrets; created with fresh ones if missing)
Writes  .generated/app.env       settings for the app and worker containers
        .generated/gateway.conf  nginx config: ingress to the app, allowlisted egress out
        compose.override.yaml    which optional services, ports, volumes and network aliases to add;
                                 Docker Compose merges it into compose.yaml automatically

Standard library only, so it runs in a plain python container with nothing installed on the host.
"""

import argparse
import copy
import ipaddress
import json
import re
import secrets
import sys
import tomllib
from pathlib import Path

DEFAULTS = {
    "site": {"domain": "", "time_zone": "Europe/London"},
    "cloudflared": {"enabled": True, "mode": "token"},
    "ingress": {"publish": "", "https": True, "hsts_seconds": 3600},
    "email": {
        "enabled": False,
        "host": "smtp.resend.com",
        "port": 587,
        "username": "resend",
        "from": "",
    },
    "egress": [],
    "gateway": {"dns": []},
    "backups": {
        "enabled": False,
        "frequency": "daily",
        "time": "03:30",
        "weekday": "sunday",
        "keep": 14,
        "encrypt": True,
        "include_files": False,
        "location": {"type": "folder", "path": "./backups", "volume": "printpool_backups"},
    },
    "app": {
        "print_cap_grams_per_month": 500,
        "max_upload_mb": 50,
        "max_request_upload_mb": 100,
        "contact_staff_emails": [],
        "contact_admin_emails": [],
    },
}

INGRESS_PORT = 8080  # gateway's port for requests to the app (cloudflared connects to gateway:8080)
GATEWAY_HEALTH_PORT = 8081
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
GENERATED_SECRETS = {
    "DJANGO_SECRET_KEY": lambda: secrets.token_urlsafe(50),
    "POSTGRES_PASSWORD": lambda: secrets.token_urlsafe(24),
}
FIXED_ENV = {"POSTGRES_DB": "printpool", "POSTGRES_USER": "printpool"}
# Settings that used to live in .env and are now controlled by printpool.toml (and overridden by it).
SUPERSEDED_ENV = {
    "DJANGO_ALLOWED_HOSTS", "DJANGO_CSRF_TRUSTED_ORIGINS", "DJANGO_SITE_URL", "DJANGO_SECURE", "DJANGO_HSTS_SECONDS",
    "DJANGO_TRUSTED_PROXY_COUNT", "DJANGO_CLIENT_IP_HEADER", "APP_PORT", "SITE_DOMAIN", "COMPOSE_PROFILES",
    "EMAIL_HOST", "EMAIL_PORT", "EMAIL_HOST_USER", "DEFAULT_FROM_EMAIL", "PRINT_CAP_GRAMS_PER_MONTH",
    "MAX_UPLOAD_MB", "MAX_REQUEST_UPLOAD_MB", "CONTACT_STAFF_EMAILS", "CONTACT_ADMIN_EMAILS",
}

ENV_TEMPLATE = """\
# PrintPool secrets. Created by setup-server; keep this file private and out of git (it's git-ignored).
# Settings that aren't secret live in printpool.toml.

# Generated automatically. Don't change these once the site is running: the database password is
# baked into the database on first start.
DJANGO_SECRET_KEY={DJANGO_SECRET_KEY}
POSTGRES_DB={POSTGRES_DB}
POSTGRES_USER={POSTGRES_USER}
POSTGRES_PASSWORD={POSTGRES_PASSWORD}

# [cloudflared] mode = "token": the tunnel token from the Cloudflare dashboard.
CLOUDFLARE_TUNNEL_TOKEN=

# [email] enabled = true: the SMTP password (for Resend, your API key, re_...).
EMAIL_PASSWORD=

# [backups] encrypt = true: the password for backup zips. Keep a copy somewhere safe; without it, backups
# can't be opened.
BACKUP_PASSWORD=
"""


class ConfigError(Exception):
    pass


# --- Loading and validation -------------------------------------------------------------------------------
def merge(defaults, given, path=""):
    """Defaults overlaid with the user's values. Unknown keys are errors, so typos don't silently do nothing."""
    result = copy.deepcopy(defaults)
    for key, value in given.items():
        where = f"{path}.{key}" if path else key
        if key not in defaults:
            raise ConfigError(f"unknown setting [{where}] (check the spelling against printpool.example.toml)")
        if isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(f"[{where}] should be a section")
            result[key] = merge(defaults[key], value, where)
        else:
            result[key] = value
    return result


def expect(cond, message):
    if not cond:
        raise ConfigError(message)


def is_hostname(value):
    return isinstance(value, str) and bool(HOSTNAME_RE.match(value.lower()))


def is_port(value):
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def is_email(value):
    return isinstance(value, str) and re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", value) is not None


def load_config(path):
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path} not found. Copy printpool.example.toml to printpool.toml and edit it first.")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} isn't valid TOML: {e}")
    cfg = merge(DEFAULTS, raw)
    validate(cfg)
    return cfg


def validate(cfg):
    site, cf, ingress, email, backups, app = (
        cfg["site"], cfg["cloudflared"], cfg["ingress"], cfg["email"], cfg["backups"], cfg["app"])

    expect(is_hostname(site["domain"]) and site["domain"] != "print.example.com" or site["domain"] == "localhost",
           "[site] domain must be your real hostname (e.g. print.yourdomain.com), or \"localhost\" for local testing")
    expect(isinstance(site["time_zone"], str) and "/" in site["time_zone"] or site["time_zone"] == "UTC",
           "[site] time_zone should look like \"Europe/London\"")

    for key in ("enabled",):
        expect(isinstance(cf[key], bool), f"[cloudflared] {key} must be true or false")
    expect(cf["mode"] in ("token", "config"), "[cloudflared] mode must be \"token\" or \"config\"")

    publish = ingress["publish"]
    expect(isinstance(publish, str), "[ingress] publish must be a string like \"127.0.0.1:8000\" or \"\"")
    if publish:
        host, _, port = publish.rpartition(":")
        expect(port.isdigit() and is_port(int(port)) and host,
               "[ingress] publish must look like \"127.0.0.1:8000\" (address:port)")
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            raise ConfigError("[ingress] publish needs an IP address before the port, e.g. \"127.0.0.1:8000\"")
    expect(cf["enabled"] or publish,
           "nothing can reach the site: enable [cloudflared] or set [ingress] publish (e.g. \"127.0.0.1:8000\")")
    expect(isinstance(ingress["https"], bool), "[ingress] https must be true or false")
    expect(isinstance(ingress["hsts_seconds"], int) and ingress["hsts_seconds"] >= 0,
           "[ingress] hsts_seconds must be a whole number of seconds")

    expect(isinstance(email["enabled"], bool), "[email] enabled must be true or false")
    if email["enabled"]:
        expect(is_hostname(email["host"]), "[email] host must be a hostname like smtp.resend.com")
        expect(is_port(email["port"]), "[email] port must be a port number (usually 587)")
        expect(isinstance(email["username"], str), "[email] username must be text")
        expect(isinstance(email["from"], str) and "@" in email["from"],
               "[email] from must be an address on your sending domain, e.g. \"PrintPool <printpool@send.example.com>\"")

    expect(isinstance(cfg["egress"], list), "[[egress]] entries must each be a table with host and port")
    for i, rule in enumerate(cfg["egress"], start=1):
        expect(isinstance(rule, dict) and set(rule) == {"host", "port"},
               f"[[egress]] entry {i} must have exactly a host and a port")
        expect(is_hostname(rule["host"]), f"[[egress]] entry {i}: host must be a hostname")
        expect(is_port(rule["port"]), f"[[egress]] entry {i}: port must be a port number")

    dns = cfg["gateway"]["dns"]
    expect(isinstance(dns, list), "[gateway] dns must be a list of IP addresses")
    for server in dns:
        try:
            ipaddress.ip_address(server)
        except (ValueError, TypeError):
            raise ConfigError(f"[gateway] dns: {server!r} isn't an IP address")

    expect(isinstance(backups["enabled"], bool), "[backups] enabled must be true or false")
    expect(backups["frequency"] in ("hourly", "daily", "weekly"),
           "[backups] frequency must be \"hourly\", \"daily\" or \"weekly\"")
    expect(isinstance(backups["time"], str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", backups["time"]),
           "[backups] time must be HH:MM, e.g. \"03:30\"")
    expect(backups["weekday"] in WEEKDAYS, "[backups] weekday must be a day name like \"sunday\"")
    expect(isinstance(backups["keep"], int) and not isinstance(backups["keep"], bool) and backups["keep"] >= 0,
           "[backups] keep must be a whole number (0 keeps every backup)")
    for key in ("encrypt", "include_files"):
        expect(isinstance(backups[key], bool), f"[backups] {key} must be true or false")
    loc = backups["location"]
    expect(loc["type"] in ("folder", "volume"), "[backups.location] type must be \"folder\" or \"volume\"")
    if loc["type"] == "folder":
        expect(isinstance(loc["path"], str) and loc["path"].strip(), "[backups.location] path must be a folder")
    else:
        expect(isinstance(loc["volume"], str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", loc["volume"]),
               "[backups.location] volume must be a Docker volume name (letters, digits, _ . -)")

    for key in ("print_cap_grams_per_month", "max_upload_mb", "max_request_upload_mb"):
        expect(isinstance(app[key], int) and not isinstance(app[key], bool) and app[key] > 0,
               f"[app] {key} must be a positive whole number")
    for key in ("contact_staff_emails", "contact_admin_emails"):
        expect(isinstance(app[key], list) and all(is_email(a) for a in app[key]),
               f"[app] {key} must be a list of email addresses")


def egress_rules(cfg):
    """All allowed (host, port) destinations, de-duplicated, in a stable order."""
    rules = []
    if cfg["email"]["enabled"]:
        rules.append((cfg["email"]["host"].lower(), cfg["email"]["port"]))
    rules += [(r["host"].lower(), r["port"]) for r in cfg["egress"]]
    seen, unique = set(), []
    for rule in rules:
        if rule not in seen:
            seen.add(rule)
            unique.append(rule)
    for host, port in unique:
        expect(port not in (INGRESS_PORT, GATEWAY_HEALTH_PORT),
               f"egress to {host}:{port}: ports {INGRESS_PORT} and {GATEWAY_HEALTH_PORT} are used by the gateway itself")
    return unique


# --- Secrets (.env) ---------------------------------------------------------------------------------------
def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def ensure_env(path, notes):
    """Create .env with fresh secrets, or add any generated ones that are missing. Never changes existing values."""
    if not path.exists():
        values = {key: make() for key, make in GENERATED_SECRETS.items()} | FIXED_ENV
        path.write_text(ENV_TEMPLATE.format(**values), encoding="utf-8")
        notes.append(f"Created {path.name} with a new secret key and database password.")
        return read_env(path)
    values = read_env(path)
    missing = {key: make() for key, make in GENERATED_SECRETS.items() if not values.get(key)}
    missing |= {key: value for key, value in FIXED_ENV.items() if not values.get(key)}
    if missing:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n# Added by setup-server\n" + "".join(f"{k}={v}\n" for k, v in missing.items()))
        notes.append(f"Added {', '.join(missing)} to {path.name}.")
        values |= missing
    stale = sorted(SUPERSEDED_ENV & set(values))
    if stale:
        notes.append(f"{path.name} still sets {', '.join(stale)}; these now come from printpool.toml and are "
                     "ignored. You can delete them from .env.")
    return values


def check_secrets(cfg, env):
    problems = []
    if cfg["cloudflared"]["enabled"] and cfg["cloudflared"]["mode"] == "token" and not env.get("CLOUDFLARE_TUNNEL_TOKEN"):
        problems.append("[cloudflared] mode = \"token\" needs CLOUDFLARE_TUNNEL_TOKEN in .env")
    if cfg["email"]["enabled"] and not (env.get("EMAIL_PASSWORD") or env.get("EMAIL_HOST_PASSWORD")):
        problems.append("[email] is enabled but EMAIL_PASSWORD in .env is empty")
    if cfg["backups"]["enabled"] and cfg["backups"]["encrypt"] and not env.get("BACKUP_PASSWORD"):
        problems.append("[backups] are enabled with encrypt = true but BACKUP_PASSWORD in .env is empty")
    return problems


# --- Rendering ------------------------------------------------------------------------------------------
def render_app_env(cfg):
    site, cf, ingress, email, backups, app = (
        cfg["site"], cfg["cloudflared"], cfg["ingress"], cfg["email"], cfg["backups"], cfg["app"])
    domain = site["domain"]
    scheme = "https" if ingress["https"] else "http"
    hosts = [domain]
    origins = [f"{scheme}://{domain}"]
    if ingress["publish"]:
        host, _, port = ingress["publish"].rpartition(":")
        host = host.strip("[]")
        local = ["localhost", "127.0.0.1"] if ipaddress.ip_address(host).is_loopback else [host]
        for name in local:
            if name not in hosts:
                hosts.append(name)
            origins.append(f"http://{name}:{port}")
    env = {
        "DJANGO_ALLOWED_HOSTS": ",".join(hosts),
        "DJANGO_CSRF_TRUSTED_ORIGINS": ",".join(dict.fromkeys(origins)),
        "DJANGO_SITE_URL": f"{scheme}://{domain}",
        "DJANGO_SECURE": "1" if ingress["https"] else "0",
        "DJANGO_HSTS_SECONDS": str(ingress["hsts_seconds"]),
        # Over HTTPS, visitors come through Cloudflare (the cloudflared container, or one outside Docker using the
        # published port), whose header has the visitor's IP. For plain-http local testing, the gateway records
        # it as the one X-Forwarded-For entry it sends.
        "DJANGO_CLIENT_IP_HEADER": "CF-Connecting-IP" if ingress["https"] else "",
        "DJANGO_TRUSTED_PROXY_COUNT": "0" if ingress["https"] else "1",
        "TIME_ZONE": site["time_zone"],
        "EMAIL_ENABLED": "1" if email["enabled"] else "0",
        "EMAIL_HOST": email["host"] if email["enabled"] else "",
        "EMAIL_PORT": str(email["port"]),
        "EMAIL_HOST_USER": email["username"],
        "DEFAULT_FROM_EMAIL": email["from"] or f"PrintPool <printpool@{domain}>",
        "PRINT_CAP_GRAMS_PER_MONTH": str(app["print_cap_grams_per_month"]),
        "MAX_UPLOAD_MB": str(app["max_upload_mb"]),
        "MAX_REQUEST_UPLOAD_MB": str(app["max_request_upload_mb"]),
        "CONTACT_STAFF_EMAILS": ",".join(app["contact_staff_emails"]),
        "CONTACT_ADMIN_EMAILS": ",".join(app["contact_admin_emails"]),
        "BACKUP_ENABLED": "1" if backups["enabled"] else "0",
        "BACKUP_FREQUENCY": backups["frequency"],
        "BACKUP_TIME": backups["time"],
        "BACKUP_WEEKDAY": backups["weekday"],
        "BACKUP_KEEP": str(backups["keep"]),
        "BACKUP_ENCRYPT": "1" if backups["encrypt"] else "0",
        "BACKUP_INCLUDE_FILES": "1" if backups["include_files"] else "0",
        "BACKUP_DIR": "/backups",
    }
    header = "# Generated by deploy/render_config.py from printpool.toml. Don't edit: re-run setup-server instead.\n"
    return header + "".join(f"{key}={value}\n" for key, value in env.items())


def render_gateway_conf(cfg, rules):
    ingress, app = cfg["ingress"], cfg["app"]
    # Over HTTPS, cloudflared (in Docker or not) tells us whether the visitor used HTTPS; for plain-http local
    # testing it's the gateway's own scheme.
    forwarded_proto = "$http_x_forwarded_proto" if ingress["https"] else "$scheme"
    body_mb = app["max_request_upload_mb"] + 10  # room for the rest of the form

    lines = [
        "# Generated by deploy/render_config.py from printpool.toml. Don't edit: re-run setup-server instead.",
        "worker_processes auto;",
        "error_log /dev/stderr warn;",
        "pid /tmp/nginx.pid;",
        "events { worker_connections 1024; }",
        "",
        "http {",
        "    server_tokens off;",
        "    access_log /dev/stdout;",
        f"    client_max_body_size {body_mb}m;",
        "    client_body_timeout 300s;",
        "    proxy_read_timeout 300s;",
        "    proxy_send_timeout 300s;",
        "    # Docker's DNS, re-checked so a restarted app container is found at its new address.",
        "    resolver 127.0.0.11 valid=10s ipv6=off;",
        "",
        "    # Ingress: the only way to reach the app.",
        "    server {",
        f"        listen {INGRESS_PORT};",
        "        location / {",
        "            set $app_upstream http://app:8000;",
        "            proxy_pass $app_upstream;",
        "            proxy_set_header Host $host;",
        f"            proxy_set_header X-Forwarded-Proto {forwarded_proto};",
        "            proxy_set_header X-Forwarded-For $remote_addr;",
        "        }",
        "    }",
        "",
        "    server {",
        f"        listen {GATEWAY_HEALTH_PORT};",
        "        location = /healthz { access_log off; return 200 \"ok\\n\"; }",
        "    }",
        "}",
    ]
    if not rules:
        lines += ["", "# No egress: the app can't reach anything outside Docker."]
        return "\n".join(lines) + "\n"

    by_port = {}
    for host, port in rules:
        by_port.setdefault(port, []).append(host)
    lines += [
        "",
        "# Egress: the app reaches only these destinations. On the app's network, each allowed hostname is an",
        "# alias of this gateway, so the app connects here and the gateway passes the connection straight through",
        "# (TLS stays end to end between the app and the real server). Anything else has nowhere to go.",
        "stream {",
        "    # Not Docker's DNS: here the allowed hostnames are aliases of this container (see resolver.sh).",
        "    include /etc/nginx/printpool-resolver.conf;",
        "    log_format egress '$time_local egress $remote_addr -> $printpool_target ($upstream_addr) '",
        "                      '$status sent=$bytes_received received=$bytes_sent';",
        "    access_log /dev/stdout egress;",
        "    proxy_connect_timeout 10s;",
    ]
    denied = "unix:/run/printpool-egress-denied.sock"  # doesn't exist: the connection is refused and logged
    for port, hosts in sorted(by_port.items()):
        if len(hosts) == 1:
            # One destination on this port: forward everything (works for any protocol, e.g. SMTP with STARTTLS).
            lines += [
                f"    map $server_port $egress_{port} {{ default {hosts[0]}:{port}; }}",
                "    server {",
                f"        listen {port};",
                f"        set $printpool_target {hosts[0]}:{port};",
                f"        proxy_pass $egress_{port};",
                "    }",
            ]
        else:
            # Several destinations share the port: pick one by the TLS server name the app asks for.
            lines.append(f"    map $ssl_preread_server_name $egress_{port} {{")
            lines += [f"        {host} {host}:{port};" for host in hosts]
            lines += [f"        default {denied};", "    }", "    server {", f"        listen {port};",
                      "        ssl_preread on;", f"        set $printpool_target $ssl_preread_server_name:{port};",
                      f"        proxy_pass $egress_{port};", "    }"]
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_override(cfg, rules):
    """compose.override.yaml, written as JSON (which is valid YAML) so paths and names never need hand-quoting."""
    cf, ingress, backups = cfg["cloudflared"], cfg["ingress"], cfg["backups"]
    gateway = {
        "networks": {"edge": {}, "app": {"aliases": sorted({host for host, _ in rules})}},
        "environment": {"PRINTPOOL_DNS": " ".join(cfg["gateway"]["dns"])},
    }
    if ingress["publish"]:
        gateway["ports"] = [f"{ingress['publish']}:{INGRESS_PORT}"]
    loc = backups["location"]
    # Long syntax, so Windows paths like D:\\Backups (with their colon) work.
    if loc["type"] == "folder":
        mount = {"type": "bind", "source": loc["path"], "target": "/backups", "bind": {"create_host_path": True}}
    else:
        mount = {"type": "volume", "source": loc["volume"], "target": "/backups"}
    services = {"gateway": gateway, "worker": {"volumes": [mount]}}
    override = {"services": services}
    if loc["type"] == "volume":
        override["volumes"] = {loc["volume"]: {"name": loc["volume"]}}

    if cf["enabled"]:
        cloudflared = {
            "image": "cloudflare/cloudflared:latest",
            "restart": "unless-stopped",
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}},
            "networks": ["edge"],
            "depends_on": {"gateway": {"condition": "service_healthy"}},
        }
        if cf["mode"] == "token":
            cloudflared["command"] = ["tunnel", "--no-autoupdate", "run"]
            cloudflared["environment"] = {"TUNNEL_TOKEN": "${CLOUDFLARE_TUNNEL_TOKEN}"}
        else:
            cloudflared["command"] = ["tunnel", "--no-autoupdate", "--config", "/etc/cloudflared/config.yml", "run"]
            cloudflared["volumes"] = ["./deploy/cloudflared:/etc/cloudflared:ro"]
        services["cloudflared"] = cloudflared

    header = "# Generated by deploy/render_config.py from printpool.toml. Don't edit: re-run setup-server instead.\n"
    return header + json.dumps(override, indent=2) + "\n"


def check_cloudflared_files(cfg, root, notes):
    if not (cfg["cloudflared"]["enabled"] and cfg["cloudflared"]["mode"] == "config"):
        return []
    folder = root / "deploy" / "cloudflared"
    config = folder / "config.yml"
    if not config.exists():
        return ["[cloudflared] mode = \"config\" needs deploy/cloudflared/config.yml (see deploy/cloudflared/README.md)"]
    text = config.read_text(encoding="utf-8")
    if "gateway:8080" not in text:
        notes.append("deploy/cloudflared/config.yml doesn't mention gateway:8080; its ingress service should be "
                     "http://gateway:8080.")
    if not list(folder.glob("*.json")):
        notes.append("No tunnel credentials .json in deploy/cloudflared/; cloudflared won't be able to connect.")
    return []


# --- Main -----------------------------------------------------------------------------------------------
def render(root, config_name="printpool.toml"):
    """Validate and write everything. Returns (summary lines, notes). Raises ConfigError on problems."""
    root = Path(root)
    cfg = load_config(root / config_name)
    rules = egress_rules(cfg)
    notes = []
    env = ensure_env(root / ".env", notes)
    problems = check_secrets(cfg, env) + check_cloudflared_files(cfg, root, notes)
    if problems:
        raise ConfigError("; ".join(problems))

    generated = root / ".generated"
    generated.mkdir(exist_ok=True)
    (generated / "app.env").write_text(render_app_env(cfg), encoding="utf-8")
    (generated / "gateway.conf").write_text(render_gateway_conf(cfg, rules), encoding="utf-8")
    (root / "compose.override.yaml").write_text(render_override(cfg, rules), encoding="utf-8")
    loc = cfg["backups"]["location"]
    if loc["type"] == "folder" and not Path(loc["path"]).is_absolute():
        (root / loc["path"]).mkdir(parents=True, exist_ok=True)

    cf, ingress, backups = cfg["cloudflared"], cfg["ingress"], cfg["backups"]
    summary = [
        f"Site:        {'https' if ingress['https'] else 'http'}://{cfg['site']['domain']}",
        "Services:    gateway, app, worker, db" + (f", cloudflared ({cf['mode']})" if cf["enabled"] else ""),
        f"Host port:   {ingress['publish'] or 'none (only through cloudflared)'}",
        "App egress:  " + (", ".join(f"{h}:{p}" for h, p in rules) or "none"),
        "Email:       " + ("on" if cfg["email"]["enabled"] else "off"),
        "Backups:     " + (
            f"{backups['frequency']}"
            + (f" at {backups['time']}" if backups["frequency"] != "hourly" else "")
            + (f" on {backups['weekday']}s" if backups["frequency"] == "weekly" else "")
            + f", keep {backups['keep'] or 'all'}, {'encrypted' if backups['encrypt'] else 'unencrypted'}"
            + (", with uploaded files" if backups["include_files"] else "")
            if backups["enabled"] else "scheduled backups off (deploy/backup.sh still works)"),
        "Backups go:  " + (f"folder {loc['path']}" if loc["type"] == "folder" else f"Docker volume {loc['volume']}"),
    ]
    return summary, notes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository folder (default: current folder)")
    args = parser.parse_args(argv)
    try:
        summary, notes = render(args.root)
    except ConfigError as e:
        print(f"Configuration problem: {e}", file=sys.stderr)
        return 1
    print("\n".join(summary))
    for note in notes:
        print(f"Note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
