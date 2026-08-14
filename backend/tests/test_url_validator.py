import socket
import unittest
from unittest import mock

import url_validator


class ValidateYoutubeUrlTest(unittest.TestCase):
    def assert_valid(self, url):
        ok, msg = url_validator.validate_youtube_url(url)
        self.assertTrue(ok, f"expected valid URL, got error: {msg}")
        self.assertEqual(msg, "")

    def assert_invalid(self, url, expected_error):
        ok, msg = url_validator.validate_youtube_url(url)
        self.assertFalse(ok, f"expected invalid URL: {url}")
        self.assertEqual(msg, expected_error)

    def test_valid_youtube_urls(self):
        for url in [
            "https://www.youtube.com/watch?v=abc123",
            "http://www.youtube.com/watch?v=abc123",
            "https://youtube.com/watch?v=abc123",
            "https://youtu.be/abc123",
            "https://m.youtube.com/watch?v=abc123",
            "https://music.youtube.com/watch?v=abc123",
        ]:
            with self.subTest(url=url):
                self.assert_valid(url)

    def test_rejects_non_youtube_domains(self):
        for url in [
            "https://evil.com/watch?v=abc123",
            "https://youtube.com.evil.com/watch?v=abc123",
            "https://youtube.com@evil.com/watch?v=abc123",
            "https://www.google.com/watch?v=abc123",
        ]:
            with self.subTest(url=url):
                self.assert_invalid(url, "Only YouTube URLs are allowed")

    def test_rejects_non_http_schemes(self):
        for url in [
            "ftp://www.youtube.com/watch?v=abc123",
            "javascript://www.youtube.com/watch?v=abc123",
            "file://www.youtube.com/watch?v=abc123",
        ]:
            with self.subTest(url=url):
                self.assert_invalid(url, "Only HTTP and HTTPS URLs are allowed")

    def test_rejects_private_ipv4_resolution(self):
        def fake_getaddrinfo(host, port, family, *args, **kwargs):
            if family == socket.AF_INET:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
            raise socket.gaierror("no ipv6")

        with mock.patch.object(url_validator.socket, "getaddrinfo", side_effect=fake_getaddrinfo):
            self.assert_invalid("https://www.youtube.com/watch?v=abc123", "Invalid YouTube URL")

    def test_rejects_when_any_resolved_ip_is_private(self):
        def fake_getaddrinfo(host, port, family, *args, **kwargs):
            if family == socket.AF_INET:
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0)),
                ]
            raise socket.gaierror("no ipv6")

        with mock.patch.object(url_validator.socket, "getaddrinfo", side_effect=fake_getaddrinfo):
            self.assert_invalid("https://www.youtube.com/watch?v=abc123", "Invalid YouTube URL")

    def test_rejects_private_ipv6_resolution(self):
        def fake_getaddrinfo(host, port, family, *args, **kwargs):
            if family == socket.AF_INET:
                raise socket.gaierror("no ipv4")
            if family == socket.AF_INET6:
                return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))]
            raise socket.gaierror("no match")

        with mock.patch.object(url_validator.socket, "getaddrinfo", side_effect=fake_getaddrinfo):
            self.assert_invalid("https://www.youtube.com/watch?v=abc123", "Invalid YouTube URL")

    def test_accepts_public_ip_resolution(self):
        def fake_getaddrinfo(host, port, family, *args, **kwargs):
            if family == socket.AF_INET:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
            raise socket.gaierror("no ipv6")

        with mock.patch.object(url_validator.socket, "getaddrinfo", side_effect=fake_getaddrinfo):
            self.assert_valid("https://www.youtube.com/watch?v=abc123")


class IsPublicIpTest(unittest.TestCase):
    def test_public_addresses(self):
        for ip in ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"]:
            with self.subTest(ip=ip):
                self.assertTrue(url_validator.is_public_ip(ip))

    def test_non_public_addresses(self):
        for ip in [
            "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1",
            "169.254.1.1", "0.0.0.0", "::1", "fc00::1", "fe80::1",
            "192.0.2.1", "198.51.100.1", "203.0.113.1", "100.64.0.1",
            "224.0.0.1", "240.0.0.1",
        ]:
            with self.subTest(ip=ip):
                self.assertFalse(url_validator.is_public_ip(ip))

    def test_invalid_input(self):
        self.assertFalse(url_validator.is_public_ip("not-an-ip"))


class SsrfGuardTest(unittest.TestCase):
    def setUp(self):
        self._orig_getaddrinfo = url_validator.socket.getaddrinfo
        self._orig_installed = url_validator._SSRF_GUARD_INSTALLED

    def tearDown(self):
        url_validator.socket.getaddrinfo = self._orig_getaddrinfo
        url_validator._SSRF_GUARD_INSTALLED = self._orig_installed

    def _install_with_fake(self):
        def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]

        url_validator.socket.getaddrinfo = fake_getaddrinfo
        url_validator._SSRF_GUARD_INSTALLED = False
        url_validator.install_ssrf_guard()

    def test_blocks_private_ip_for_youtube_domain(self):
        self._install_with_fake()
        with self.assertRaises(socket.gaierror):
            url_validator.socket.getaddrinfo("www.youtube.com", 443)

    def test_passes_through_non_youtube_host(self):
        self._install_with_fake()
        results = url_validator.socket.getaddrinfo("redis", 6379)
        self.assertEqual(results[0][4][0], "10.0.0.1")

    def test_passes_through_ip_literal(self):
        self._install_with_fake()
        results = url_validator.socket.getaddrinfo("0.0.0.0", 5000)
        self.assertEqual(results[0][4][0], "10.0.0.1")


if __name__ == "__main__":
    unittest.main()
