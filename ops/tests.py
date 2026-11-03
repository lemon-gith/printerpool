import io
import os
import shutil
import tempfile
import time
import zoneinfo
from datetime import datetime
from pathlib import Path
from unittest import mock, skipUnless

import pyzipper
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from accounts.models import User
from ops import backups
from ops.management.commands import run_worker
from ops.schedule import next_run
from prints.models import PrintRequest, RequestFile

LONDON = zoneinfo.ZoneInfo("Europe/London")
HAS_PG_TOOLS = bool(shutil.which("pg_dump") and shutil.which("pg_restore"))


def london(*args):
    return datetime(*args, tzinfo=LONDON)


@override_settings(TIME_ZONE="Europe/London")
class ScheduleTests(SimpleTestCase):
    def test_daily(self):
        self.assertEqual(next_run(london(2026, 10, 29, 2, 0), "daily", "03:30"), london(2026, 10, 29, 3, 30))
        self.assertEqual(next_run(london(2026, 10, 29, 3, 30), "daily", "03:30"), london(2026, 10, 30, 3, 30))

    def test_weekly(self):
        # 29 Oct 2026 is a Thursday; the next Sunday is 1 Nov.
        self.assertEqual(next_run(london(2026, 10, 29, 12, 0), "weekly", "03:30", "sunday"), london(2026, 11, 1, 3, 30))
        self.assertEqual(next_run(london(2026, 11, 1, 3, 29), "weekly", "03:30", "sunday"), london(2026, 11, 1, 3, 30))

    def test_hourly_uses_the_minute(self):
        self.assertEqual(next_run(london(2026, 10, 29, 14, 10), "hourly", "03:30"), london(2026, 10, 29, 14, 30))
        self.assertEqual(next_run(london(2026, 10, 29, 14, 45), "hourly", "03:30"), london(2026, 10, 29, 15, 30))

    def test_wall_clock_time_survives_clock_changes(self):
        # Clocks go back at 02:00 on 25 Oct 2026 (BST -> GMT): 03:30 is still 03:30 local, an hour later in UTC.
        before = next_run(london(2026, 10, 23, 12, 0), "daily", "03:30")  # 24 Oct, still BST
        after = next_run(london(2026, 10, 25, 12, 0), "daily", "03:30")   # 26 Oct, GMT
        on_the_day = next_run(london(2026, 10, 24, 12, 0), "daily", "03:30")  # 25 Oct, just after the change
        self.assertEqual((on_the_day.day, on_the_day.hour, on_the_day.utcoffset().total_seconds()), (25, 3, 0))
        self.assertEqual((before.hour, after.hour), (3, 3))
        self.assertEqual(before.utcoffset().total_seconds(), 3600)
        self.assertEqual(after.utcoffset().total_seconds(), 0)


class PruneTests(SimpleTestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        for day in range(1, 6):
            (self.folder / f"printpool-202610{day:02d}-033000.zip").write_bytes(b"x")
        (self.folder / "unrelated.zip").write_bytes(b"x")

    def names(self):
        return sorted(p.name for p in self.folder.iterdir())

    def test_keeps_the_newest(self):
        removed = backups.prune(self.folder, keep=2)
        self.assertEqual([p.name for p in removed], [f"printpool-202610{d:02d}-033000.zip" for d in (1, 2, 3)])
        self.assertEqual(self.names(), ["printpool-20261004-033000.zip", "printpool-20261005-033000.zip", "unrelated.zip"])

    def test_zero_keeps_everything(self):
        self.assertEqual(backups.prune(self.folder, keep=0), [])
        self.assertEqual(len(self.names()), 6)

    def test_old_partial_files_are_cleared(self):
        stale = self.folder / ".printpool-20261001-033000.zip.partial"
        fresh = self.folder / ".printpool-20261029-033000.zip.partial"
        stale.write_bytes(b"x")
        fresh.write_bytes(b"x")
        two_days_ago = time.time() - 2 * 86400
        os.utime(stale, (two_days_ago, two_days_ago))
        backups.prune(self.folder, keep=0)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())  # might be a backup in progress


@skipUnless(HAS_PG_TOOLS, "needs pg_dump and pg_restore")
class BackupRoundTripTests(TransactionTestCase):
    """Real pg_dump/pg_restore against the test database (TransactionTestCase, so they can see the data)."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.media = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out)
        self.addCleanup(shutil.rmtree, self.media)
        storages = {
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": str(self.media)}},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        }
        self.settings_override = override_settings(STORAGES=storages, BACKUP_PASSWORD="correct horse", BACKUP_DIR=str(self.out))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.alice = User.objects.create_user("alice", password="x")
        req = PrintRequest.objects.create(user=self.alice, title="Bracket")
        (self.media / "models").mkdir()
        (self.media / "models" / "part.stl").write_bytes(b"solid part\nendsolid part\n")
        RequestFile.objects.create(request=req, file="models/part.stl", original_name="part.stl", size=24, sha256="0" * 64)

    def backup(self, **kw):
        options = {"encrypt": True, "password": "correct horse", "include_files": True, "keep": 0} | kw
        path, _ = backups.create_backup(self.out, **options)
        return path

    def test_encrypted_backup_and_restore(self):
        path = self.backup()
        self.assertRegex(path.name, r"^printpool-\d{8}-\d{6}\.zip$")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        with pyzipper.AESZipFile(path) as zf:
            self.assertEqual(sorted(zf.namelist()), ["database.dump", "manifest.json", "media/models/part.stl"])
            with self.assertRaises(RuntimeError):
                zf.read("database.dump")  # unreadable without the password
            zf.setpassword(b"correct horse")
            self.assertEqual(zf.read("media/models/part.stl"), b"solid part\nendsolid part\n")

        # Lose data, then restore it.
        PrintRequest.objects.all().delete()
        User.objects.filter(username="alice").delete()
        (self.media / "models" / "part.stl").unlink()
        result = backups.restore_backup(path, password="correct horse")

        self.assertTrue(User.objects.filter(username="alice").exists())
        self.assertEqual(PrintRequest.objects.get().title, "Bracket")
        self.assertEqual((self.media / "models" / "part.stl").read_bytes(), b"solid part\nendsolid part\n")
        self.assertEqual(result["files_restored"], 1)

    def test_unencrypted_without_files(self):
        path = self.backup(encrypt=False, password="", include_files=False)
        with pyzipper.AESZipFile(path) as zf:
            self.assertEqual(sorted(zf.namelist()), ["database.dump", "manifest.json"])
            self.assertTrue(zf.read("database.dump").startswith(b"PGDMP"))  # readable without a password

    def test_wrong_or_missing_password(self):
        path = self.backup()
        with self.assertRaisesMessage(backups.BackupError, "wrong password"):
            backups.restore_backup(path, password="nope")
        with self.assertRaisesMessage(backups.BackupError, "encrypted"):
            backups.restore_backup(path, password="")
        self.assertTrue(User.objects.filter(username="alice").exists())  # untouched

    def test_encryption_needs_a_password(self):
        with self.assertRaisesMessage(backups.BackupError, "BACKUP_PASSWORD"):
            self.backup(password="")
        self.assertEqual(list(self.out.iterdir()), [])

    def test_failed_dump_leaves_nothing_behind(self):
        with override_settings(DATABASES={"default": {**self._db(), "PASSWORD": "wrong"}}):
            with self.assertRaisesMessage(backups.BackupError, "pg_dump failed"):
                self.backup()
        self.assertEqual(list(self.out.iterdir()), [])

    def test_backup_and_restore_commands(self):
        out = io.StringIO()
        call_command("backup", "--keep", "1", stdout=out)
        call_command("backup", "--keep", "1", "--no-include-files", stdout=out)
        self.assertIn("Backup written", out.getvalue())
        zips = backups.list_backups(self.out)
        self.assertEqual(len(zips), 1)  # --keep 1 removed the first (same-second names are fine: newest wins)
        with self.assertRaisesMessage(CommandError, "--yes"):
            call_command("restore_backup", str(zips[0]), stdout=io.StringIO())
        PrintRequest.objects.all().delete()
        User.objects.filter(username="alice").delete()
        call_command("restore_backup", str(zips[0]), "--yes", stdout=out)
        self.assertTrue(User.objects.filter(username="alice").exists())

    def test_not_a_backup(self):
        bogus = self.out / "printpool-20260101-000000.zip"
        with pyzipper.ZipFile(bogus, "w") as zf:
            zf.writestr("hello.txt", "hi")
        with self.assertRaisesMessage(backups.BackupError, "isn't a PrintPool backup"):
            backups.restore_backup(bogus, password="")

    @staticmethod
    def _db():
        from django.conf import settings
        return dict(settings.DATABASES["default"])


class WorkerTests(SimpleTestCase):
    def test_health_check_follows_the_heartbeat(self):
        beat = Path(tempfile.mkdtemp()) / "heartbeat"
        self.addCleanup(shutil.rmtree, beat.parent)
        with mock.patch.object(run_worker, "HEARTBEAT", beat):
            with self.assertRaises(SystemExit) as missing:
                call_command("run_worker", "--check")
            beat.touch()
            with self.assertRaises(SystemExit) as fresh:
                call_command("run_worker", "--check")
            old = time.time() - 600
            os.utime(beat, (old, old))
            with self.assertRaises(SystemExit) as stale:
                call_command("run_worker", "--check")
        self.assertEqual((missing.exception.code, fresh.exception.code, stale.exception.code), (1, 0, 1))


@override_settings(EMAIL_ENABLED=False)
class EmailDisabledTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="x")
        self.client.force_login(self.user)

    def test_account_page_says_email_is_off(self):
        page = self.client.get(reverse("account"))
        self.assertContains(page, "Email isn't set up on this server")
        self.assertNotContains(page, "Add an address")

    def test_email_pages_are_gone(self):
        self.assertEqual(self.client.post(reverse("email_add"), {"email": "a@example.com"}).status_code, 404)
        self.assertFalse(self.user.email_addresses.exists())

    def test_no_notifications_are_queued(self):
        from notifications.services import notify

        req = PrintRequest.objects.create(user=self.user, title="T")
        with self.captureOnCommitCallbacks() as callbacks:
            notify("request_approved", req)
        self.assertEqual(callbacks, [])
