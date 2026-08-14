import unittest
from unittest import mock

from flask import Flask

import rate_limiter


class GetClientIpTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_returns_remote_addr_when_xff_disabled(self):
        with mock.patch.object(rate_limiter, "USE_X_FORWARDED_FOR", False):
            with self.app.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "1.2.3.4"},
                headers={"X-Forwarded-For": "9.9.9.9"},
            ):
                self.assertEqual(rate_limiter.get_client_ip(), "1.2.3.4")

    def test_uses_xff_when_enabled_without_trusted_proxies(self):
        # Rightmost entry wins: the leftmost is client-supplied and spoofable.
        with mock.patch.object(rate_limiter, "USE_X_FORWARDED_FOR", True), \
                mock.patch.object(rate_limiter, "TRUSTED_PROXIES", set()):
            with self.app.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "1.2.3.4"},
                headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8"},
            ):
                self.assertEqual(rate_limiter.get_client_ip(), "8.8.8.8")

    def test_uses_xff_only_from_trusted_proxy(self):
        with mock.patch.object(rate_limiter, "USE_X_FORWARDED_FOR", True), \
                mock.patch.object(rate_limiter, "TRUSTED_PROXIES", {"1.2.3.4"}):
            with self.app.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "1.2.3.4"},
                headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8"},
            ):
                self.assertEqual(rate_limiter.get_client_ip(), "8.8.8.8")

    def test_ignores_xff_from_untrusted_proxy(self):
        with mock.patch.object(rate_limiter, "USE_X_FORWARDED_FOR", True), \
                mock.patch.object(rate_limiter, "TRUSTED_PROXIES", {"1.2.3.4"}):
            with self.app.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "6.6.6.6"},
                headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8"},
            ):
                self.assertEqual(rate_limiter.get_client_ip(), "6.6.6.6")

    def test_remote_addr_fallback(self):
        with self.app.test_request_context("/", environ_base={"REMOTE_ADDR": "1.2.3.4"}):
            self.assertEqual(rate_limiter.get_client_ip(), "1.2.3.4")


class CreateLimiterTest(unittest.TestCase):
    def test_local_default_is_no_rate_limiting(self):
        self.assertFalse(rate_limiter.RATE_LIMIT_ENABLED)

    def test_disabled_limiter(self):
        app = Flask(__name__)
        with mock.patch.object(rate_limiter, "RATE_LIMIT_ENABLED", False):
            limiter = rate_limiter.create_limiter(app)
        self.assertFalse(limiter.enabled)
        self.assertEqual(limiter._storage_uri, "memory://")

    def test_enabled_limiter(self):
        app = Flask(__name__)
        with mock.patch.object(rate_limiter, "RATE_LIMIT_ENABLED", True):
            limiter = rate_limiter.create_limiter(app)
        self.assertTrue(limiter.enabled)

    def test_redis_storage_no_double_prefix(self):
        app = Flask(__name__)
        with mock.patch.dict("os.environ", {
            "RATE_LIMIT_STORAGE": "redis",
            "REDIS_URL": "redis://localhost:6379",
        }):
            limiter = rate_limiter.create_limiter(app)
        self.assertEqual(limiter._storage_uri, "redis://localhost:6379")


class DownloadRateLimitTest(unittest.TestCase):
    """Exercise the custom per-IP download history in app.py."""

    def setUp(self):
        import app

        self.app = app
        self._saved = {
            "enabled": app.RATE_LIMIT_ENABLED,
            "hourly": app.MAX_DOWNLOADS_PER_HOUR,
            "daily": app.MAX_DOWNLOADS_PER_DAY,
        }
        app.download_history.clear()

    def tearDown(self):
        self.app.RATE_LIMIT_ENABLED = self._saved["enabled"]
        self.app.MAX_DOWNLOADS_PER_HOUR = self._saved["hourly"]
        self.app.MAX_DOWNLOADS_PER_DAY = self._saved["daily"]
        self.app.download_history.clear()

    def test_disabled_means_no_limit(self):
        self.app.RATE_LIMIT_ENABLED = False
        ok, msg = self.app.check_rate_limit("1.2.3.4")
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        self.app.record_download("1.2.3.4")
        self.assertEqual(self.app.download_history.get("1.2.3.4", []), [])

    def test_hourly_limit_reached(self):
        self.app.RATE_LIMIT_ENABLED = True
        self.app.MAX_DOWNLOADS_PER_HOUR = 2
        self.app.MAX_DOWNLOADS_PER_DAY = 100
        self.app.record_download("1.2.3.4")
        self.app.record_download("1.2.3.4")
        ok, msg = self.app.check_rate_limit("1.2.3.4")
        self.assertFalse(ok)
        self.assertIn("Hourly download limit reached", msg)

    def test_daily_limit_reached(self):
        self.app.RATE_LIMIT_ENABLED = True
        self.app.MAX_DOWNLOADS_PER_HOUR = 100
        self.app.MAX_DOWNLOADS_PER_DAY = 2
        self.app.record_download("1.2.3.4")
        self.app.record_download("1.2.3.4")
        ok, msg = self.app.check_rate_limit("1.2.3.4")
        self.assertFalse(ok)
        self.assertIn("Daily download limit reached", msg)

    def test_under_limit_allowed(self):
        self.app.RATE_LIMIT_ENABLED = True
        self.app.MAX_DOWNLOADS_PER_HOUR = 10
        self.app.MAX_DOWNLOADS_PER_DAY = 50
        self.app.record_download("1.2.3.4")
        ok, msg = self.app.check_rate_limit("1.2.3.4")
        self.assertTrue(ok)
        self.assertEqual(msg, "")


if __name__ == "__main__":
    unittest.main()
