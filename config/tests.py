import tempfile
from unittest import mock

from django.db import OperationalError
from django.test import TestCase, override_settings

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": tempfile.mkdtemp()}},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=STORAGES)
class HealthTests(TestCase):
    def test_healthy(self):
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "database": "ok", "storage": "ok"})
        self.assertIn("no-cache", resp["Cache-Control"])

    def test_no_login_needed(self):
        self.assertEqual(self.client.get("/health/").status_code, 200)

    def test_database_down(self):
        with mock.patch("config.views.connection.cursor", side_effect=OperationalError("down")):
            with self.assertLogs("config.views", "ERROR"):
                resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["database"], "failing")

    def test_storage_not_writable(self):
        with mock.patch("config.views.os.access", return_value=False):
            resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["storage"], "failing")
