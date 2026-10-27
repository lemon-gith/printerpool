# PrintPool

3D print request system: students submit STL/3MF files, staff approve and run prints, admins manage accounts.

Django 5.2 LTS + PostgreSQL 16, run with Docker Compose.

## Roles

| Role  | Can do |
|-------|--------|
| user  | Submit requests with files, see own requests/prints/history, cancel own request while still *submitted*, recall & edit own request before printing starts, see own monthly filament usage |
| staff | Everything users can, for **all** requests: download files, approve/reject, queue/start/finish prints, record filament used |
| admin | Everything staff can, plus the Django admin panel at `/admin/` (accounts, printers, raw data) |

The `role` field is the single source of truth. Django's `is_staff`/`is_superuser` flags mirror it automatically, so only admins can reach `/admin/`.

## Running it

```sh
cp .env.example .env
# edit .env: set DJANGO_SECRET_KEY, POSTGRES_PASSWORD, DJANGO_ALLOWED_HOSTS, DJANGO_CSRF_TRUSTED_ORIGINS
docker compose up -d --build
docker compose exec app python manage.py createsuperuser   # your first admin
```

The app is on `http://<host>:8000`. Migrations run automatically on every start.

### Creating accounts

In `/admin/` → Users → *Add user*: enter a username and role only. A random password is generated and shown **once** in the confirmation banner. Give it to the person; they're forced to choose their own on first login. To reset a forgotten password, select the user(s) and use the *Reset password* action.

Accounts are **deactivated, never deleted**, so request history and usage figures stay intact. Use the *Deactivate selected users* action (or untick *Active*). A deactivated user is logged out immediately and can't log in. *Reactivate selected users* undoes it.

### Editing a request (recall & edit)

Requests are immutable. To change one, its owner clicks **Recall & edit**. This is possible while it's *submitted* or *approved*, i.e. before any print has started. The original becomes *Recalled*: any queued prints are cancelled and its files expire in 7 days. The owner is then taken to a pre-filled form where they can change the details, keep any of the original files and add new ones. Submitting creates a **new** request that links back to the one it replaces and goes through approval again. Replaced requests are hidden from the main list but stay reachable from the new request's page and via the *Recalled* filter.

### Printers

Add printers under `/admin/` → Printers. Staff can then choose one when queueing a print. Choosing a printer is optional.

## File expiry

Each request has a `files_expire_at` timestamp:

| Event | Files expire |
|-------|--------------|
| Request submitted | 30 days later |
| Request rejected, cancelled or recalled | 7 days later |
| A print is started | 7 days later (resets on each print start) |
| A print fails | 7 days later (so a reprint still has the files) |

Approval alone doesn't change the expiry. The rules live in `prints/services.py`.

Downloads are blocked as soon as the deadline passes. The files are actually deleted by `python manage.py purge_expired_files` (use `--dry-run` to preview), run hourly from cron (see [Scheduled jobs](#scheduled-jobs)). The database rows are kept for history.

## Print cap

The monthly cap is in grams of filament: `PRINT_CAP_GRAMS_PER_MONTH` in `.env`, with a per-user override in the admin. Usage is calculated live from prints that **succeeded or failed** during the current calendar month, using the grams staff enter when finishing a print (the slicer estimate if left blank). Students see their usage on their dashboard, and staff see the requester's usage on each request. Nothing is blocked by the cap yet; it's informational. See `cap_status()` / `filament_used()` in `prints/services.py`.

## HTTPS on a public VPS

Point a DNS name at the VPS, then in `.env`:

```
SITE_DOMAIN=print.example.ac.uk
DJANGO_ALLOWED_HOSTS=print.example.ac.uk
DJANGO_CSRF_TRUSTED_ORIGINS=https://print.example.ac.uk
DJANGO_SECURE=1
APP_PORT=127.0.0.1:8000          # only reachable through Caddy
DJANGO_TRUSTED_PROXY_COUNT=1     # trust Caddy's X-Forwarded-For for real visitor IPs
```

Then run `docker compose --profile proxy up -d`. Caddy fetches and renews a Let's Encrypt certificate automatically.

**Keep `APP_PORT` and `DJANGO_TRUSTED_PROXY_COUNT` together.** Login throttling locks out a username + IP pair after 5 failures. Behind Caddy, the app would otherwise see every visitor as Caddy's address, so anyone could lock any user out. `DJANGO_TRUSTED_PROXY_COUNT=1` makes it use the address Caddy recorded instead. That's only safe while the app port is unreachable from outside (`127.0.0.1:`), because a client talking to the app directly could fake that header. For local testing without Caddy, leave both at their defaults.

On Hetzner, also add a Cloud Firewall allowing only TCP 22, 80 and 443 inbound.

## Backups

Only the database is backed up automatically. Uploaded files expire within a month and can be re-uploaded.

- **Nightly** `deploy/backup.sh` writes a compressed `pg_dump` to `BACKUP_DIR` on the server (default `/var/backups/printpool`). It checks the dump is readable before keeping it, writes a `.sha256` checksum next to it, and deletes dumps older than `BACKUP_KEEP_DAYS` (default 14).
- **Weekly** `deploy/offsite.sh` re-verifies the newest dump's checksum and copies it (plus checksum) off the server, over **SSH** (`scp` with a dedicated key), an **HTTPS upload** (`PUT <url>/<file>` with `Authorization: Bearer <API key>`), or both.

Setup:

```sh
cp deploy/backup.env.example deploy/backup.env   # git-ignored; fill in the OFFSITE_* settings
chmod 600 deploy/backup.env

# SSH transport: create a key used only for backups, and install the public half on your server
sudo ssh-keygen -t ed25519 -N "" -C printpool-backup -f /root/.ssh/printpool_backup
sudo cat /root/.ssh/printpool_backup.pub
```

On the receiving server, put that public key in the backup user's `~/.ssh/authorized_keys`, prefixed with `restrict,` so the key can copy files but can't open a shell or forward ports:

```
restrict ssh-ed25519 AAAA... printpool-backup
```

Run both scripts once by hand (`sudo deploy/backup.sh && sudo deploy/offsite.sh`) to check they work, then add them to cron.

**Restoring** (works for any dump, local or off-site):

```sh
docker compose stop app
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < printpool-YYYYMMDDTHHMMSSZ.dump
docker compose start app
```

## Scheduled jobs

`deploy/crontab.example` holds all three jobs: hourly file purge, nightly dump, weekly off-site copy. Install it with `sudo crontab -e`, adjusting the path to the repo.

## Moving to another server

1. On the old host: `docker compose stop app`, run `deploy/backup.sh`, and copy the newest dump across. To keep files that haven't expired yet, also run: `docker run --rm -v printpool_media:/data -v "$PWD":/backup alpine tar czf /backup/media.tgz -C /data .`
2. On the new host: clone the repo, copy `.env` and `deploy/backup.env` (update the hostnames), then `docker compose up -d db`.
3. Restore the database as above. Restore files (if copied): `docker compose run --rm -v "$PWD":/backup --entrypoint sh app -c "tar xzf /backup/media.tgz -C /data/media"`
4. `docker compose up -d`, then install the crontab.

## Development

```sh
docker compose run --rm app python manage.py test          # run tests
docker compose run --rm -u root -v "$PWD:/app" app python manage.py makemigrations   # after model changes
```

## Layout

```
accounts/   User model with roles, forced password change, admin account creation
prints/     requests, files, prints, printers, audit log
  services.py   all workflow rules: transitions, permissions, expiry, purge, cap
  validators.py STL/3MF checks (extension, size, file signature)
templates/  server-rendered HTML
deploy/     Caddyfile (HTTPS proxy), backup scripts, example crontab
```

Container logs are capped at 5 × 10 MB per service (`x-logging` in `compose.yaml`), so they can't fill the disk.

## Next steps

- Email notifications (status changes)
- Live status updates
- Printer API polling (OctoPrint / Moonraker / Bambu) as a worker service
- Enforce the cap (warn at submit, block at approval), plus custom periods
- Scheduling, using `Print.priority`
