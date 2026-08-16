import unittest
from url_validator import validate_youtube_url, is_playlist_url, normalize_playlist_url
from rate_limiter import get_client_ip
import os
from unittest.mock import patch


class TestSecurityFeatures(unittest.TestCase):

    def test_ssrf_protection_private_ips(self):
        """Test that private IPs are blocked"""
        # These should all be invalid
        invalid_urls = [
            "http://localhost:8080",
            "http://127.0.0.1:5000",
            "http://192.168.1.1",
            "http://10.0.0.1",
            "http://172.16.0.1",
            "http://169.254.169.254",  # Cloud metadata
            "http://[::1]:5000",  # IPv6 loopback
            "http://0.0.0.0:5000",
        ]

        for url in invalid_urls:
            is_valid, _ = validate_youtube_url(url)
            self.assertFalse(is_valid, f"Should block {url}")

    def test_ssrf_protection_youtube_urls(self):
        """Test that valid YouTube URLs are allowed"""
        valid_urls = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        ]

        for url in valid_urls:
            is_valid, _ = validate_youtube_url(url)
            self.assertTrue(is_valid, f"Should allow {url}")

    def test_xss_protection_filename(self):
        """Test that filenames are sanitized"""
        from app import sanitize_filename

        test_cases = [
            ("<script>alert(1)</script>", "scriptalert(1)script"),
            ("file/with/slashes", "filewithslashes"),
            ("file\\with\\backslashes", "filewithbackslashes"),
            ("file:with:colons", "filewithcolons"),
            ("file*with*asterisks", "filewithasterisks"),
            ("file?with?questions", "filewithquestions"),
            ('file"with"quotes', "filewithquotes"),
            ("file<with>brackets", "filewithbrackets"),
            ("file|with|pipes", "filewithpipes"),
        ]

        for input_name, expected in test_cases:
            result = sanitize_filename(input_name)
            self.assertEqual(result, expected)

    def test_playlist_url_detection(self):
        """Test playlist URL detection"""
        playlist_urls = [
            "https://www.youtube.com/playlist?list=PL1234567890",
            "https://www.youtube.com/watch?v=abc123&list=PL1234567890",
        ]

        for url in playlist_urls:
            self.assertTrue(is_playlist_url(url), f"Should detect playlist: {url}")

    def test_playlist_normalization(self):
        """Test playlist URL normalization"""
        test_cases = [
            (
                "https://www.youtube.com/watch?v=abc123&list=PL1234567890",
                "https://www.youtube.com/playlist?list=PL1234567890",
            ),
        ]

        for input_url, expected in test_cases:
            result = normalize_playlist_url(input_url)
            self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
