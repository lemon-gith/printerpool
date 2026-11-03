import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User


class Command(BaseCommand):
    help = (
        "Create the first admin from DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_PASSWORD, for unattended setup "
        "(e.g. cloud-init). Does nothing if that account exists. The password must be changed at first login, "
        "since it passed through server metadata."
    )

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        if not username or not password:
            raise CommandError("Set DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD.")
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Admin account “{username}” already exists; leaving it unchanged.")
            return
        user = User(username=username, role=User.Role.ADMIN, must_change_password=True)
        try:
            validate_password(password, user)
        except ValidationError as e:
            raise CommandError("That password is too weak: " + " ".join(e.messages))
        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS(
            f"Created admin “{username}”. You'll be asked to choose a new password when you first log in."
        ))
