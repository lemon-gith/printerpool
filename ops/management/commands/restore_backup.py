import getpass
import sys

import pyzipper
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ops.backups import BackupError, is_encrypted, restore_backup


class Command(BaseCommand):
    help = (
        "Replace the database (and uploaded files, if the backup has them) with a backup. Stop the app and worker "
        "first; deploy/restore.sh / .ps1 do that for you."
    )

    def add_arguments(self, parser):
        parser.add_argument("path", help="the backup zip")
        parser.add_argument("--no-files", action="store_true", help="restore only the database, not uploaded files")
        parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")

    def handle(self, *args, path, no_files, yes, **options):
        if not yes:
            if not sys.stdin.isatty():
                raise CommandError("Add --yes to confirm: this replaces everything in the database.")
            answer = input(f"This replaces everything in the database with {path}. Type 'restore' to continue: ")
            if answer.strip() != "restore":
                raise CommandError("Cancelled.")
        password = settings.BACKUP_PASSWORD
        try:
            if not password and is_encrypted(path) and sys.stdin.isatty():
                password = getpass.getpass("Backup password: ")
            result = restore_backup(path, password=password, restore_files=not no_files)
        except (BackupError, OSError, pyzipper.BadZipFile) as e:
            raise CommandError(f"Restore failed: {e}")
        self.stdout.write(self.style.SUCCESS(
            f"Restored the database from {path}"
            + (f" and {result['files_restored']} uploaded file(s)." if result["files_restored"] else ".")
        ))
        if result["files_in_backup"] and not result["files_restored"]:
            self.stdout.write(f"(The backup also has {result['files_in_backup']} uploaded file(s); they weren't restored.)")
