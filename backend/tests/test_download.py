import os
import tempfile
import unittest

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


class UniqueZipNameTest(unittest.TestCase):
    def test_names_are_clean_and_deduped(self):
        used = set()
        self.assertEqual(app.unique_zip_name("Song", used), "Song.mp3")
        self.assertEqual(app.unique_zip_name("Song", used), "Song (2).mp3")
        self.assertEqual(app.unique_zip_name("Song", used), "Song (3).mp3")
        self.assertEqual(app.unique_zip_name("Other", used), "Other.mp3")


if __name__ == "__main__":
    unittest.main()
