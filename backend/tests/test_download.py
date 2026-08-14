import base64
import os
import tempfile
import unittest
from unittest import mock

import app


class MakeProgressHookTest(unittest.TestCase):
    def setUp(self):
        app.download_jobs.clear()

    def tearDown(self):
        app.download_jobs.clear()

    def test_single_video_progress_updates(self):
        app.download_jobs["j1"] = {"status": "processing", "progress": 0}
        hook = app.make_progress_hook("j1")
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        self.assertEqual(app.download_jobs["j1"]["progress"], 50.0)
        hook({"status": "finished"})
        self.assertEqual(app.download_jobs["j1"]["progress"], 100.0)

    def test_playlist_progress_combines_index(self):
        app.download_jobs["j2"] = {"status": "processing", "progress": 0}
        hook = app.make_progress_hook("j2", video_index=0, total_videos=2)
        hook({"status": "downloading", "downloaded_bytes": 25, "total_bytes": 100})
        self.assertEqual(app.download_jobs["j2"]["progress"], 12.5)

        hook = app.make_progress_hook("j2", video_index=1, total_videos=2)
        hook({"status": "downloading", "downloaded_bytes": 100, "total_bytes": 100})
        self.assertEqual(app.download_jobs["j2"]["progress"], 100.0)

    def test_missing_total_does_not_crash(self):
        app.download_jobs["j3"] = {"status": "processing", "progress": 0}
        hook = app.make_progress_hook("j3")
        hook({"status": "downloading", "downloaded_bytes": 50})
        self.assertEqual(app.download_jobs["j3"]["progress"], 0)

    def test_unknown_job_is_ignored(self):
        hook = app.make_progress_hook("missing")
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        self.assertNotIn("missing", app.download_jobs)


class GetYdlOptsTest(unittest.TestCase):
    def test_thumbnail_pipeline(self):
        opts = app.get_ydl_opts()
        keys = [pp["key"] for pp in opts["postprocessors"]]
        self.assertEqual(
            keys,
            ["FFmpegExtractAudio", "FFmpegThumbnailsConvertor", "EmbedThumbnail", "FFmpegMetadata"],
        )
        thumb = opts["postprocessors"][1]
        self.assertEqual(thumb["format"], "jpg")
        self.assertTrue(opts["writethumbnail"])
        self.assertNotIn("progress_hooks", opts)

    def test_progress_hook_is_wired(self):
        sentinel = lambda d: None
        opts = app.get_ydl_opts(progress_hook=sentinel)
        self.assertEqual(opts["progress_hooks"], [sentinel])

    def test_ignoreerrors_not_set_so_errors_surface(self):
        # `ignoreerrors` would make extract_info() return None on a bot check
        # instead of raising, hiding the real reason from our friendly handler.
        for opts in (app.get_ydl_opts(), app.get_info_opts()):
            self.assertNotIn("ignoreerrors", opts)

    def test_long_downloads_do_not_skip_fragments_or_use_old_size_cap(self):
        opts = app.get_ydl_opts()
        self.assertFalse(opts["skip_unavailable_fragments"])
        self.assertEqual(opts["live_from_start"], app.DOWNLOAD_LIVESTREAM_FROM_START)
        self.assertEqual(opts["max_filesize"], app.MAX_FILE_SIZE_MB * 1024 * 1024)


class MediaDurationTest(unittest.TestCase):
    def test_accepts_complete_output(self):
        probe_result = mock.Mock(returncode=0, stdout="4342.0\n")
        with mock.patch.object(app.subprocess, "run", return_value=probe_result):
            self.assertIsNone(app.validate_media_duration("podcast.mp3", 4342))

    def test_accepts_small_metadata_difference(self):
        # FFprobe can report a few percent less than YouTube's stated
        # duration on a complete file; that must not block the download.
        probe_result = mock.Mock(returncode=0, stdout="4240.0\n")
        with mock.patch.object(app.subprocess, "run", return_value=probe_result):
            self.assertIsNone(app.validate_media_duration("podcast.mp3", 4342))

    def test_rejects_truncated_output(self):
        probe_result = mock.Mock(returncode=0, stdout="667.0\n")
        with mock.patch.object(app.subprocess, "run", return_value=probe_result):
            error = app.validate_media_duration("podcast.mp3", 4342)
        self.assertIn("Incomplete download detected", error)
        self.assertIn("11.1 minutes", error)

    def test_rejects_output_when_duration_cannot_be_verified(self):
        probe_result = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(app.subprocess, "run", return_value=probe_result):
            error = app.validate_media_duration("podcast.mp3", 4342)
        self.assertIn("Could not verify", error)


class AuthOptsTest(unittest.TestCase):
    """Cookies.txt / proxy are wired into yt-dlp opts only when configured."""

    def setUp(self):
        self._saved = (
            app.COOKIE_FILE,
            app.YOUTUBE_COOKIES_B64,
            app.YOUTUBE_PROXY,
            app.YOUTUBE_USER_AGENT,
            app.YOUTUBE_COOKIE_BEHAVIOR,
        )
        self._tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        self._tmp.write(b"# Netscape HTTP Cookie File")
        self._tmp.close()
        app.YOUTUBE_COOKIE_BEHAVIOR = "all"

    def tearDown(self):
        (
            app.COOKIE_FILE,
            app.YOUTUBE_COOKIES_B64,
            app.YOUTUBE_PROXY,
            app.YOUTUBE_USER_AGENT,
            app.YOUTUBE_COOKIE_BEHAVIOR,
        ) = self._saved
        os.unlink(self._tmp.name)
        dest = app.TEMP_DIR / "cookies.txt"
        if dest.exists():
            dest.unlink()

    def test_no_auth_opts_by_default(self):
        app.COOKIE_FILE = ""
        app.YOUTUBE_COOKIES_B64 = ""
        app.YOUTUBE_PROXY = ""
        app.YOUTUBE_USER_AGENT = ""
        for opts in (app.get_ydl_opts(), app.get_info_opts()):
            self.assertNotIn("cookiefile", opts)
            self.assertNotIn("proxy", opts)

    def test_cookie_file_is_passed_when_present(self):
        app.COOKIE_FILE = self._tmp.name
        app.YOUTUBE_COOKIES_B64 = ""
        app.YOUTUBE_PROXY = ""
        app.YOUTUBE_USER_AGENT = ""
        for opts in (
            app.get_ydl_opts(use_cookies=True),
            app.get_info_opts(use_cookies=True),
        ):
            self.assertEqual(opts["cookiefile"], self._tmp.name)
            self.assertNotIn("proxy", opts)

    def test_when_needed_starts_without_cookies(self):
        app.COOKIE_FILE = self._tmp.name
        app.YOUTUBE_COOKIES_B64 = ""
        app.YOUTUBE_PROXY = ""
        app.YOUTUBE_USER_AGENT = ""
        app.YOUTUBE_COOKIE_BEHAVIOR = "when_needed"
        for opts in (app.get_ydl_opts(), app.get_info_opts()):
            self.assertNotIn("cookiefile", opts)

    def test_missing_cookie_file_is_skipped(self):
        app.COOKIE_FILE = "/nonexistent/cookies.txt"
        app.YOUTUBE_COOKIES_B64 = ""
        app.YOUTUBE_PROXY = ""
        app.YOUTUBE_USER_AGENT = ""
        for opts in (app.get_ydl_opts(), app.get_info_opts()):
            self.assertNotIn("cookiefile", opts)

    def test_readonly_cookie_file_is_copied_to_writable_location(self):
        app.COOKIE_FILE = self._tmp.name
        app.YOUTUBE_COOKIES_B64 = ""
        app.YOUTUBE_PROXY = ""
        app.YOUTUBE_USER_AGENT = ""
        with mock.patch.object(app, "_file_is_writable", return_value=False):
            opts = app.get_info_opts(use_cookies=True)
        dest = app.TEMP_DIR / "cookies.txt"
        self.assertEqual(opts["cookiefile"], str(dest))
        self.assertTrue(dest.exists())

    def test_base64_cookie_content_is_written_and_passed(self):
        app.COOKIE_FILE = ""
        app.YOUTUBE_COOKIES_B64 = base64.b64encode(
            b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tSID\tvalue\n"
        ).decode()
        app.YOUTUBE_PROXY = ""
        app.YOUTUBE_USER_AGENT = ""
        opts = app.get_info_opts(use_cookies=True)
        self.assertEqual(opts["cookiefile"], str(app.COOKIE_CACHE_FILE))
        self.assertTrue(app.COOKIE_CACHE_FILE.read_bytes().startswith(b"# Netscape"))

    def test_proxy_and_user_agent_are_passed(self):
        app.COOKIE_FILE = ""
        app.YOUTUBE_COOKIES_B64 = ""
        app.YOUTUBE_PROXY = "http://user:pass@proxy.example:8080"
        app.YOUTUBE_USER_AGENT = "Mozilla/5.0 test"
        for opts in (app.get_ydl_opts(), app.get_info_opts()):
            self.assertEqual(opts["proxy"], "http://user:pass@proxy.example:8080")
            self.assertEqual(opts["http_headers"], {"User-Agent": "Mozilla/5.0 test"})
            self.assertNotIn("cookiefile", opts)


class FriendlyYtdlpErrorTest(unittest.TestCase):
    """yt-dlp failures map to actionable, user-safe messages."""

    def setUp(self):
        self._saved = (
            app.COOKIE_FILE,
            app.YOUTUBE_COOKIES_B64,
            app.YOUTUBE_COOKIE_BEHAVIOR,
        )
        app.YOUTUBE_COOKIES_B64 = ""
        app.YOUTUBE_COOKIE_BEHAVIOR = "when_needed"

    def tearDown(self):
        (
            app.COOKIE_FILE,
            app.YOUTUBE_COOKIES_B64,
            app.YOUTUBE_COOKIE_BEHAVIOR,
        ) = self._saved

    def test_bot_check_without_cookies(self):
        app.COOKIE_FILE = ""
        msg = app._friendly_ytdlp_error(
            Exception("Sign in to confirm you're not a bot.")
        )
        self.assertIn("bot check", msg)
        self.assertNotIn("cookies", msg)

    def test_bot_check_with_cookies_suggests_stale_cookies(self):
        app.COOKIE_FILE = "/etc/secrets/cookies.txt"
        msg = app._friendly_ytdlp_error(
            Exception("Sign in to confirm you're not a bot.")
        )
        self.assertIn("expired", msg)
        self.assertIn("cookies", msg)

    def test_sign_in_error_with_cookies(self):
        app.COOKIE_FILE = "/etc/secrets/cookies.txt"
        msg = app._friendly_ytdlp_error(Exception("Sign in required"))
        self.assertIn("expired", msg)

    def test_403(self):
        app.COOKIE_FILE = ""
        msg = app._friendly_ytdlp_error(Exception("HTTP Error 403: Forbidden"))
        self.assertIn("403", msg)

    def test_cookie_file_os_error_does_not_map_to_session_expired(self):
        # A read-only-mount error mentions "cookies.txt" in the path but is not
        # an auth failure, so it must not be reported as an expired session.
        app.COOKIE_FILE = "/etc/secrets/cookies.txt"
        msg = app._friendly_ytdlp_error(
            Exception("[Errno 30] Read-only file system: '/etc/secrets/cookies.txt'")
        )
        self.assertIsNone(msg)

    def test_unknown_returns_none(self):
        app.COOKIE_FILE = ""
        self.assertIsNone(
            app._friendly_ytdlp_error(Exception("something else entirely"))
        )


class FileEndpointAuthTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        fd, self.tmp_file = tempfile.mkstemp(suffix=".mp3")
        os.write(fd, b"fake-mp3")
        os.close(fd)

    def tearDown(self):
        app.download_jobs.clear()
        if os.path.exists(self.tmp_file):
            os.unlink(self.tmp_file)

    def _add_completed_job(self, owner_ip):
        app.download_jobs["job-auth"] = {
            "status": "completed",
            "progress": 100,
            "ip": owner_ip,
            "file": self.tmp_file,
            "filename": "x.mp3",
        }

    def test_rejects_foreign_ip(self):
        self._add_completed_job("9.9.9.9")
        resp = self.client.get(
            "/api/download/job-auth/file", environ_base={"REMOTE_ADDR": "1.2.3.4"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_allows_owner_ip(self):
        self._add_completed_job("1.2.3.4")
        resp = self.client.get(
            "/api/download/job-auth/file", environ_base={"REMOTE_ADDR": "1.2.3.4"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"fake-mp3")

    def test_not_ready_job_is_404(self):
        app.download_jobs["job-auth"] = {
            "status": "processing",
            "progress": 0,
            "ip": "1.2.3.4",
        }
        resp = self.client.get(
            "/api/download/job-auth/file", environ_base={"REMOTE_ADDR": "1.2.3.4"}
        )
        self.assertEqual(resp.status_code, 404)


class _FakeYDL:
    def __init__(self, result):
        self._result = result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class CookieRetryTest(unittest.TestCase):
    def setUp(self):
        self._saved = (
            app.YOUTUBE_COOKIE_BEHAVIOR,
            app.COOKIE_FILE,
            app.YOUTUBE_COOKIES_B64,
        )

    def tearDown(self):
        (
            app.YOUTUBE_COOKIE_BEHAVIOR,
            app.COOKIE_FILE,
            app.YOUTUBE_COOKIES_B64,
        ) = self._saved

    def test_when_needed_retries_auth_failure_with_cookies(self):
        app.YOUTUBE_COOKIE_BEHAVIOR = "when_needed"
        app.COOKIE_FILE = "/etc/secrets/cookies.txt"
        app.YOUTUBE_COOKIES_B64 = ""
        options = []
        bot = Exception("Sign in to confirm you're not a bot.")
        success = {"id": "video", "title": "Video"}
        with mock.patch.object(
            app.yt_dlp,
            "YoutubeDL",
            side_effect=[_FakeYDL(bot), _FakeYDL(success)],
        ), mock.patch.object(
            app, "_writable_cookie_file", return_value="/tmp/cookies.txt"
        ):
            result = app._extract_info_with_cookie_retry(
                "https://www.youtube.com/watch?v=video",
                lambda use_cookies: options.append(use_cookies) or {},
            )

        self.assertEqual(result, success)
        self.assertEqual(options, [False, True])

    def test_when_needed_does_not_retry_unrelated_failure(self):
        app.YOUTUBE_COOKIE_BEHAVIOR = "when_needed"
        options = []
        with mock.patch.object(
            app.yt_dlp,
            "YoutubeDL",
            return_value=_FakeYDL(Exception("video unavailable")),
        ), mock.patch.object(
            app, "_writable_cookie_file", return_value="/tmp/cookies.txt"
        ):
            with self.assertRaisesRegex(Exception, "video unavailable"):
                app._extract_info_with_cookie_retry(
                    "https://www.youtube.com/watch?v=video",
                    lambda use_cookies: options.append(use_cookies) or {},
                )

        self.assertEqual(options, [False])

    def test_all_starts_with_cookies(self):
        app.YOUTUBE_COOKIE_BEHAVIOR = "all"
        options = []
        with mock.patch.object(
            app.yt_dlp,
            "YoutubeDL",
            return_value=_FakeYDL({"id": "video"}),
        ):
            app._extract_info_with_cookie_retry(
                "https://www.youtube.com/watch?v=video",
                lambda use_cookies: options.append(use_cookies) or {},
            )
        self.assertEqual(options, [True])


class HealthCheckTest(unittest.TestCase):
    """/api/health classifies YouTube extraction status correctly."""

    def test_ok_when_extraction_succeeds(self):
        with mock.patch.object(
            app.yt_dlp, "YoutubeDL",
            return_value=_FakeYDL({"id": "jNQXAC9IVRw", "title": "Me at the zoo"}),
        ):
            result = app._run_health_check()
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["detail"])

    def test_blocked_when_bot_check(self):
        bot = Exception("Sign in to confirm you're not a bot.")
        with mock.patch.object(
            app.yt_dlp, "YoutubeDL", return_value=_FakeYDL(bot),
        ):
            result = app._run_health_check()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("bot check", result["detail"])

    def test_error_when_extraction_returns_nothing(self):
        with mock.patch.object(
            app.yt_dlp, "YoutubeDL", return_value=_FakeYDL(None),
        ):
            result = app._run_health_check()
        self.assertEqual(result["status"], "error")

    def test_error_when_unknown_exception(self):
        with mock.patch.object(
            app.yt_dlp, "YoutubeDL",
            return_value=_FakeYDL(Exception("something broke")),
        ):
            result = app._run_health_check()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["exception"], "Exception")


class UniqueZipNameTest(unittest.TestCase):
    def test_names_are_clean_and_deduped(self):
        used = set()
        self.assertEqual(app.unique_zip_name("Song", used), "Song.mp3")
        self.assertEqual(app.unique_zip_name("Song", used), "Song (2).mp3")
        self.assertEqual(app.unique_zip_name("Song", used), "Song (3).mp3")
        self.assertEqual(app.unique_zip_name("Other", used), "Other.mp3")


if __name__ == "__main__":
    unittest.main()
