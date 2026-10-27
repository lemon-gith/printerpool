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


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": MEDIA}},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class RecallTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user("student", password="x")
        self.other = User.objects.create_user("other", password="x")
        self.staff = User.objects.create_user("tech", password="x", role=User.Role.STAFF)
        self.req = services.submit_request(self.student, files=[stl("orig.stl")], title="Old", material="pla")

    def recall(self, user=None):
        self.client.force_login(user or self.student)
        return self.client.post(reverse("request_action", args=[self.req.pk]), {"to_status": RS.RECALLED})

    def test_only_owner_can_recall(self):
        self.assertNotIn(RS.RECALLED, services.allowed_request_transitions(self.staff, self.req))
        self.assertNotIn(RS.RECALLED, services.allowed_request_transitions(self.other, self.req))
        self.assertIn(RS.RECALLED, services.allowed_request_transitions(self.student, self.req))

    def test_cannot_recall_once_printing(self):
        services.transition_request(self.req, RS.APPROVED, self.staff)
        job = services.create_print(self.req, self.staff, printer=None, est_duration_minutes=5, est_filament_g=5)
        services.transition_print(job, PS.PRINTING, self.staff)
        self.req.refresh_from_db()
        self.assertNotIn(RS.RECALLED, services.allowed_request_transitions(self.student, self.req))

    def test_recall_cancels_queued_prints_and_sets_expiry(self):
        services.transition_request(self.req, RS.APPROVED, self.staff)
        job = services.create_print(self.req, self.staff, printer=None, est_duration_minutes=5, est_filament_g=5)
        resp = self.recall()
        self.assertRedirects(resp, f"{reverse('request_create')}?from={self.req.pk}")
        job.refresh_from_db()
        self.req.refresh_from_db()
        self.assertEqual((self.req.status, job.status), (RS.RECALLED, PS.CANCELLED))
        self.assertAlmostEqual(self.req.files_expire_at, timezone.now() + timedelta(days=7), delta=timedelta(seconds=5))

    def test_edit_form_prefilled_and_resubmit_copies_kept_files(self):
        self.recall()
        page = self.client.get(f"{reverse('request_create')}?from={self.req.pk}")
        self.assertContains(page, 'value="Old"')
        old_file = self.req.files.get()
        resp = self.client.post(
            f"{reverse('request_create')}?from={self.req.pk}",
            {"title": "New", "material": "petg", "keep_files": [old_file.pk], "files": [stl("extra.stl")]},
        )
        new = PrintRequest.objects.get(title="New")
        self.assertRedirects(resp, reverse("request_detail", args=[new.pk]))
        self.assertEqual(new.supersedes, self.req)
        self.assertEqual((new.status, new.material), (RS.SUBMITTED, "petg"))
        names = sorted(new.files.values_list("original_name", flat=True))
        self.assertEqual(names, ["extra.stl", "orig.stl"])
        copy = new.files.get(original_name="orig.stl")
        self.assertNotEqual(copy.file.name, old_file.file.name)  # its own copy, expiring on its own schedule
        self.assertEqual(copy.sha256, old_file.sha256)

    def test_cannot_resubmit_twice(self):
        self.recall()
        url = f"{reverse('request_create')}?from={self.req.pk}"
        self.client.post(url, {"title": "A", "material": "pla", "keep_files": [self.req.files.get().pk]})
        resp = self.client.get(url)
        self.assertRedirects(resp, reverse("request_create"))
        self.assertEqual(PrintRequest.objects.filter(supersedes=self.req).count(), 1)

    def test_needs_at_least_one_file(self):
        self.recall()
        resp = self.client.post(f"{reverse('request_create')}?from={self.req.pk}", {"title": "A", "material": "pla"})
        self.assertContains(resp, "at least one file")

    def test_replaced_request_hidden_from_default_list(self):
        self.recall()
        self.client.post(f"{reverse('request_create')}?from={self.req.pk}", {"title": "New", "material": "pla", "keep_files": [self.req.files.get().pk]})
        titles = [r.title for r in self.client.get("/").context["page"].object_list]
        self.assertEqual(titles, ["New"])
        recalled = [r.title for r in self.client.get("/", {"status": "recalled"}).context["page"].object_list]
        self.assertEqual(recalled, ["Old"])

    def test_other_user_cannot_resubmit_or_steal_files(self):
        self.recall()
        self.client.force_login(self.other)
        resp = self.client.get(f"{reverse('request_create')}?from={self.req.pk}")
        self.assertRedirects(resp, reverse("request_create"))
        # A plain new request can't pick files from someone else's request either.
        self.client.post(reverse("request_create"), {"title": "X", "material": "pla", "keep_files": [self.req.files.get().pk]})
        self.assertFalse(PrintRequest.objects.filter(title="X").exists())


class DashboardTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("tech", password="x", role=User.Role.STAFF)
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")
        for i in range(30):
            PrintRequest.objects.create(user=self.alice, title=f"Alice part {i}")
        PrintRequest.objects.create(user=self.bob, title="Bob's gear")

    def test_paginates_and_keeps_filters(self):
        self.client.force_login(self.staff)
        resp = self.client.get("/", {"q": "alice"})
        self.assertEqual(resp.context["page"].paginator.count, 30)
        self.assertEqual(len(resp.context["page"].object_list), 25)
        self.assertContains(resp, "q=alice&amp;page=2")
        self.assertEqual(len(self.client.get("/", {"q": "alice", "page": 2}).context["page"].object_list), 5)

    def test_staff_search_by_username_and_title(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/", {"q": "bob"}).context["page"].paginator.count, 1)
        self.assertEqual(self.client.get("/", {"q": "gear"}).context["page"].paginator.count, 1)

    def test_users_only_see_their_own(self):
        self.client.force_login(self.bob)
        resp = self.client.get("/", {"q": "part"})
        self.assertEqual(resp.context["page"].paginator.count, 0)


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": MEDIA}},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class AdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("root", password="x")
        self.student = User.objects.create_user("student", password="x")
        self.client.force_login(self.admin)

    def test_status_is_read_only_in_admin(self):
        req = PrintRequest.objects.create(user=self.student, title="T")
        page = self.client.get(reverse("admin:prints_printrequest_change", args=[req.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="status"')
        self.assertContains(page, 'name="files_expire_at_0"')  # expiry is still adjustable

    def test_users_cannot_be_deleted_only_deactivated(self):
        page = self.client.get(reverse("admin:accounts_user_delete", args=[self.student.pk]))
        self.assertEqual(page.status_code, 403)
        self.client.post(
            reverse("admin:accounts_user_changelist"),
            {"action": "deactivate", "_selected_action": [self.student.pk, self.admin.pk]},
        )
        self.student.refresh_from_db()
        self.admin.refresh_from_db()
        self.assertFalse(self.student.is_active)
        self.assertTrue(self.admin.is_active)  # can't lock yourself out

    def test_deactivated_user_is_logged_out(self):
        other = self.client_class()
        other.force_login(self.student)
        User.objects.filter(pk=self.student.pk).update(is_active=False)
        self.assertRedirects(other.get("/"), f"{reverse('login')}?next=/")
