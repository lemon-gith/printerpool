import re
import shutil
import struct
import tempfile
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from notifications import services
from notifications.models import EmailAddress
from prints import services as print_services
from prints.models import Print, PrintRequest

MEDIA = tempfile.mkdtemp()
RS, PS = PrintRequest.Status, Print.Status


def code_from_last_email():
    return re.search(r"\b(\d{6})\b", mail.outbox[-1].subject).group(1)


def stl():
    return SimpleUploadedFile("p.stl", b"\0" * 80 + struct.pack("<I", 1) + b"\0" * 50)


class VerificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="x")
        self.client.force_login(self.user)

    def add(self, email="Alice@Example.com"):
        return self.client.post(reverse("email_add"), {"email": email})

    def test_add_sends_code_and_nothing_is_enabled(self):
        self.add()
        address = self.user.email_addresses.get()
        self.assertEqual(address.email, "alice@example.com")
        self.assertFalse(address.is_verified)
        self.assertEqual(address.notify_events, [])
        self.assertEqual(mail.outbox[-1].to, ["alice@example.com"])
        code = code_from_last_email()
        self.assertNotIn(code, address.otp_hash)  # only a keyed hash is stored

    def test_verify_with_code(self):
        self.add()
        address = self.user.email_addresses.get()
        self.client.post(reverse("email_verify", args=[address.pk]), {"code": code_from_last_email()})
        address.refresh_from_db()
        self.assertTrue(address.is_verified)
        self.assertEqual(address.notify_events, [])  # verifying doesn't opt in to anything

    def test_wrong_code_attempts_are_limited(self):
        self.add()
        address = self.user.email_addresses.get()
        real = code_from_last_email()
        wrong = "000000" if real != "000000" else "111111"
        for _ in range(services.OTP_MAX_ATTEMPTS):
            with self.assertRaises(ValidationError):
                services.verify_code(address, wrong)
        with self.assertRaisesMessage(ValidationError, "Too many wrong attempts"):
            services.verify_code(address, real)  # even the right code is refused now
        self.assertFalse(address.is_verified)

    def test_code_expires(self):
        self.add()
        address = self.user.email_addresses.get()
        EmailAddress.objects.filter(pk=address.pk).update(otp_expires_at=timezone.now() - timedelta(seconds=1))
        address.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "expired"):
            services.verify_code(address, code_from_last_email())

    def test_resend_cooldown_and_daily_limit(self):
        self.add()
        address = self.user.email_addresses.get()
        with self.assertRaisesMessage(ValidationError, "Please wait"):
            services.send_code(address)
        for _ in range(services.OTP_SENDS_PER_DAY - 1):
            EmailAddress.objects.filter(pk=address.pk).update(otp_sent_at=timezone.now() - timedelta(minutes=5))
            address.refresh_from_db()
            services.send_code(address)
        EmailAddress.objects.filter(pk=address.pk).update(otp_sent_at=timezone.now() - timedelta(minutes=5))
        address.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "Too many codes"):
            services.send_code(address)

    def test_new_code_invalidates_old_one(self):
        self.add()
        address = self.user.email_addresses.get()
        old = code_from_last_email()
        EmailAddress.objects.filter(pk=address.pk).update(otp_sent_at=timezone.now() - timedelta(minutes=5))
        address.refresh_from_db()
        services.send_code(address)
        new = code_from_last_email()
        if old != new:
            with self.assertRaises(ValidationError):
                services.verify_code(address, old)
        services.verify_code(address, new)
        self.assertTrue(address.is_verified)

    def test_address_verified_elsewhere_cannot_be_added(self):
        bob = User.objects.create_user("bob", password="x")
        EmailAddress.objects.create(user=bob, email="shared@example.com", verified_at=timezone.now())
        self.add("shared@example.com")
        self.assertFalse(self.user.email_addresses.exists())

    def test_address_limit(self):
        for i in range(services.MAX_ADDRESSES_PER_USER):
            self.add(f"a{i}@example.com")
        self.add("one-too-many@example.com")
        self.assertEqual(self.user.email_addresses.count(), services.MAX_ADDRESSES_PER_USER)

    def test_cannot_touch_someone_elses_address(self):
        bob = User.objects.create_user("bob", password="x")
        theirs = EmailAddress.objects.create(user=bob, email="bob@example.com")
        self.assertEqual(self.client.post(reverse("email_remove", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("email_resend", args=[theirs.pk])).status_code, 404)

    def test_preferences_need_verification_and_are_filtered_by_role(self):
        address = EmailAddress.objects.create(user=self.user, email="a@example.com")
        with self.assertRaises(ValidationError):
            services.set_preferences(address, ["request_approved"])
        address.verified_at = timezone.now()
        address.save()
        services.set_preferences(address, ["request_approved", "staff_new_request", "bogus"])
        self.assertEqual(address.notify_events, ["request_approved"])  # staff-only and unknown keys dropped

    def test_account_page_explains_opt_in(self):
        resp = self.client.get(reverse("account"))
        self.assertContains(resp, "opt-in")
        self.assertContains(resp, "Everything is off by default")

    def test_mail_failure_on_add_reports_and_allows_resend(self):
        with mock.patch("notifications.services.send_mail", side_effect=OSError("smtp down")):
            resp = self.add()
        self.assertRedirects(resp, reverse("account"))
        self.assertTrue(self.user.email_addresses.exists())


@override_settings(
    SITE_URL="https://print.example.ac.uk",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": MEDIA}},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class NotificationTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.student = User.objects.create_user("student", password="x")
        self.staff = User.objects.create_user("tech", password="x", role=User.Role.STAFF)

    def address(self, user, email, events, verified=True):
        return EmailAddress.objects.create(
            user=user, email=email, notify_events=events, verified_at=timezone.now() if verified else None
        )

    def submit(self):
        with self.captureOnCommitCallbacks(execute=True):
            return print_services.submit_request(self.student, files=[stl()], title="Bracket", material="pla")

    def approve(self, req, note=""):
        with self.captureOnCommitCallbacks(execute=True):
            return print_services.transition_request(req, RS.APPROVED, self.staff, note)

    def test_only_verified_opted_in_addresses_get_mail(self):
        self.address(self.student, "on@example.com", ["request_approved"])
        self.address(self.student, "off@example.com", [])
        self.address(self.student, "other-event@example.com", ["print_failed"])
        self.address(self.student, "unverified@example.com", ["request_approved"], verified=False)
        req = self.submit()
        mail.outbox.clear()

        self.approve(req, note="Looks great")

        self.assertEqual([m.to for m in mail.outbox], [["on@example.com"]])
        msg = mail.outbox[0]
        self.assertIn("Bracket", msg.subject)
        self.assertIn("Looks great", msg.body)
        self.assertIn(f"https://print.example.ac.uk/requests/{req.pk}/", msg.body)
        self.assertIn("https://print.example.ac.uk/account/", msg.body)  # how to turn it off

    def test_nothing_sent_by_default(self):
        self.address(self.student, "s@example.com", [])
        self.address(self.staff, "t@example.com", [])
        req = self.submit()
        self.approve(req)
        self.assertEqual(mail.outbox, [])

    def test_staff_new_request_notification(self):
        self.address(self.staff, "tech@example.com", ["staff_new_request"])
        inactive = User.objects.create_user("gone", password="x", role=User.Role.STAFF, is_active=False)
        self.address(inactive, "gone@example.com", ["staff_new_request"])
        self.submit()
        self.assertEqual([m.to for m in mail.outbox], [["tech@example.com"]])
        self.assertIn("new request", mail.outbox[0].subject)

    def test_print_started_includes_eta(self):
        self.address(self.student, "s@example.com", ["print_started"])
        req = self.approve(self.submit())
        job = print_services.create_print(req, self.staff, printer=None, est_duration_minutes=90, est_filament_g=10)
        with self.captureOnCommitCallbacks(execute=True):
            print_services.transition_print(job, PS.PRINTING, self.staff)
        self.assertIn("estimated to finish", mail.outbox[-1].body)

    def test_actor_is_not_emailed_about_their_own_action(self):
        self.address(self.staff, "tech@example.com", ["request_approved", "staff_new_request"])
        with self.captureOnCommitCallbacks(execute=True):
            req = print_services.submit_request(self.staff, files=[stl()], title="Mine", material="pla")
        self.approve(req)
        self.assertEqual(mail.outbox, [])

    def test_mail_outage_does_not_break_the_workflow(self):
        self.address(self.student, "s@example.com", ["request_approved"])
        req = self.submit()
        with mock.patch("notifications.services.send_mail", side_effect=OSError("smtp down")):
            with self.assertLogs("notifications.services", "ERROR"):
                self.approve(req)
        req.refresh_from_db()
        self.assertEqual(req.status, RS.APPROVED)
