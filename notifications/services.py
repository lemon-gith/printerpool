"""Email address verification and notification sending."""

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .events import ALL_EVENTS, STAFF_EVENTS, USER_EVENTS, events_for
from .models import EmailAddress, OtpSend

log = logging.getLogger(__name__)

MAX_ADDRESSES_PER_USER = 3
OTP_LIFETIME = timedelta(minutes=15)
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN = timedelta(seconds=60)
OTP_SENDS_PER_DAY = 10  # per user, across all their addresses


def site_url(path=""):
    return settings.SITE_URL.rstrip("/") + path


# --- Verification -------------------------------------------------------------------------------
def _otp_digest(address, code):
    # Keyed with SECRET_KEY and bound to this address, so a leaked database row doesn't reveal the code.
    return salted_hmac("notifications.otp", f"{address.pk}:{address.email}:{code}").hexdigest()


def add_address(user, email):
    email = email.strip().lower()
    if user.email_addresses.count() >= MAX_ADDRESSES_PER_USER:
        raise ValidationError(f"You can add up to {MAX_ADDRESSES_PER_USER} addresses.")
    if user.email_addresses.filter(email=email).exists():
        raise ValidationError("You've already added that address.")
    if EmailAddress.objects.filter(email=email, verified_at__isnull=False).exists():
        raise ValidationError("That address is already in use on another account.")
    address = EmailAddress.objects.create(user=user, email=email)
    send_code(address)
    return address


def send_code(address):
    now = timezone.now()
    if address.is_verified:
        raise ValidationError("This address is already verified.")
    if address.otp_sent_at and now - address.otp_sent_at < OTP_RESEND_COOLDOWN:
        wait = int((OTP_RESEND_COOLDOWN - (now - address.otp_sent_at)).total_seconds()) + 1
        raise ValidationError(f"Please wait {wait} seconds before requesting another code.")
    if OtpSend.objects.filter(user=address.user, sent_at__gte=now - timedelta(days=1)).count() >= OTP_SENDS_PER_DAY:
        raise ValidationError("Too many codes sent today. Please try again tomorrow.")

    code = f"{secrets.randbelow(10**6):06d}"
    address.otp_hash = _otp_digest(address, code)
    address.otp_expires_at = now + OTP_LIFETIME
    address.otp_attempts = 0
    address.otp_sent_at = now
    address.save(update_fields=["otp_hash", "otp_expires_at", "otp_attempts", "otp_sent_at"])
    OtpSend.objects.create(user=address.user, email=address.email, sent_at=now)

    minutes = int(OTP_LIFETIME.total_seconds() // 60)
    try:
        send_mail(
            subject=f"PrintPool verification code: {code}",
            message=(
                f"Your PrintPool verification code is {code}\n\n"
                f"Enter it on your account page to confirm this address. It expires in {minutes} minutes.\n\n"
                "Adding this address doesn't turn on any notifications. You choose those separately, "
                "and they're all off until you do.\n\n"
                "If you didn't ask for this, you can ignore this email."
            ),
            from_email=None,
            recipient_list=[address.email],
        )
    except Exception:
        log.exception("Failed to send verification code to address %s", address.pk)
        raise ValidationError("We couldn't send the code right now. Please try again later.")


def verify_code(address, code):
    if address.is_verified:
        return
    if not address.otp_hash or not address.otp_expires_at or address.otp_expires_at < timezone.now():
        raise ValidationError("That code has expired. Request a new one.")
    if address.otp_attempts >= OTP_MAX_ATTEMPTS:
        raise ValidationError("Too many wrong attempts. Request a new code.")
    if not constant_time_compare(_otp_digest(address, code.strip()), address.otp_hash):
        address.otp_attempts += 1
        address.save(update_fields=["otp_attempts"])
        left = OTP_MAX_ATTEMPTS - address.otp_attempts
        raise ValidationError(f"That code isn't right. {left} attempt(s) left." if left else "Too many wrong attempts. Request a new code.")

    address.verified_at = timezone.now()
    address.otp_hash = ""
    address.otp_expires_at = None
    try:
        with transaction.atomic():
            address.save(update_fields=["verified_at", "otp_hash", "otp_expires_at"])
    except IntegrityError:
        raise ValidationError("That address has just been verified on another account.")


def set_preferences(address, event_keys):
    if not address.is_verified:
        raise ValidationError("Verify this address before choosing notifications.")
    allowed = events_for(address.user)
    address.notify_events = [key for key in allowed if key in set(event_keys)]
    address.save(update_fields=["notify_events"])


# --- Notifications ----------------------------------------------------------------------------
def _recipients(users, event, exclude_user=None):
    return list(
        EmailAddress.objects.filter(
            user__in=users,
            user__is_active=True,
            verified_at__isnull=False,
            notify_events__contains=[event],
        )
        .exclude(user=exclude_user)
        .select_related("user")
    )


def _send(addresses, event, subject, body):
    label = ALL_EVENTS[event]
    for address in addresses:
        footer = (
            f"\n\n--\nYou're receiving this because \"{label}\" is turned on for {address.email}.\n"
            f"Change your email notifications: {site_url(reverse('account'))}"
        )
        try:
            send_mail(subject=f"PrintPool: {subject}", message=body + footer, from_email=None, recipient_list=[address.email])
        except Exception:
            # A mail outage must never break the print workflow; the change itself has already been saved.
            log.exception("Failed to send %s notification to address %s", event, address.pk)


REQUEST_EVENT_TEXT = {
    "request_approved": ("approved", "Your request “{title}” has been approved and will be queued for printing."),
    "request_rejected": ("rejected", "Your request “{title}” has been rejected."),
    "request_completed": ("completed", "Your request “{title}” is complete. Your print(s) are ready."),
    "print_started": ("print started", "A print of your request “{title}” has started{eta}."),
    "print_succeeded": ("print finished", "A print of your request “{title}” has finished successfully."),
    "print_failed": ("print failed", "A print of your request “{title}” failed. Staff will decide whether to reprint it."),
}


def notify(event, req, actor=None, print_job=None, note=""):
    """Queue `event` about print request `req` to be emailed once the current transaction commits."""
    if event not in ALL_EVENTS:
        raise ValueError(f"Unknown notification event {event!r}")
    if not settings.EMAIL_ENABLED:
        return
    transaction.on_commit(lambda: _deliver(event, req, actor, print_job, note), robust=True)


def _deliver(event, req, actor, print_job, note):
    link = site_url(reverse("request_detail", args=[req.pk]))
    if event in USER_EVENTS:
        short, text = REQUEST_EVENT_TEXT[event]
        eta = ""
        if print_job is not None and print_job.est_finish_at:
            eta = f", estimated to finish around {timezone.localtime(print_job.est_finish_at):%H:%M on %d %b}"
        body = text.format(title=req.title, eta=eta)
        if note:
            body += f"\n\nNote from staff: {note}"
        body += f"\n\nView the request: {link}"
        _send(_recipients([req.user], event, exclude_user=actor), event, f"{req.title} ({short})", body)
    elif event in STAFF_EVENTS:
        User = get_user_model()
        staff = User.objects.filter(role__in=[User.Role.STAFF, User.Role.ADMIN])
        body = f"{req.user.username} submitted a new request: “{req.title}”.\n\nReview it: {link}"
        _send(_recipients(staff, event, exclude_user=req.user), event, f"new request: {req.title}", body)
