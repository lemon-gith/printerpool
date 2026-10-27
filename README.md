# PrintPool

3D print request system: students submit STL/3MF files, staff approve and run prints, admins manage accounts.

Django 5.2 LTS + PostgreSQL 16, run with Docker Compose.

## Roles

| Role  | Can do |
|-------|--------|
| user  | Submit requests with files, see own requests/prints/history, cancel own request while still *submitted*, see own monthly filament usage |
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

### Printers

Add printers under `/admin/` → Printers. Staff can then choose one when queueing a print. Choosing a printer is optional.

## File expiry

Each request has a `files_expire_at` timestamp:

| Event | Files expire |
|-------|--------------|
| Request submitted | 30 days later |
| Request rejected or cancelled | 7 days later |
| A print is started | 7 days later (resets on each print start) |
| A print fails | 7 days later (so a reprint still has the files) |

Approval alone doesn't change the expiry. The rules live in `prints/services.py`.

Downloads are blocked as soon as the deadline passes. The files are actually deleted by a cleanup command; the database rows are kept for history. Schedule the command on the host with cron (`crontab -e`):

```cron
17 * * * * cd /path/to/printerpool && docker compose exec -T app python manage.py purge_expired_files >> /var/log/printpool-purge.log 2>&1
```

Use `--dry-run` to preview.

## Print cap

The monthly cap is in grams of filament: `PRINT_CAP_GRAMS_PER_MONTH` in `.env`, with a per-user override in the admin. Usage is calculated live from prints that **succeeded or failed** during the current calendar month, using the grams staff enter when finishing a print (the slicer estimate if left blank). Students see their usage on their dashboard, and staff see the requester's usage on each request. Nothing is blocked by the cap yet; it's informational. See `cap_status()` / `filament_used()` in `prints/services.py`.

## HTTPS on a public VPS

Point a DNS name at the VPS, then in `.env`:

```
SITE_DOMAIN=print.example.ac.uk
DJANGO_ALLOWED_HOSTS=print.example.ac.uk
DJANGO_CSRF_TRUSTED_ORIGINS=https://print.example.ac.uk
DJANGO_SECURE=1
APP_PORT=127.0.0.1:8000     # stop exposing the app directly
```

Then run `docker compose --profile proxy up -d`. Caddy fetches and renews a Let's Encrypt certificate automatically.

In the lab, if the university already puts its own reverse proxy in front of the server, leave the `proxy` profile off and point that proxy at port 8000.

## Backups

```sh
# database
docker compose exec -T db pg_dump -U printpool -Fc printpool > printpool-$(date +%F).dump
# uploaded files
docker run --rm -v printpool_media:/data -v "$PWD":/backup alpine tar czf /backup/media-$(date +%F).tgz -C /data .
```

## Moving from the VPS to the lab server

1. On the old host: `docker compose stop app` and take both backups above.
2. On the new host: clone the repo, copy `.env` (update the hostnames), then `docker compose up -d db`.
3. Restore the database: `docker compose exec -T db pg_restore -U printpool -d printpool --clean --if-exists < printpool-DATE.dump`
4. Restore the files: `docker compose run --rm -v "$PWD":/backup --entrypoint sh app -c "tar xzf /backup/media-DATE.tgz -C /data/media"`
5. `docker compose up -d`, then set up the cron job again.

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
deploy/     Caddyfile for the optional HTTPS proxy
```

## Next steps

- Live status updates (htmx polling the print table)
- Printer API polling (OctoPrint / Moonraker / Bambu) as a worker service
- Enforce the cap (warn at submit, block at approval), plus custom periods
- Scheduling, using `Print.priority`
