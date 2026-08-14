import unittest

from url_validator import get_playlist_id, is_playlist_url, normalize_playlist_url


class PlaylistRoutingTest(unittest.TestCase):
    def test_get_playlist_id_from_watch_with_list(self):
        self.assertEqual(
            get_playlist_id(
                "https://www.youtube.com/watch?v=PKluXW-DTfs"
                "&list=PLcpDUrLqJs2NFk_UQ2ZlWSZs5KDbel1ZL&index=8"
            ),
            "PLcpDUrLqJs2NFk_UQ2ZlWSZs5KDbel1ZL",
        )

    def test_get_playlist_id_from_playlist_url(self):
        self.assertEqual(
            get_playlist_id("https://www.youtube.com/playlist?list=PLabc123"),
            "PLabc123",
        )

    def test_get_playlist_id_none_without_list(self):
        self.assertIsNone(get_playlist_id("https://www.youtube.com/watch?v=abc123"))
        self.assertIsNone(get_playlist_id("https://youtu.be/abc123"))

    def test_is_playlist_url(self):
        self.assertTrue(is_playlist_url("https://www.youtube.com/watch?v=X&list=Y"))
        self.assertTrue(is_playlist_url("https://www.youtube.com/playlist?list=Y"))
        self.assertFalse(is_playlist_url("https://www.youtube.com/watch?v=X"))
        self.assertFalse(is_playlist_url("https://youtu.be/X"))

    def test_normalize_watch_url_to_playlist(self):
        url = "https://www.youtube.com/watch?v=PKluXW-DTfs&list=PLabc&index=8"
        self.assertEqual(
            normalize_playlist_url(url),
            "https://www.youtube.com/playlist?list=PLabc",
        )

    def test_normalize_playlist_url_unchanged(self):
        url = "https://www.youtube.com/playlist?list=PLabc"
        self.assertEqual(normalize_playlist_url(url), url)

    def test_normalize_non_playlist_url_unchanged(self):
        url = "https://www.youtube.com/watch?v=PKluXW-DTfs"
        self.assertEqual(normalize_playlist_url(url), url)


if __name__ == "__main__":
    unittest.main()
