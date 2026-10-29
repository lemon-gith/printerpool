import logging
import signal
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from ops.backups import BackupError, create_backup
from ops.schedule import next_run
from prints.services import purge_expired_files

log = logging.getLogger("printpool.worker")

HEARTBEAT = Path("/tmp/printpool-worker.heartbeat")
PURGE_EVERY = timedelta(hours=1)
TICK_SECONDS = 15


class Command(BaseCommand):
    help = "Background jobs: purge expired files hourly, and make scheduled backups ([backups] in printpool.toml)."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="health check: exit 0 if the worker is running")

    def handle(self, *args, check, **options):
        if check:
            # Healthy if the loop has ticked recently (it touches the heartbeat file every few seconds).
            age = time.time() - HEARTBEAT.stat().st_mtime if HEARTBEAT.exists() else float("inf")
            raise SystemExit(0 if age < 180 else 1)

        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        now = timezone.now()
        next_purge = now
        next_backup = self._next_backup(now)
        log.info("Worker started. %s", f"Next backup: {timezone.localtime(next_backup):%Y-%m-%d %H:%M %Z}"
                    if next_backup else "Scheduled backups are off.")

        while not stopping:
            HEARTBEAT.touch()
            now = timezone.now()
            if now >= next_purge:
                self._purge()
                next_purge = now + PURGE_EVERY
            if next_backup and now >= next_backup:
                self._backup()
                next_backup = self._next_backup(timezone.now())
                log.info("Next backup: %s", f"{timezone.localtime(next_backup):%Y-%m-%d %H:%M %Z}")
            for _ in range(TICK_SECONDS):
                if stopping:
                    break
                time.sleep(1)
        log.info("Worker stopped.")

    def _next_backup(self, now):
        if not settings.BACKUP_ENABLED:
            return None
        return next_run(now, settings.BACKUP_FREQUENCY, settings.BACKUP_TIME, settings.BACKUP_WEEKDAY)

    def _purge(self):
        try:
            purged = purge_expired_files()
            if purged:
                log.info("Purged expired files for %d request(s).", len(purged))
        except Exception:
            log.exception("Purging expired files failed")

    def _backup(self):
        try:
            path, removed = create_backup(
                settings.BACKUP_DIR,
                encrypt=settings.BACKUP_ENCRYPT,
                password=settings.BACKUP_PASSWORD,
                include_files=settings.BACKUP_INCLUDE_FILES,
                keep=settings.BACKUP_KEEP,
            )
            log.info("Backup written: %s%s", path.name, f" (removed {len(removed)} old)" if removed else "")
        except (BackupError, OSError):
            log.exception("Scheduled backup failed")
