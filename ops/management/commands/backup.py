from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ops.backups import BackupError, create_backup


class Command(BaseCommand):
    help = "Make a backup now (database, and uploaded files if configured) into the backup folder."

    def add_arguments(self, parser):
        parser.add_argument("--output", default=settings.BACKUP_DIR, help="folder to write to (default: %(default)s)")
        parser.add_argument("--keep", type=int, default=settings.BACKUP_KEEP,
                            help="keep this many backups in the folder, deleting older ones; 0 keeps all")
        files = parser.add_mutually_exclusive_group()
        files.add_argument("--include-files", dest="include_files", action="store_true", default=None)
        files.add_argument("--no-include-files", dest="include_files", action="store_false")
        encrypt = parser.add_mutually_exclusive_group()
        encrypt.add_argument("--encrypt", dest="encrypt", action="store_true", default=None)
        encrypt.add_argument("--no-encrypt", dest="encrypt", action="store_false")

    def handle(self, *args, output, keep, include_files, encrypt, **options):
        if include_files is None:
            include_files = settings.BACKUP_INCLUDE_FILES
        if encrypt is None:
            encrypt = settings.BACKUP_ENCRYPT
        if keep < 0:
            raise CommandError("--keep can't be negative")
        try:
            path, removed = create_backup(
                output, encrypt=encrypt, password=settings.BACKUP_PASSWORD, include_files=include_files, keep=keep
            )
        except BackupError as e:
            raise CommandError(f"Backup failed: {e}")
        size_mb = path.stat().st_size / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(
            f"Backup written: {path.name} ({size_mb:.1f} MB, {'encrypted' if encrypt else 'not encrypted'}"
            f"{', with uploaded files' if include_files else ''})"
        ))
        for old in removed:
            self.stdout.write(f"Removed old backup {old.name}")
