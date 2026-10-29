# PrintPool

3D print request system: students submit STL/3MF files, staff approve and run prints, admins manage accounts.

Django 5.2 LTS + PostgreSQL 16, run entirely in Docker Compose. Deployment is self-contained: it changes nothing on the
machine outside this folder, so it plays nicely on a shared PC, and visitors reach it through a Cloudflare Tunnel.

## Roles

| Role  | Can do |
|-------|--------|
| user  | Submit requests with files; see own requests, prints and history; recall own unfinished requests (and optionally edit and resubmit them); see own monthly filament usage; opt in to email notifications |
| staff | Everything users can, for **all** requests: download files, approve/reject requests, queue/start/finish/cancel prints, record filament used |
| admin | Everything staff can, plus the Django admin panel at `/admin/` (accounts, printers, raw data) |

The `role` field is the single source of truth. Django's `is_staff`/`is_superuser` flags mirror it automatically, so only admins can reach `/admin/`.

## Running it

Needs only **Docker** (Docker Desktop on Windows/macOS). [docs/DEPLOY.md](docs/DEPLOY.md) has the full walkthrough.

```sh
cp printpool.example.toml printpool.toml    # Windows: Copy-Item printpool.example.toml printpool.toml
# edit printpool.toml: at least [site] domain and [cloudflared]
deploy/setup-server.sh                       # Windows: .\deploy\setup-server.ps1
docker compose exec app python manage.py createsuperuser
```

`setup-server` checks `printpool.toml`, creates `.env` with fresh secrets on the first run (and tells you if a
secret your settings need is missing), builds and starts everything, and waits until it's healthy. Run it again after
editing `printpool.toml` or pulling a new version. Migrations run automatically.

For local testing without Cloudflare, set `[cloudflared] enabled = false`, `[ingress] publish = "127.0.0.1:8000"`,
`https = false` and `[site] domain = "localhost"`, then open `http://localhost:8000`.

### Configuration

| File | What | In git? |
|------|------|---------|
| `printpool.toml` | All deployment settings: domain, cloudflared, which ports and destinations are open, email, backups, app limits. Annotated in `printpool.example.toml`. | no (copy the example) |
| `.env` | Secrets only: Django secret key and database password (generated), and `CLOUDFLARE_TUNNEL_TOKEN`, `EMAIL_PASSWORD`, `BACKUP_PASSWORD` when you use those features. | no |
| `.generated/`, `compose.override.yaml` | Rendered from the two above by `deploy/render_config.py`. Don't edit; re-run `setup-server`. | no |

### What runs, and what can talk to what

```
  cloudflared ─┐  (optional container, [cloudflared])
  host port ───┤  (optional, [ingress] publish)
               ▼
  gateway (nginx) ── edge network (internet): allowlisted egress only
     │  app network (internal)
     ▼
  app ─── db network (internal) ─── db, worker
```

- **gateway** (nginx) is the only way in: cloudflared, or a published port if you set one, connects to it on port 8080.
  It is also the only way out: on the app's network, each allowed destination's hostname (e.g. `smtp.resend.com`
  when email is on) is an alias of the gateway, which passes those connections straight through. TLS stays end
  to end, so the gateway never sees message contents. Each connection is logged (`docker compose logs gateway`).
- **app**, **worker** and **db** are only on `internal` Docker networks: no route to the internet, and they can't
  even look up outside hostnames. The database publishes no ports, and only the app and worker can reach it; the
  gateway can't.
- **worker** runs the hourly cleanup of expired files and scheduled backups.
- Opening more egress: turn on `[email]`, or add `[[egress]]` entries with a host and port. Several hosts can
  share port 443; they're told apart by the TLS server name. To look those hostnames up, the gateway uses this
  machine's DNS servers, or `[gateway] dns` if set.

### Creating accounts

- **Many at once:** `/admin/` → Users → **Import users (CSV)**, with columns `username` (required), `first_name`, `last_name`, `role` (`user`/`student`, `staff` or `admin`; blank means user). The whole file is checked first, and nothing is created if any row has a problem (bad or duplicate username, unknown role, existing account). Up to 1000 rows; Excel's "CSV UTF-8" works.
- **One at a time:** `/admin/` → Users → *Add user*: enter a username and role only.
- **Forgotten password:** select the user(s) and use the *Reset password* action.

In every case a random temporary password is generated and shown **once**, on a printable sheet of cut-out slips (with a CSV download). The page isn't cached and the passwords aren't stored. People are forced to choose their own password at first login.

Accounts are **deactivated, never deleted**, so request history and usage figures stay intact. Use the *Deactivate selected users* action (or untick *Active*). A deactivated user is logged out immediately and can't log in. *Reactivate selected users* undoes it.

### Wording: recall, reject, cancel

- **Recall**: the owner withdraws a request that isn't finished yet.
- **Reject**: staff decline a request (before or during printing).
- **Cancel**: applies to **prints** only. Staff cancel a queued or running print.

A request can't be recalled, rejected or completed while one of its prints is running. Staff finish or cancel that print first. Recalling or rejecting cancels any prints still queued, and completing needs any queued prints dealt with first.

### Editing a request (recall & edit)

Requests are immutable. To change one, its owner clicks **Recall request**. The request becomes *Recalled* and its files are set to expire in 7 days. The owner is then taken to a pre-filled form; leaving that page without resubmitting simply keeps the request withdrawn. Submitting creates a **new** request that links back to the one it replaces and goes through approval again.

The form starts with the original files listed as bubbles. Each has an **×** to remove it (with *undo*), and more files can be added (new uploads have an × too, on any request form). The form submits the IDs of the originals still kept:

- **Kept originals** are **taken over** by the new request: same file IDs, same stored files, now expiring on the new request's schedule (30 days from submission, and so on).
- **New uploads** get new file IDs. If an upload is byte-for-byte identical to one of the originals (same SHA-256), the new request inherits that original instead of storing a duplicate. Picking the same file twice stores it once.
- **Removed originals** stay with the recalled request and expire 7 days after the recall.

Replaced requests are hidden from the main list but stay reachable from the new request's page and via the *Recalled* filter.

### Printers

Add printers under `/admin/` → Printers. Staff can then choose one when queueing a print. Choosing a printer is optional.

## File expiry

Each request has a `files_expire_at` timestamp:

| Event | Files expire |
|-------|--------------|
| Request submitted | 30 days later |
| Request rejected or recalled | 7 days later |
| A print is started | 7 days later (resets on each print start) |
| A print fails | 7 days later (so a reprint still has the files) |

Approval alone doesn't change the expiry. The rules live in `prints/services.py`.

Downloads are blocked as soon as the deadline passes. The **worker** container deletes expired files every hour (`python manage.py purge_expired_files`, which also takes `--dry-run`). The database rows are kept for history.

## Email notifications

Email is optional and **opt-in**, managed by each user on their **Account** page:

1. They add an address (up to 3). It's sent a 6-digit code (valid 15 minutes, 5 tries). Codes are rate-limited to one a minute and 10 a day per user, so the form can't be used to spam someone.
2. Once the code is confirmed, they choose which notifications that **address** receives. Everything is off by default, and each address has its own choices.

Users can opt in to: request approved / rejected / completed, and print started / finished / failed. Staff can also opt in to "a new request is submitted". Nobody gets emailed about their own action, deactivated users get nothing, and an address can only be verified by one account. Every email includes a link to turn notifications off.

Emails are sent after the change is saved. If the mail service is down, the workflow carries on and the failure is logged.

With `[email] enabled = false` (the default), the app hides every email feature, and the Account page just says email isn't set up.

### Setting up Resend

1. Create a Resend account and **add a domain** you control (a subdomain such as `send.yourdomain` is tidy). Add the DNS records Resend shows you (SPF and DKIM) and wait for Resend to show the domain as verified.
2. Create an **API key** with sending access, and put it in `.env` as `EMAIL_PASSWORD=re_...`.
3. In `printpool.toml`:

   ```toml
   [email]
   enabled = true
   host = "smtp.resend.com"
   port = 587
   username = "resend"
   from = "PrintPool <printpool@send.yourdomain>"
   ```

4. Run `setup-server` again, then add your own address on the Account page to test.

Turning email on also allows the app to reach exactly `smtp.resend.com:587` (and nothing else) through the gateway. Any SMTP provider works the same way; if the uni provides a relay, change `host`, `port` and `username`.

## Contact links

Set `contact_staff_emails` and/or `contact_admin_emails` under `[app]` in `printpool.toml` to show plain `mailto:` links. Each is a list of addresses (`["a@uni.ac.uk", "b@uni.ac.uk"]`), and the link opens one email addressed to all of them. They open the person's own email app, and the server sends nothing. Everyone sees *Contact staff*; staff and admins also see *Contact an admin*. On their own request page, users get an *Email staff* link with the request's title and URL pre-filled. Leave a setting blank to hide its link.

## Live updates

Request pages and the dashboard refresh their status, prints, files and history every 20 seconds without reloading (`static/js/live.js`). Polling pauses while the tab is hidden, backs off if the server errors, and leaves a section alone while you're typing in it.

## Print cap

The monthly cap is in grams of filament: `print_cap_grams_per_month` under `[app]` in `printpool.toml`, with a per-user override in the admin. Usage is calculated live from prints that **succeeded or failed** during the current calendar month, using the grams staff enter when finishing a print (the slicer estimate if left blank). Students see their usage on their dashboard, and staff see the requester's usage on each request. Nothing is blocked by the cap yet; it's informational. See `cap_status()` / `filament_used()` in `prints/services.py`.

## HTTPS and visitor IPs

Cloudflare handles HTTPS. With `[ingress] https = true` (the default), Django marks cookies secure, redirects any
request that didn't arrive over HTTPS, and sends HSTS for `hsts_seconds` (1 hour to start; raise it to a year once
everything works).

Login throttling locks out a username + IP pair after 5 failures, so it needs each visitor's real IP. Behind the
cloudflared container that comes from Cloudflare's `CF-Connecting-IP` header; with only a published port, from the
gateway. Anyone with access to the machine itself could fake those headers. That's accepted here, because the
people running it are trusted staff.

## Backups

A backup is a zip named `printpool-YYYYMMDD-HHMMSS.zip`, holding a `pg_dump` of the database (`database.dump`) and,
with `include_files`, the uploaded model files (`media/`). With `encrypt = true` it's an **AES-256** zip using
`BACKUP_PASSWORD` from `.env`. Windows' built-in zip support can't open those, but [7-Zip](https://www.7-zip.org/)
can. Each dump is checked before it's kept, backups appear in the folder complete or not at all, and only their
owner can read them.

- **Scheduled:** set `[backups] enabled = true` with `frequency` (`hourly`, `daily`, `weekly`), `time`, `weekday`,
  `keep` (how many to keep; 0 keeps all), `encrypt`, `include_files`, and `[backups.location]` (a folder on this
  machine, or a named Docker volume). The worker makes them; `docker compose logs worker` shows when the next is due.
- **On demand:** `deploy/backup.sh` / `deploy\backup.ps1` makes one now in the same location, or elsewhere with
  `--to <folder>` (PowerShell: `-To`). Options: `--keep N`, `--include-files`/`--no-include-files`,
  `--encrypt`/`--no-encrypt`. To schedule these from cron or Windows Task Scheduler instead of the worker, see
  [deploy/SCHEDULING.md](deploy/SCHEDULING.md).
- **Restore:** `deploy/restore.sh <zip>` / `deploy\restore.ps1 <zip>` (add `--no-files` for the database only). It
  asks for confirmation, stops the app and worker, replaces the database (in one transaction, so a failure changes
  nothing) and the uploaded files, then starts them again. For a backup made with a different password, set
  `BACKUP_PASSWORD` in the environment for that run.

Copying backups to another machine (e.g. over SSH) is planned as an add-on.

## Monitoring

- **`/health/`** returns `{"status": "ok", ...}` with HTTP 200 when the app can reach the database and write uploads,
  and 503 otherwise. Every service has a Docker healthcheck, so `docker compose ps` shows the state at a glance.
- **Logs:** `docker compose logs -f [gateway|app|worker|db|cloudflared]`, capped at 5 × 10 MB per service.
- **Errors:** to report unhandled exceptions to Sentry, put `SENTRY_DSN` in `.env` and allow its host in an
  `[[egress]]` entry. No personal data is sent.

## Continuous integration

`.github/workflows/tests.yml` runs on every pull request and every push to `main`. It runs the `render_config`
tests, checks for missing migrations, runs Django's system checks and its production checklist (`check --deploy`),
runs the tests against Postgres 16 (including real backup and restore round trips), renders the example config and
validates the Compose files, and builds both images.

## Moving to another machine

1. On the old machine: `deploy/backup.sh --to <folder> --include-files`.
2. On the new one: clone the repo, copy `printpool.toml` and `.env` across (the same `BACKUP_PASSWORD` is needed to
   open the backup), and run `deploy/setup-server.sh`.
3. `deploy/restore.sh <the zip>`.

## Development

```sh
docker compose run --rm app python manage.py test               # Django tests (after setup-server has run once)
python3 deploy/test_render_config.py                            # deployment config tests (standard library only)
docker compose run --rm -u root -v "$PWD:/app" app python manage.py makemigrations   # after model changes
```

## Layout

```
accounts/   User model with roles, forced password change, admin account creation
notifications/  opt-in email addresses (OTP verification), per-address preferences, sending
prints/     requests, files, prints, printers, audit log
  services.py   all workflow rules: transitions, permissions, expiry, purge, cap
  validators.py STL/3MF checks (extension, size, file signature)
ops/        backups (create, restore, keep-N), backup schedule, the worker loop
templates/  server-rendered HTML
static/js/  live.js (polling live updates), file-picker.js (removable file bubbles)
printpool.example.toml   annotated deployment settings
deploy/
  setup-server.sh/.ps1   deploy or update
  backup.sh/.ps1, restore.sh/.ps1, SCHEDULING.md
  render_config.py       printpool.toml -> .generated/ + compose.override.yaml (with tests)
  gateway/               nginx image for the gateway (and its DNS setup for egress)
  cloudflared/           config.yml + credentials for a locally managed tunnel (git-ignored)
docs/       DEPLOY.md: step-by-step deployment guide
```

## Next steps

- Printer API polling (OctoPrint / Moonraker / Bambu) as a worker service
- Enforce the cap (warn at submit, block at approval), plus custom periods
- Scheduling, using `Print.priority`
