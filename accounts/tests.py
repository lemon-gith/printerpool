import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.ip import client_ip
from accounts.models import User


class ClientIpTests(SimpleTestCase):
    def req(self, xff=None):
        meta = {"REMOTE_ADDR": "172.18.0.5"}  # the proxy container
        if xff:
            meta["HTTP_X_FORWARDED_FOR"] = xff
        return RequestFactory().get("/", **meta)

    @override_settings(TRUSTED_PROXY_COUNT=0)
    def test_without_proxy_header_is_ignored(self):
        self.assertEqual(client_ip(self.req("6.6.6.6")), "172.18.0.5")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_behind_one_proxy_uses_address_it_recorded(self):
        self.assertEqual(client_ip(self.req("81.2.69.160")), "81.2.69.160")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_client_cannot_spoof_by_prepending(self):
        # Client sends "X-Forwarded-For: 6.6.6.6"; a proxy that appends would produce this.
        self.assertEqual(client_ip(self.req("6.6.6.6, 81.2.69.160")), "81.2.69.160")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_falls_back_without_header(self):
        self.assertEqual(client_ip(self.req()), "172.18.0.5")

    @override_settings(CLIENT_IP_HEADER="CF-Connecting-IP", TRUSTED_PROXY_COUNT=0)
    def test_cloudflare_header(self):
        request = RequestFactory().get(
            "/", REMOTE_ADDR="127.0.0.1", HTTP_CF_CONNECTING_IP="81.2.69.160", HTTP_X_FORWARDED_FOR="6.6.6.6"
        )
        self.assertEqual(client_ip(request), "81.2.69.160")

    @override_settings(CLIENT_IP_HEADER="CF-Connecting-IP", TRUSTED_PROXY_COUNT=1)
    def test_missing_cloudflare_header_never_falls_back_to_forwarded_for(self):
        request = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="6.6.6.6")
        self.assertEqual(client_ip(request), "127.0.0.1")


class CreateInitialAdminTests(TestCase):
    def run_command(self, username="boss", password="A-decent-temporary-pw-42"):
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"DJANGO_SUPERUSER_USERNAME": username, "DJANGO_SUPERUSER_PASSWORD": password}):
            call_command("create_initial_admin", stdout=out)
        return out.getvalue()

    def test_creates_admin_that_must_change_password(self):
        self.run_command()
        boss = User.objects.get(username="boss")
        self.assertEqual(boss.role, User.Role.ADMIN)
        self.assertTrue(boss.is_superuser and boss.must_change_password)
        self.assertTrue(boss.check_password("A-decent-temporary-pw-42"))

    def test_existing_account_is_left_alone(self):
        User.objects.create_user("boss", password="original-password-1")
        self.assertIn("already exists", self.run_command())
        boss = User.objects.get(username="boss")
        self.assertTrue(boss.check_password("original-password-1"))
        self.assertEqual(boss.role, User.Role.USER)

    def test_weak_password_refused(self):
        with self.assertRaisesMessage(CommandError, "too weak"):
            self.run_command(password="password")
        self.assertFalse(User.objects.filter(username="boss").exists())



class BulkImportTests(TestCase):
    url = "/admin/accounts/user/import/"

    def setUp(self):
        self.admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(self.admin)

    def upload(self, text, encoding="utf-8"):
        return self.client.post(self.url, {"csv_file": SimpleUploadedFile("users.csv", text.encode(encoding))})

    def test_creates_accounts_and_shows_passwords_once(self):
        resp = self.upload("username,first_name,last_name,role\nab1234,Ada,Byron,\ncd5678,Charles,Dodgson,student\ntech01,Grace,Hopper,staff\n")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("no-store", resp["Cache-Control"])
        users = {u.username: u for u in User.objects.exclude(pk=self.admin.pk)}
        self.assertEqual(sorted(users), ["ab1234", "cd5678", "tech01"])
        self.assertEqual(users["tech01"].role, User.Role.STAFF)
        self.assertEqual(users["cd5678"].role, User.Role.USER)
        self.assertTrue(all(u.must_change_password for u in users.values()))
        # The password on the sheet works, and only its hash is stored.
        for item in resp.context["issued"]:
            self.assertTrue(item.user.check_password(item.password))
            self.assertNotIn(item.password, User.objects.get(pk=item.user.pk).password)
            self.assertContains(resp, item.password, count=2)  # slip + CSV download
        self.assertContains(resp, "shown only once")

    def test_excel_bom_and_extra_spaces(self):
        self.upload("﻿Username , First_Name\n  zz99 , Zed \n\n", encoding="utf-8")
        self.assertEqual(User.objects.get(username="zz99").first_name, "Zed")

    def test_any_bad_row_creates_nothing(self):
        User.objects.create_user("taken", password="x")
        resp = self.upload(
            "username,role\nok1,\nTAKEN,\nbad name,\nok1,\n,staff\nok2,wizard\n"
        )
        self.assertEqual(resp.status_code, 200)
        errors = resp.context["errors"]
        self.assertTrue(any("Line 3" in e and "already exists" in e for e in errors))
        self.assertTrue(any("Line 4" in e and "valid username" in e for e in errors))
        self.assertTrue(any("Line 5" in e and "also on line 2" in e for e in errors))
        self.assertTrue(any("Line 6" in e and "empty" in e for e in errors))
        self.assertTrue(any("Line 7" in e and "wizard" in e for e in errors))
        self.assertFalse(User.objects.filter(username__in=["ok1", "ok2"]).exists())

    def test_header_problems(self):
        resp = self.upload("name,email\nx,y\n")
        self.assertIn("Missing column(s): username", resp.context["errors"][0])
        resp = self.upload("username,email\nx,y@z.com\n")
        self.assertIn("Unknown column(s): email", resp.context["errors"][0])
        resp = self.upload("username\n")
        self.assertIn("no accounts", resp.context["errors"][0])

    def test_non_utf8_file(self):
        resp = self.upload("username,first_name\nab1,Zoë\n", encoding="utf-16")
        self.assertIn("UTF-8", resp.context["errors"][0])

    def test_staff_cannot_import(self):
        self.client.force_login(User.objects.create_user("tech", password="x", role=User.Role.STAFF))
        resp = self.upload("username\nsneaky\n")
        self.assertNotEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="sneaky").exists())

    def test_single_add_and_reset_use_the_sheet(self):
        resp = self.client.post(reverse("admin:accounts_user_add"), {"username": "solo", "role": "user"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Account “solo” created")
        self.assertIn("no-store", resp["Cache-Control"])
        solo = User.objects.get(username="solo")
        self.assertTrue(solo.check_password(resp.context["issued"][0].password))

        resp = self.client.post(
            reverse("admin:accounts_user_changelist"), {"action": "reset_password", "_selected_action": [solo.pk]}
        )
        new_password = resp.context["issued"][0].password
        solo.refresh_from_db()
        self.assertTrue(solo.check_password(new_password))
        self.assertTrue(solo.must_change_password)

    def test_import_button_on_user_list(self):
        self.assertContains(self.client.get(reverse("admin:accounts_user_changelist")), "Import users (CSV)")
