"""Tests for render_config.py. Standard library only: python -m unittest deploy/test_render_config.py"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_config  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

BASE = """
[site]
domain = "print.uni.example"

[cloudflared]
mode = "token"
"""


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def render(self, toml, env=None):
        (self.root / "printpool.toml").write_text(toml, encoding="utf-8")
        if env is not None:
            (self.root / ".env").write_text(env, encoding="utf-8")
        return render_config.render(self.root)

    def env_values(self):
        return render_config.read_env(self.root / ".generated" / "app.env")

    def override(self):
        text = (self.root / "compose.override.yaml").read_text()
        return json.loads(text.split("\n", 1)[1])

    def gateway_conf(self):
        return (self.root / ".generated" / "gateway.conf").read_text()

    # --- secrets ---
    def test_first_run_creates_env_with_secrets_and_requires_token(self):
        with self.assertRaisesRegex(render_config.ConfigError, "CLOUDFLARE_TUNNEL_TOKEN"):
            self.render(BASE)
        env = render_config.read_env(self.root / ".env")
        self.assertGreaterEqual(len(env["DJANGO_SECRET_KEY"]), 50)
        self.assertGreaterEqual(len(env["POSTGRES_PASSWORD"]), 24)
        self.assertEqual(env["POSTGRES_USER"], "printpool")

    def test_existing_secrets_are_never_changed(self):
        env = "DJANGO_SECRET_KEY=keep-me\nPOSTGRES_PASSWORD=keep-this-too\nCLOUDFLARE_TUNNEL_TOKEN=t\n"
        self.render(BASE, env)
        self.render(BASE)
        values = render_config.read_env(self.root / ".env")
        self.assertEqual((values["DJANGO_SECRET_KEY"], values["POSTGRES_PASSWORD"]), ("keep-me", "keep-this-too"))
        self.assertEqual(values["POSTGRES_DB"], "printpool")  # missing fixed values are added

    def test_old_env_settings_are_flagged(self):
        _, notes = self.render(BASE, "CLOUDFLARE_TUNNEL_TOKEN=t\nAPP_PORT=8000\nCOMPOSE_PROFILES=proxy\n")
        self.assertTrue(any("APP_PORT" in n and "COMPOSE_PROFILES" in n for n in notes))

    # --- defaults: tunnel, no egress ---
    def test_default_deployment(self):
        self.render(BASE, "CLOUDFLARE_TUNNEL_TOKEN=t\n")
        env = self.env_values()
        self.assertEqual(env["DJANGO_ALLOWED_HOSTS"], "print.uni.example")
        self.assertEqual(env["DJANGO_SITE_URL"], "https://print.uni.example")
        self.assertEqual((env["DJANGO_SECURE"], env["DJANGO_CLIENT_IP_HEADER"]), ("1", "CF-Connecting-IP"))
        self.assertEqual((env["EMAIL_ENABLED"], env["BACKUP_ENABLED"]), ("0", "0"))
        override = self.override()
        services = override["services"]
        self.assertNotIn("ports", services["gateway"])  # nothing published on the host
        self.assertEqual(services["gateway"]["networks"]["app"]["aliases"], [])
        self.assertEqual(services["cloudflared"]["image"], "cloudflare/cloudflared:latest")
        self.assertEqual(services["cloudflared"]["environment"]["TUNNEL_TOKEN"], "${CLOUDFLARE_TUNNEL_TOKEN}")
        conf = self.gateway_conf()
        self.assertIn("No egress", conf)
        self.assertNotIn("stream {", conf)
        self.assertIn("X-Forwarded-Proto $http_x_forwarded_proto", conf)  # trust cloudflared's view
        self.assertTrue((self.root / "backups").is_dir())

    def test_local_testing_without_cloudflared(self):
        self.render("""
[site]
domain = "print.uni.example"
[cloudflared]
enabled = false
[ingress]
publish = "127.0.0.1:8000"
https = false
""", "")
        env = self.env_values()
        self.assertEqual(env["DJANGO_ALLOWED_HOSTS"], "print.uni.example,localhost,127.0.0.1")
        self.assertIn("http://localhost:8000", env["DJANGO_CSRF_TRUSTED_ORIGINS"])
        self.assertEqual((env["DJANGO_SECURE"], env["DJANGO_CLIENT_IP_HEADER"], env["DJANGO_TRUSTED_PROXY_COUNT"]),
                         ("0", "", "1"))
        override = self.override()
        self.assertEqual(override["services"]["gateway"]["ports"], ["127.0.0.1:8000:8080"])
        self.assertNotIn("cloudflared", override["services"])
        self.assertIn("X-Forwarded-Proto $scheme", self.gateway_conf())

    def test_cloudflared_outside_docker(self):
        self.render("""
[site]
domain = "print.uni.example"
[cloudflared]
enabled = false
[ingress]
publish = "127.0.0.1:8000"
https = true
""", "")
        env = self.env_values()
        self.assertEqual((env["DJANGO_SECURE"], env["DJANGO_CLIENT_IP_HEADER"]), ("1", "CF-Connecting-IP"))
        self.assertIn("X-Forwarded-Proto $http_x_forwarded_proto", self.gateway_conf())  # no redirect loop
        self.assertNotIn("cloudflared", self.override()["services"])

    # --- egress ---
    def test_email_opens_exactly_its_destination(self):
        self.render(BASE + """
[email]
enabled = true
from = "PrintPool <printpool@send.uni.example>"
""", "CLOUDFLARE_TUNNEL_TOKEN=t\nEMAIL_PASSWORD=re_x\n")
        env = self.env_values()
        self.assertEqual((env["EMAIL_ENABLED"], env["EMAIL_HOST"], env["EMAIL_PORT"]), ("1", "smtp.resend.com", "587"))
        self.assertEqual(self.override()["services"]["gateway"]["networks"]["app"]["aliases"], ["smtp.resend.com"])
        conf = self.gateway_conf()
        self.assertIn("listen 587;", conf)
        self.assertIn("default smtp.resend.com:587;", conf)
        self.assertNotIn("ssl_preread", conf)  # one destination on the port: plain pass-through

    def test_email_needs_its_password(self):
        with self.assertRaisesRegex(render_config.ConfigError, "EMAIL_PASSWORD"):
            self.render(BASE + "[email]\nenabled = true\nfrom = \"a@uni.example\"\n", "CLOUDFLARE_TUNNEL_TOKEN=t\n")

    def test_several_hosts_on_one_port_are_chosen_by_server_name(self):
        self.render(BASE + """
[[egress]]
host = "api.one.example"
port = 443
[[egress]]
host = "api.two.example"
port = 443
""", "CLOUDFLARE_TUNNEL_TOKEN=t\n")
        conf = self.gateway_conf()
        self.assertIn("ssl_preread on;", conf)
        self.assertIn("api.one.example api.one.example:443;", conf)
        self.assertIn("api.two.example api.two.example:443;", conf)
        self.assertIn("default unix:", conf)  # anything else is refused
        self.assertEqual(self.override()["services"]["gateway"]["networks"]["app"]["aliases"],
                         ["api.one.example", "api.two.example"])

    def test_egress_cannot_use_the_gateways_own_ports(self):
        with self.assertRaisesRegex(render_config.ConfigError, "used by the gateway"):
            self.render(BASE + "[[egress]]\nhost = \"x.example\"\nport = 8080\n", "CLOUDFLARE_TUNNEL_TOKEN=t\n")

    def test_dns_override_reaches_the_gateway(self):
        self.render(BASE + "[gateway]\ndns = [\"10.0.0.53\", \"10.0.0.54\"]\n", "CLOUDFLARE_TUNNEL_TOKEN=t\n")
        self.assertEqual(self.override()["services"]["gateway"]["environment"]["PRINTPOOL_DNS"], "10.0.0.53 10.0.0.54")

    # --- backups ---
    def test_backups_to_a_named_volume(self):
        self.render(BASE + """
[backups]
enabled = true
frequency = "weekly"
weekday = "friday"
keep = 0
encrypt = true
include_files = true
[backups.location]
type = "volume"
volume = "pp_backups"
""", "CLOUDFLARE_TUNNEL_TOKEN=t\nBACKUP_PASSWORD=s3cret\n")
        env = self.env_values()
        self.assertEqual((env["BACKUP_FREQUENCY"], env["BACKUP_WEEKDAY"], env["BACKUP_KEEP"], env["BACKUP_INCLUDE_FILES"]),
                         ("weekly", "friday", "0", "1"))
        override = self.override()
        self.assertEqual(override["services"]["worker"]["volumes"],
                         [{"type": "volume", "source": "pp_backups", "target": "/backups"}])
        self.assertEqual(override["volumes"], {"pp_backups": {"name": "pp_backups"}})

    def test_backup_folder_with_windows_path(self):
        self.render(BASE + "[backups.location]\ntype = \"folder\"\npath = 'D:\\PrintPool Backups'\n",
                    "CLOUDFLARE_TUNNEL_TOKEN=t\n")
        mount = self.override()["services"]["worker"]["volumes"][0]
        self.assertEqual((mount["type"], mount["source"]), ("bind", "D:\\PrintPool Backups"))

    def test_encrypted_backups_need_a_password(self):
        with self.assertRaisesRegex(render_config.ConfigError, "BACKUP_PASSWORD"):
            self.render(BASE + "[backups]\nenabled = true\n", "CLOUDFLARE_TUNNEL_TOKEN=t\n")

    # --- validation ---
    def test_helpful_errors(self):
        cases = {
            BASE + "[backups]\nfrequncy = \"daily\"\n": "unknown setting \\[backups.frequncy\\]",
            BASE.replace("print.uni.example", "print.example.com"): "real hostname",
            BASE + "[backups]\ntime = \"3:30pm\"\n": "HH:MM",
            BASE + "[backups]\nkeep = -1\n": "whole number",
            BASE + "[ingress]\npublish = \"8000\"\n": "address:port",
            BASE.replace('mode = "token"', 'enabled = false') + "[ingress]\npublish = \"\"\n": "nothing can reach",
            BASE + "[app]\ncontact_staff_emails = [\"nope\"]\n": "email addresses",
            "[site\n": "isn't valid TOML",
        }
        for toml, message in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(render_config.ConfigError, message):
                    self.render(toml, "CLOUDFLARE_TUNNEL_TOKEN=t\n")

    def test_config_mode_needs_its_files(self):
        toml = BASE.replace('mode = "token"', 'mode = "config"')
        with self.assertRaisesRegex(render_config.ConfigError, "config.yml"):
            self.render(toml, "")
        folder = self.root / "deploy" / "cloudflared"
        folder.mkdir(parents=True)
        (folder / "config.yml").write_text("ingress:\n  - service: http://localhost:8000\n")
        _, notes = self.render(toml)
        self.assertTrue(any("gateway:8080" in n for n in notes))
        self.assertTrue(any("credentials" in n for n in notes))
        self.assertEqual(self.override()["services"]["cloudflared"]["volumes"],
                         ["./deploy/cloudflared:/etc/cloudflared:ro"])

    def test_example_file_is_valid_once_domain_and_token_are_set(self):
        example = (REPO / "printpool.example.toml").read_text().replace("print.example.com", "print.uni.example")
        summary, _ = self.render(example, "CLOUDFLARE_TUNNEL_TOKEN=t\n")
        self.assertIn("Site:        https://print.uni.example", summary)


if __name__ == "__main__":
    unittest.main()
