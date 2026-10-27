from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class EmailAddress(models.Model):
    """An optional address a user has added. It receives nothing until verified with a one-time code, and then
    only the notifications switched on for it (all off by default)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_addresses")
    email = models.EmailField()
    verified_at = models.DateTimeField(null=True, blank=True)
    notify_events = models.JSONField(default=list, blank=True, help_text="Event keys from notifications.events.")

    # One-time code for verification. Only a keyed hash is stored, never the code itself.
    otp_hash = models.CharField(max_length=128, blank=True)
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    otp_attempts = models.PositiveSmallIntegerField(default=0)
    otp_sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "email"], name="unique_email_per_user"),
            # An address can be verified by one account only.
            models.UniqueConstraint(
                fields=["email"], condition=Q(verified_at__isnull=False), name="unique_verified_email"
            ),
        ]

    def __str__(self):
        return self.email

    @property
    def is_verified(self):
        return self.verified_at is not None


class OtpSend(models.Model):
    """One row per verification email sent, for rate limiting (so the form can't be used to spam an inbox)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    email = models.EmailField()
    sent_at = models.DateTimeField(default=timezone.now, db_index=True)
