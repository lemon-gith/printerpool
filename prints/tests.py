import shutil
import struct
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from prints import services
from prints.models import Print, PrintRequest, RequestFile

RS, PS = PrintRequest.Status, Print.Status
MEDIA = tempfile.mkdtemp()


def binary_stl(triangles=2):
    return b"\0" * 80 + struct.pack("<I", triangles) + b"\0" * (50 * triangles)


def stl(name="part.stl", triangles=2):
    """A valid binary STL; vary `triangles` to get files with different contents."""
    return SimpleUploadedFile(name, binary_stl(triangles), content_type="model/stl")


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

    def test_request_actions_use_clear_vocabulary(self):
        req = self.submit()
        self.assertEqual(services.allowed_request_transitions(self.student, req), [RS.RECALLED])
        self.assertEqual(services.allowed_request_transitions(self.staff, req), [RS.APPROVED, RS.REJECTED])

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

    def start_print(self, finish=None):
        if self.req.status == RS.SUBMITTED:
            services.transition_request(self.req, RS.APPROVED, self.staff)
        job = services.create_print(self.req, self.staff, printer=None, est_duration_minutes=5, est_filament_g=5)
        services.transition_print(job, PS.PRINTING, self.staff)
        if finish:
            services.transition_print(job, finish, self.staff)
        self.req.refresh_from_db()
        return job

    def test_nothing_closes_while_a_print_is_running(self):
        self.start_print()
        self.assertEqual(services.allowed_request_transitions(self.student, self.req), [])
        self.assertEqual(services.allowed_request_transitions(self.staff, self.req), [])
        with self.assertRaisesMessage(PermissionDenied, "A print is running"):
            services.transition_request(self.req, RS.RECALLED, self.student)

    def test_can_recall_in_progress_request_once_nothing_is_printing(self):
        self.start_print(finish=PS.FAILED)
        self.assertEqual(self.req.status, RS.IN_PROGRESS)
        self.assertIn(RS.RECALLED, services.allowed_request_transitions(self.student, self.req))

    def test_complete_needs_queued_prints_dealt_with(self):
        self.start_print(finish=PS.SUCCEEDED)
        self.assertIn(RS.COMPLETED, services.allowed_request_transitions(self.staff, self.req))
        services.create_print(self.req, self.staff, printer=None, est_duration_minutes=5, est_filament_g=5)
        self.assertNotIn(RS.COMPLETED, services.allowed_request_transitions(self.staff, self.req))

    def test_recall_cancels_queued_prints_and_sets_expiry(self):
        services.transition_request(self.req, RS.APPROVED, self.staff)
        job = services.create_print(self.req, self.staff, printer=None, est_duration_minutes=5, est_filament_g=5)
        resp = self.recall()
        self.assertRedirects(resp, f"{reverse('request_create')}?from={self.req.pk}")
        job.refresh_from_db()
        self.req.refresh_from_db()
        self.assertEqual((self.req.status, job.status), (RS.RECALLED, PS.CANCELLED))
        self.assertAlmostEqual(self.req.files_expire_at, timezone.now() + timedelta(days=7), delta=timedelta(seconds=5))

    def resubmit(self, keep=None, **data):
        """Post the edit form the way the browser does: `keep` lists original files still ticked (default: all)."""
        if keep is None:
            keep = list(self.req.files.all())
        data.setdefault("keep_files", [rf.pk for rf in keep])
        return self.client.post(f"{reverse('request_create')}?from={self.req.pk}", {"title": "New", "material": "petg", **data})

    def three_file_request(self):
        self.req = services.submit_request(
            self.student, files=[stl("a.stl", 1), stl("b.stl", 2), stl("c.stl", 3)], title="Multi", material="pla"
        )
        self.recall()
        return {rf.original_name: rf for rf in self.req.files.all()}

    def test_resubmit_unchanged_takes_over_the_same_files(self):
        self.recall()
        page = self.client.get(f"{reverse('request_create')}?from={self.req.pk}")
        self.assertContains(page, 'value="Old"')
        self.assertContains(page, "orig.stl")
        original = self.req.files.get()

        resp = self.resubmit()

        new = PrintRequest.objects.get(title="New")
        self.assertRedirects(resp, reverse("request_detail", args=[new.pk]))
        self.assertEqual((new.supersedes, new.status, new.material), (self.req, RS.SUBMITTED, "petg"))
        moved = new.files.get()
        self.assertEqual((moved.pk, moved.file.name), (original.pk, original.file.name))  # same file, not a copy
        self.assertFalse(self.req.files.exists())
        self.assertAlmostEqual(new.files_expire_at, timezone.now() + timedelta(days=30), delta=timedelta(seconds=5))
        self.assertContains(self.client.get(reverse("request_detail", args=[self.req.pk])), "carried over to")

        # The recalled request's own expiry no longer touches the file.
        PrintRequest.objects.filter(pk=self.req.pk).update(files_expire_at=timezone.now() - timedelta(minutes=1))
        services.purge_expired_files()
        self.assertTrue(moved.file.storage.exists(moved.file.name))

    def test_remove_one_original_and_add_a_new_file(self):
        files = self.three_file_request()
        self.resubmit(keep=[files["a.stl"], files["c.stl"]], files=[stl("d.stl", 4)])

        new = PrintRequest.objects.get(title="New")
        by_name = {rf.original_name: rf for rf in new.files.all()}
        self.assertEqual(sorted(by_name), ["a.stl", "c.stl", "d.stl"])
        self.assertEqual(by_name["a.stl"].pk, files["a.stl"].pk)  # kept files keep their ids
        self.assertEqual(by_name["c.stl"].pk, files["c.stl"].pk)
        self.assertNotIn(by_name["d.stl"].pk, {rf.pk for rf in files.values()})  # new upload, new id
        self.assertEqual(list(self.req.files.all()), [files["b.stl"]])  # removed file stays with the recalled request

    def test_reuploading_an_original_inherits_it_instead_of_duplicating(self):
        files = self.three_file_request()
        stored_before = RequestFile.objects.count()
        # User removed b.stl, then uploaded the very same file again (different name, same bytes).
        self.resubmit(keep=[files["a.stl"]], files=[stl("b-again.stl", 2)])
        new = PrintRequest.objects.get(title="New")
        self.assertEqual(sorted(new.files.values_list("pk", flat=True)), sorted([files["a.stl"].pk, files["b.stl"].pk]))
        self.assertEqual(RequestFile.objects.count(), stored_before)  # nothing new stored

    def test_same_file_twice_is_stored_once(self):
        self.client.force_login(self.student)
        self.client.post(reverse("request_create"), {"title": "Dup", "material": "pla", "files": [stl("x.stl", 5), stl("y.stl", 5)]})
        self.assertEqual(PrintRequest.objects.get(title="Dup").files.count(), 1)

    def test_removing_every_file_is_refused(self):
        self.recall()
        resp = self.resubmit(keep=[])
        self.assertContains(resp, "needs at least one file")
        self.assertFalse(PrintRequest.objects.filter(title="New").exists())

    def test_cannot_keep_files_from_another_request(self):
        theirs = services.submit_request(self.other, files=[stl("theirs.stl", 9)], title="Theirs", material="pla")
        self.recall()
        resp = self.resubmit(keep_files=[theirs.files.get().pk])
        self.assertEqual(resp.status_code, 200)  # form error, not a submission
        self.assertFalse(PrintRequest.objects.filter(title="New").exists())
        self.assertEqual(theirs.files.count(), 1)

    def test_file_limit_counts_kept_and_new(self):
        files = self.three_file_request()
        with self.settings(MAX_FILES_PER_REQUEST=3):
            resp = self.resubmit(files=[stl("d.stl", 4)])
        self.assertContains(resp, "At most 3 files")
        self.assertEqual(len(files), self.req.files.count())

    def test_expired_originals_must_be_reuploaded(self):
        self.recall()
        PrintRequest.objects.filter(pk=self.req.pk).update(files_expire_at=timezone.now() - timedelta(minutes=1))
        page = self.client.get(f"{reverse('request_create')}?from={self.req.pk}")
        self.assertContains(page, "original files have expired")
        resp = self.resubmit(keep=[], keep_files=[self.req.files.get().pk])
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(PrintRequest.objects.filter(title="New").exists())

    def test_cannot_resubmit_twice(self):
        self.recall()
        self.resubmit()
        resp = self.client.get(f"{reverse('request_create')}?from={self.req.pk}")
        self.assertRedirects(resp, reverse("request_create"))
        self.assertEqual(PrintRequest.objects.filter(supersedes=self.req).count(), 1)

    def test_new_request_needs_a_file(self):
        self.client.force_login(self.student)
        resp = self.client.post(reverse("request_create"), {"title": "A", "material": "pla"})
        self.assertContains(resp, "needs at least one file")

    def test_replaced_request_hidden_from_default_list(self):
        self.recall()
        self.resubmit()
        titles = [r.title for r in self.client.get("/").context["page"].object_list]
        self.assertEqual(titles, ["New"])
        recalled = [r.title for r in self.client.get("/", {"status": "recalled"}).context["page"].object_list]
        self.assertEqual(recalled, ["Old"])

    def test_other_user_cannot_resubmit(self):
        self.recall()
        self.client.force_login(self.other)
        resp = self.client.get(f"{reverse('request_create')}?from={self.req.pk}")
        self.assertRedirects(resp, reverse("request_create"))
        self.assertEqual(self.req.files.count(), 1)


@override_settings(CONTACT_STAFF_EMAILS=["lab@uni.ac.uk", "tech@uni.ac.uk"], CONTACT_ADMIN_EMAILS=["boss@uni.ac.uk"])
class ContactLinkTests(TestCase):
    def test_multiple_addresses_in_one_mailto(self):
        self.client.force_login(User.objects.create_user("s", password="x"))
        page = self.client.get("/")
        self.assertContains(page, 'href="mailto:lab@uni.ac.uk,tech@uni.ac.uk?subject=')
        self.assertNotContains(page, "boss@uni.ac.uk")  # admin contact is for staff only

    def test_staff_see_admin_contact(self):
        self.client.force_login(User.objects.create_user("t", password="x", role=User.Role.STAFF))
        self.assertContains(self.client.get("/"), 'href="mailto:boss@uni.ac.uk?subject=')

    def test_invalid_address_fails_at_startup(self):
        from django.core.exceptions import ImproperlyConfigured

        from config.settings import env_emails

        with mock.patch.dict("os.environ", {"X_EMAILS": "ok@uni.ac.uk, not-an-email"}):
            with self.assertRaisesMessage(ImproperlyConfigured, "not-an-email"):
                env_emails("X_EMAILS")


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
