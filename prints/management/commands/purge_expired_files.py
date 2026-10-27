from django.core.management.base import BaseCommand

from prints.services import purge_expired_files


class Command(BaseCommand):
    help = "Delete uploaded model files for requests whose files have expired. Safe to run repeatedly."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List what would be purged without deleting.")

    def handle(self, *args, dry_run=False, **options):
        purged = purge_expired_files(dry_run=dry_run)
        verb = "Would purge" if dry_run else "Purged"
        for req in purged:
            self.stdout.write(f"{verb} files for request {req.pk} ({req.title}), expired {req.files_expire_at:%Y-%m-%d %H:%M}")
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(purged)} request(s)."))
