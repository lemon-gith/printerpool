# Deploying PrintPool

A start-to-finish checklist for running PrintPool on a small Hetzner Cloud server. Most of the server work is
done by `deploy/setup-server.sh`; this guide covers that plus the steps that happen elsewhere (Hetzner's
console, DNS, Resend, your backup server, monitoring).

Budget about an hour the first time. Steps marked *(optional)* can be done later.

## 0. What you need

- A **domain or subdomain** for the site, e.g. `print.example.ac.uk`. A uni subdomain usually needs a request
  to IT; a cheap domain of your own works just as well.
- An **SSH key** on your computer (`ssh-keygen -t ed25519` if you don't have one).

## 1. Create the server (Hetzner console)

1. In the Hetzner Cloud console, create a project, then **Add Server**:
   - **Location:** an EU one (Falkenstein, Nuremberg or Helsinki), since you're storing student data.
   - **Image:** Ubuntu 24.04.
   - **Type:** the smallest shared-vCPU x86 type (CX22/CX23, 2 vCPU, 4 GB RAM) is plenty.
   - **SSH key:** add your public key. Don't use a root password.
   - *(optional)* **Backups:** tick it for nightly whole-server snapshots (+20% of the server price). This is
     a separate safety net on top of PrintPool's own database backups.
2. **Firewalls → Create Firewall**, with inbound rules:
   - TCP 22 (SSH). Restrict it to your own IP address if that's practical.
   - TCP 80 and TCP 443 (web), and UDP 443 (HTTP/3), from anywhere.

   Apply it to the server. The setup script also turns on the server's own firewall (ufw); the Hetzner one is
   an outer layer that Docker can't bypass.

## 2. Point the domain at the server

At your DNS provider, add an `A` record for your (sub)domain with the server's IPv4 address, and an `AAAA`
record with its IPv6 address. Check it from your computer before continuing:

```sh
dig +short print.example.ac.uk    # should print the server's IP
```

Caddy needs this to get an HTTPS certificate.

## 3. Run the setup script

```sh
ssh root@<server-ip>
git clone https://github.com/lemon-gith/printerpool /opt/printerpool
/opt/printerpool/deploy/setup-server.sh print.example.ac.uk
```

It installs Docker, automatic security updates and the firewall, and switches SSH to keys only (only if it
finds a key, so you can't be locked out). It then writes `.env` with a fresh secret key and database password,
set up for HTTPS behind Caddy, creates `deploy/jobs.env` and an SSH key for off-site backups, installs the
scheduled jobs (`/etc/cron.d/printpool`) and log rotation, and starts everything.

It's safe to run again: existing `.env`, `deploy/jobs.env` and keys are never overwritten.

Then create your admin account and open the site:

```sh
cd /opt/printerpool
docker compose exec app python manage.py createsuperuser
```

Visit `https://print.example.ac.uk`. On the very first start, the certificate can take a minute.

## 4. Add people

- **Lots of accounts:** `/admin/` → Users → **Import users (CSV)**. The page explains the format. You get a
  one-time sheet of temporary passwords to print as slips or download as a CSV. Hand them out, then destroy
  the sheet. Everyone picks their own password at first login.
- **One account:** `/admin/` → Users → Add user, which shows the same kind of sheet for that one person.
- **Lost password:** select the user → *Reset password*.
- **Printers:** `/admin/` → Printers.
- *(optional)* **Contact links:** set `CONTACT_STAFF_EMAILS` / `CONTACT_ADMIN_EMAILS` in `.env`.

After changing `.env`, apply it with `docker compose --profile proxy up -d`.

## 5. Email *(optional, but needed for notifications)*

1. Create a [Resend](https://resend.com) account and **add a domain**. A subdomain like `send.example.ac.uk`
   keeps it tidy. Add the DNS records Resend shows you, and wait until Resend says the domain is verified.
2. Create an **API key** with sending access.
3. In `/opt/printerpool/.env`:

   ```
   EMAIL_HOST=smtp.resend.com
   EMAIL_PORT=587
   EMAIL_HOST_USER=resend
   EMAIL_HOST_PASSWORD=re_your_api_key
   DEFAULT_FROM_EMAIL=PrintPool <printpool@send.example.ac.uk>
   ```

4. `docker compose --profile proxy up -d`, then add your own address on the **Account** page. You should get
   a code within a few seconds.

Keep port 587: Hetzner blocks outgoing 25 and 465 on new accounts.

## 6. Off-site backups

The server keeps 14 nightly database dumps in `/var/backups/printpool`. Once a week, `deploy/offsite.sh` copies
the newest one elsewhere, so that losing the server doesn't lose the data.

**Over SSH** (simplest, to any Linux machine you control):

1. On the **receiving** machine, create a user and folder for backups, and add the key the setup script
   printed (also in `/root/.ssh/printpool_backup.pub` on the PrintPool server) to that user's
   `~/.ssh/authorized_keys`, prefixed with `restrict ` so the key can copy files but can't open a shell:

   ```
   restrict ssh-ed25519 AAAA... printpool-backup@print.example.ac.uk
   ```

2. On the **PrintPool** server, in `/opt/printerpool/deploy/jobs.env`:

   ```
   OFFSITE_SSH_TARGET=backup@your-server.example.com:/srv/backups/printpool
   OFFSITE_SSH_KEY=/root/.ssh/printpool_backup
   ```

**Over HTTPS** instead (or as well): set `OFFSITE_HTTP_URL` and `OFFSITE_API_KEY`. Each file is uploaded with
`PUT <url>/<filename>` and an `Authorization: Bearer <key>` header.

Test both scripts by hand, and check the file arrived:

```sh
/opt/printerpool/deploy/backup.sh && /opt/printerpool/deploy/offsite.sh
```

**Practise a restore** once, so you know it works before you need it (README → Backups → Restoring).

## 7. Monitoring

So you hear about problems before your users do:

1. **Is the site up?** Add an HTTP monitor for `https://print.example.ac.uk/health/` in a free uptime service
   (UptimeRobot, Better Stack, or Healthchecks.io's own). It returns `200` when the app can reach its
   database and write uploads, and `503` otherwise.
2. **Did the scheduled jobs run?** In [Healthchecks.io](https://healthchecks.io) (free), create three checks
   and paste their ping URLs into `deploy/jobs.env`:

   | Variable | Schedule | Grace time |
   |---|---|---|
   | `PURGE_PING_URL` | every hour | 30 minutes |
   | `BACKUP_PING_URL` | every day | 2 hours |
   | `OFFSITE_PING_URL` | every week | 1 day |

   Each run reports its start, then success or failure. You get an email if a job fails **or stops running**.
3. *(optional)* **Errors:** create a free Sentry project and put its DSN in `.env` as `SENTRY_DSN`. Crashes are
   then reported with their stack traces. Personal data (usernames, IPs, request bodies) isn't sent.

Container logs are capped at 50 MB per service; read them with `docker compose logs app`. The job logs are
`/var/log/printpool-*.log` (rotated weekly).

## 8. After a week or so

Once HTTPS has been working without problems, make browsers remember to always use it. In `.env`, set
`DJANGO_HSTS_SECONDS=31536000` (a year), then `docker compose --profile proxy up -d`. It starts at an hour
because a long value is hard to undo if the site ever needs to go back to plain HTTP.

## Updating

```sh
/opt/printerpool/deploy/update.sh
```

This pulls the latest code, rebuilds, restarts (migrations run automatically), removes old images and
refreshes the scheduled jobs. There's a few seconds of downtime while the app container restarts.

## Launch checklist

- [ ] `https://<domain>` loads with a valid certificate; `http://` redirects to it
- [ ] Admin account created; accounts imported; a test student can log in and submit a request
- [ ] Staff can approve, queue, start and finish a print
- [ ] *(if using email)* Verification code arrives; an opted-in notification arrives
- [ ] `backup.sh` and `offsite.sh` ran by hand; the file reached the backup server; a restore was practised
- [ ] Uptime monitor on `/health/` is green; Healthchecks.io shows all three jobs reporting
- [ ] Hetzner Cloud Firewall attached; SSH password login is off (`ssh -o PubkeyAuthentication=no root@<ip>` is refused)
