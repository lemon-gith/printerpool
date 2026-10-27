import shutil
import struct
import tempfile
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from prints import services
from prints.models import Print, PrintRequest

RS, PS = PrintRequest.Status, Print.Status
MEDIA = tempfile.mkdtemp()


def binary_stl(triangles=2):
    return b"\0" * 80 + struct.pack("<I", triangles) + b"\0" * (50 * triangles)


def stl(name="part.stl"):
    return SimpleUploadedFile(name, binary_stl(), content_type="model/stl")


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": MEDIA}},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class WorkflowTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.student = User.objects.create_user("student", password="x")
        self.other = User.objects.create_user("other", password="x")
        self.staff = User.objects.create_user("tech", password="x", role=User.Role.STAFF)

    def submit(self, user=None):
        return services.submit_request(user or self.student, files=[stl()], title="Bracket", material="pla")

    # --- expiry ---
    def test_new_request_expires_in_30_days(self):
        req = self.submit()
        self.assertAlmostEqual(req.files_expire_at, timezone.now() + timedelta(days=30), delta=timedelta(seconds=5))

    def test_reject_sets_7_day_expiry(self):
        req = services.transition_request(self.submit(), RS.REJECTED, self.staff, "Too big")
        self.assertAlmostEqual(req.files_expire_at, timezone.now() + timedelta(days=7), delta=timedelta(seconds=5))

    def test_approval_does_not_change_expiry(self):
        req = self.submit()
        before = req.files_expire_at
        req = services.transition_request(req, RS.APPROVED, self.staff)
        self.assertEqual(req.files_expire_at, before)

    def test_starting_print_sets_7_day_expiry_and_moves_request(self):
        req = services.transition_request(self.submit(), RS.APPROVED, self.staff)
        job = services.create_print(req, self.staff, printer=None, est_duration_minutes=90, est_filament_g=40)
        services.transition_print(job, PS.PRINTING, self.staff)
        req.refresh_from_db()
        self.assertEqual(req.status, RS.IN_PROGRESS)
        self.assertAlmostEqual(req.files_expire_at, timezone.now() + timedelta(days=7), delta=timedelta(seconds=5))
        self.assertEqual(
            list(req.events.values_list("to_status", flat=True)),
            ["submitted", "approved", "queued", "printing", "in_progress"],
        )

    # --- permissions ---
    def test_user_cannot_approve(self):
        with self.assertRaises(PermissionDenied):
            services.transition_request(self.submit(), RS.APPROVED, self.student)

    def test_owner_can_cancel_submitted(self):
        req = services.transition_request(self.submit(), RS.CANCELLED, self.student)
        self.assertEqual(req.status, RS.CANCELLED)

    def test_other_user_cannot_see_request_or_file(self):
        req = self.submit()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("request_detail", args=[req.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("file_download", args=[req.files.get().pk])).status_code, 404)

    def test_staff_can_download(self):
        req = self.submit()
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("file_download", args=[req.files.get().pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), binary_stl())

    def test_user_cannot_post_print_actions(self):
        req = services.transition_request(self.submit(), RS.APPROVED, self.staff)
        self.client.force_login(self.student)
        resp = self.client.post(reverse("print_create", args=[req.pk]), {"est_duration_minutes": 1, "est_filament_g": 1})
        self.assertEqual(resp.status_code, 403)

    # --- purge ---
    def test_purge_removes_expired_files_only(self):
        expired, fresh = self.submit(), self.submit()
        PrintRequest.objects.filter(pk=expired.pk).update(files_expire_at=timezone.now() - timedelta(minutes=1))
        path = expired.files.get().file.path

        purged = services.purge_expired_files()

        self.assertEqual([r.pk for r in purged], [expired.pk])
        expired.refresh_from_db()
        self.assertIsNotNone(expired.files_purged_at)
        self.assertEqual(expired.files.count(), 1)  # metadata kept
        with self.assertRaises(FileNotFoundError):
            open(path)
        self.assertTrue(fresh.files.get().file.storage.exists(fresh.files.get().file.name))
        self.assertEqual(services.purge_expired_files(), [])  # idempotent

    def test_download_blocked_once_expired_even_before_purge(self):
        req = self.submit()
        PrintRequest.objects.filter(pk=req.pk).update(files_expire_at=timezone.now() - timedelta(seconds=1))
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("file_download", args=[req.files.get().pk])).status_code, 404)

    # --- upload validation ---
    def test_upload_rejects_non_stl(self):
        self.client.force_login(self.student)
        bad = SimpleUploadedFile("evil.stl", b"MZ not a model at all", content_type="model/stl")
        resp = self.client.post(reverse("request_create"), {"title": "x", "material": "pla", "files": [bad]})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "not a valid STL")
        self.assertFalse(PrintRequest.objects.exists())

    def test_upload_accepts_multiple_files(self):
        self.client.force_login(self.student)
        ascii_stl = SimpleUploadedFile("b.stl", b"solid b\nendsolid b\n")
        resp = self.client.post(reverse("request_create"), {"title": "x", "material": "pla", "files": [stl("a.stl"), ascii_stl]})
        req = PrintRequest.objects.get()
        self.assertRedirects(resp, reverse("request_detail", args=[req.pk]))
        self.assertEqual(sorted(req.files.values_list("original_name", flat=True)), ["a.stl", "b.stl"])

    # --- cap ---
    def test_cap_counts_succeeded_and_failed_prints(self):
        req = services.transition_request(self.submit(), RS.APPROVED, self.staff)
        for outcome, used in ((PS.FAILED, 15), (PS.SUCCEEDED, None)):
            job = services.create_print(req, self.staff, printer=None, est_duration_minutes=60, est_filament_g=40)
            services.transition_print(job, PS.PRINTING, self.staff)
            services.transition_print(job, outcome, self.staff, filament_used_g=used)
        self.assertEqual(services.cap_status(self.student)["used_g"], 55)


class AccountTests(TestCase):
    def test_role_controls_django_admin_flags(self):
        u = User.objects.create_user("a", password="x", role=User.Role.ADMIN)
        self.assertTrue(u.is_staff and u.is_superuser)
        u.role = User.Role.STAFF
        u.save()
        self.assertFalse(u.is_staff or u.is_superuser)
        self.assertTrue(u.is_print_staff)

    def test_createsuperuser_is_admin(self):
        self.assertEqual(User.objects.create_superuser("root", password="x").role, User.Role.ADMIN)

    def test_forced_password_change(self):
        u = User.objects.create_user("new", password="Temp-pass-123", must_change_password=True)
        self.client.force_login(u)
        self.assertRedirects(self.client.get("/"), reverse("password_change"))
        self.client.post(
            reverse("password_change"),
            {"old_password": "Temp-pass-123", "new_password1": "a-much-better-pw-77", "new_password2": "a-much-better-pw-77"},
        )
        u.refresh_from_db()
        self.assertFalse(u.must_change_password)
        self.assertEqual(self.client.get("/").status_code, 200)
