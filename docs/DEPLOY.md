# Deploying PrintPool

PrintPool runs entirely in Docker and changes nothing on the machine outside its own folder: no packages, firewall
rules, services or scheduled tasks. That makes it safe to run on a shared PC. Visitors reach it through a
**Cloudflare Tunnel**, so the machine needs no open ports and no public IP address.

Budget about half an hour the first time.

## 0. What you need

- **Docker:** Docker Desktop on Windows or macOS, Docker Engine with the compose plugin on Linux. That's the only
  thing to install.
- **A domain on Cloudflare** (for the hostname, e.g. `print.yourdomain.com`, and the tunnel).
- **This repository:** `git clone https://github.com/lemon-gith/printerpool` (or download it).

## 1. Create the tunnel (Cloudflare)

Pick one:

- **Managed in the dashboard (simplest).** Zero Trust → Networks → Tunnels → *Create a tunnel* (type
  *Cloudflared*). Copy the **token** (the long string in the install command shown; you don't run that command).
  Add a **public hostname**, e.g. `print.yourdomain.com`, with service type **HTTP** and URL **`gateway:8080`**.
- **Managed locally with a `config.yml`.** Set up the tunnel the way you normally would, with its ingress for your
  hostname pointing at `http://gateway:8080`. Then put `config.yml` and the tunnel's credentials `.json` in
  `deploy/cloudflared/`, following [deploy/cloudflared/README.md](../deploy/cloudflared/README.md).

`gateway:8080` is PrintPool's gateway as seen from the cloudflared container on PrintPool's Docker network.

## 2. Configure

```sh
cp printpool.example.toml printpool.toml          # Windows: Copy-Item printpool.example.toml printpool.toml
```

Edit `printpool.toml`. Every setting is explained in the file. At minimum:

```toml
[site]
domain = "print.yourdomain.com"     # the tunnel's public hostname
time_zone = "Europe/London"

[cloudflared]
enabled = true
mode = "token"                      # or "config" for a locally managed tunnel
```

Leave email, backups and `[[egress]]` off for now if you like; you can turn them on later.

## 3. Deploy

```sh
deploy/setup-server.sh                            # Windows: .\deploy\setup-server.ps1
```

The first run creates `.env` with a fresh secret key and database password, then stops if a secret your settings
need is missing. With `mode = "token"`, that's the tunnel token: put it in `.env` as `CLOUDFLARE_TUNNEL_TOKEN=...`
and run `setup-server` again. It then builds everything, starts it, and waits until it's healthy.

If PowerShell refuses to run the script: `powershell -ExecutionPolicy Bypass -File .\deploy\setup-server.ps1`.

Create your admin account, then visit `https://print.yourdomain.com`:

```sh
docker compose exec app python manage.py createsuperuser
```

Instead, you can set `DJANGO_SUPERUSER_USERNAME` and `DJANGO_SUPERUSER_PASSWORD` when running `setup-server` the
first time; that admin then has to change the password at first login.

## 4. Add people

- **Lots of accounts:** `/admin/` → Users → **Import users (CSV)**. The page explains the format. You get a one-time
  sheet of temporary passwords to print as slips or download as a CSV. Hand them out, then destroy the sheet.
  Everyone picks their own password at first login.
- **One account:** `/admin/` → Users → *Add user*, which shows the same kind of sheet for that one person.
- **Lost password:** select the user → *Reset password*.
- **Printers:** `/admin/` → Printers.
- **Contact links** *(optional)*: `[app] contact_staff_emails` / `contact_admin_emails` in `printpool.toml`.

## 5. Email *(optional)*

1. In [Resend](https://resend.com), add your sending domain (e.g. `send.yourdomain.com`), add the DNS records it
   shows, and wait until it says *verified*. Create an API key.
2. Put the key in `.env` as `EMAIL_PASSWORD=re_...`.
3. In `printpool.toml`, set `[email] enabled = true` and `from = "PrintPool <printpool@send.yourdomain.com>"`.
4. Run `setup-server` again, then add your own address on the **Account** page. A code should arrive within a few
   seconds.

This also allows the app to reach `smtp.resend.com:587` (and nothing else) through the gateway. If the code doesn't
arrive, `docker compose logs gateway` shows whether the connection was made, and `docker compose logs app` shows
any error from the mail server. If the gateway can't look up `smtp.resend.com` on your network, set
`[gateway] dns` to your network's DNS servers.

## 6. Backups

Pick one or both:

- **Automatic:** in `printpool.toml`, set `[backups] enabled = true` and choose `frequency`, `time`, `keep`,
  `encrypt` and `include_files`. Then pick `[backups.location]`: a folder on this machine (`type = "folder"`) or a
  named Docker volume (`type = "volume"`). With `encrypt = true`, put a password in `.env` as `BACKUP_PASSWORD`,
  and **keep a copy of it somewhere safe**: without it, the backups can't be opened. Run `setup-server` again.
  `docker compose logs worker` shows when the next backup is due.
- **On demand or from your own scheduler:** `deploy/backup.sh` / `deploy\backup.ps1`, optionally with
  `--to <folder>`. For cron or Windows Task Scheduler, see [deploy/SCHEDULING.md](../deploy/SCHEDULING.md).

Encrypted backups are AES-256 zips; on Windows, open them with 7-Zip.

**Practise a restore** once, so you know it works before you need it:

```sh
deploy/restore.sh backups/printpool-YYYYMMDD-HHMMSS.zip      # Windows: .\deploy\restore.ps1 <zip>
```

It asks you to type `restore`, then stops the app, replaces the database (and uploaded files, if the backup has
them) in one go, and starts the app again.

## 7. After a while

Once everything has worked for a week or so, set `[ingress] hsts_seconds = 31536000` (a year) and run
`setup-server` again. Browsers will then always use HTTPS for the site. It starts at an hour because a long value is
hard to undo.

## Updating

```sh
git pull
deploy/setup-server.sh                            # Windows: .\deploy\setup-server.ps1
```

This rebuilds, restarts (migrations run automatically) and waits for everything to be healthy. There are a few
seconds of downtime while the app restarts. To also get the latest cloudflared and nginx images:
`docker compose pull cloudflared` and `docker compose build --pull gateway`, then run `setup-server` again.

## Stopping and removing

- Stop: `docker compose stop`. Start again: `docker compose start` (or `setup-server`).
- Remove the containers but keep the data: `docker compose down`.
- **Delete everything, including the database and uploads:** `docker compose down -v`. Make a backup first.

## Testing locally without Cloudflare

In `printpool.toml`: `[site] domain = "localhost"`, `[cloudflared] enabled = false`,
`[ingress] publish = "127.0.0.1:8000"` and `https = false`. Run `setup-server`, then open `http://localhost:8000`.

You can also keep cloudflared running *outside* Docker: disable the container, publish `127.0.0.1:8000`, and point
that cloudflared's ingress at `http://localhost:8000`. Keep `[ingress] https = true` (the default), so the app
trusts Cloudflare's headers.

## Launch checklist

- [ ] `setup-server` finishes with app, worker and gateway healthy; `docker compose ps` shows cloudflared running
- [ ] `https://<domain>` loads; you can log in as admin
- [ ] Accounts imported; a test student can log in and submit a request
- [ ] Staff can approve, queue, start and finish a print
- [ ] After a deliberately wrong login, *Admin → Access attempts* shows your real IP (not a 172.x Docker address)
- [ ] *(email)* A verification code arrives; an opted-in notification arrives
- [ ] *(backups)* A backup appears in the chosen location; you've practised a restore; `BACKUP_PASSWORD` is stored
      somewhere safe
