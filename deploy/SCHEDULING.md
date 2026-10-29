# Scheduling backups

There are two ways to get regular backups. Pick one.

## Option 1: let PrintPool do it (easiest)

In `printpool.toml`, set `[backups] enabled = true` and choose the `frequency`, `time`, `keep`, `encrypt`,
`include_files` and `[backups.location]`. Then run `deploy/setup-server.sh` (or `.ps1`) again. The worker
container makes the backups; nothing is set up on the machine itself. With `encrypt = true`, put the password
in `.env` as `BACKUP_PASSWORD`.

Check it's working with `docker compose logs worker`: it logs when the next backup is due and each one it makes.

## Option 2: your own scheduler

Use this if you'd rather control timing from the machine, or back up to a folder outside `printpool.toml`'s
location. Leave `[backups] enabled = false` and schedule `deploy/backup.sh` / `deploy\backup.ps1`. Backups are
encrypted per `[backups] encrypt` (password from `.env`), and `--keep N` limits how many are kept in the folder
(0 keeps all).

### Linux / macOS: cron

Run `crontab -e` as a user that can use Docker, and add a line. For example, every night at 03:30, keeping 14
backups in `/srv/printpool-backups`:

```cron
30 3 * * *  /path/to/printerpool/deploy/backup.sh --to /srv/printpool-backups --keep 14 >> /path/to/printerpool/backups.log 2>&1
```

Other frequencies: `0 * * * *` hourly, `30 3 * * 0` Sundays at 03:30. Cron runs with a minimal `PATH`; if it
can't find `docker`, add `PATH=/usr/local/bin:/usr/bin:/bin` as the first line of the crontab.

### Windows: Task Scheduler

1. Open **Task Scheduler** → **Create Task…**
2. **General:** name it "PrintPool backup". Choose **Run whether user is logged on or not**, as a user that can
   use Docker Desktop. Docker Desktop must be running, so set it to start when you sign in.
3. **Triggers → New:** *Daily* (or *Weekly*), at your chosen time.
4. **Actions → New:** *Start a program*
   - Program: `powershell.exe`
   - Arguments: `-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\printerpool\deploy\backup.ps1" -To "D:\PrintPoolBackups" --keep 14`
   - Start in: `C:\path\to\printerpool`
5. **Settings:** tick **Run task as soon as possible after a scheduled start is missed**.

Run it once from the Task Scheduler list (**Run**) and check that a `printpool-….zip` appears in the folder.

## Opening and restoring backups

Backups are zips named `printpool-YYYYMMDD-HHMMSS.zip`, holding `database.dump` (and `media/` with uploaded
files, if included). Encrypted ones use AES-256: Windows' built-in zip support can't open those, so use
[7-Zip](https://www.7-zip.org/) (or `7z x` on Linux/macOS) if you want to look inside.

To restore one, use `deploy/restore.sh <zip>` or `deploy\restore.ps1 <zip>` (see the README).
