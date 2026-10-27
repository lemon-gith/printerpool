import secrets

from django.contrib.auth.models import AbstractUser, UserManager as DjangoUserManager
from django.db import models

# Unambiguous characters only, since these passwords are read out / written down by hand.
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_initial_password(length=12):
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


class UserManager(DjangoUserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("role", User.Role.ADMIN)
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    class Role(models.TextChoices):
        USER = "user", "User"
        STAFF = "staff", "Staff"
        ADMIN = "admin", "Admin"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.USER)
    must_change_password = models.BooleanField(
        default=False, help_text="Forces a password change on next login."
    )
    print_cap_grams_override = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Monthly filament cap in grams. Leave blank to use the site default.",
    )

    objects = UserManager()

    def save(self, *args, **kwargs):
        # `role` is the single source of truth; Django's own flags just mirror it so that
        # only admins can reach the Django admin panel.
        is_admin = self.role == self.Role.ADMIN
        self.is_staff = is_admin
        self.is_superuser = is_admin
        super().save(*args, **kwargs)

    @property
    def is_print_staff(self):
        """Staff-level access to the print workflow (admins included)."""
        return self.role in (self.Role.STAFF, self.Role.ADMIN)

    @property
    def is_admin(self):
        return self.role == self.Role.ADMIN
