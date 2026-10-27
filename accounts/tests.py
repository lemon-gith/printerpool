from django.test import RequestFactory, SimpleTestCase, override_settings

from accounts.ip import client_ip


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
