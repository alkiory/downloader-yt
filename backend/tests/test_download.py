import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app


class SanitizeFilenameTest(unittest.TestCase):
    def test_removes_invalid_characters(self):
        self.assertEqual(
            app.sanitize_filename('a<b>c:d"e/f\\g|h?i*j'), "abcdefghij"
        )

    def test_strips_leading_and_trailing_dots_and_spaces(self):
        self.assertEqual(app.sanitize_filename("  title... "), "title")

    def test_truncates_long_names(self):
        self.assertEqual(len(app.sanitize_filename("x" * 300)), 200)


class GetYdlOptsTest(unittest.TestCase):
    def test_postprocessor_pipeline(self):
        opts = app.get_ydl_opts()
        keys = [pp["key"] for pp in opts["postprocessors"]]
        self.assertEqual(
            keys, ["FFmpegExtractAudio", "EmbedThumbnail", "FFmpegMetadata"]
        )
        audio = opts["postprocessors"][0]
        self.assertEqual(audio["preferredcodec"], "mp3")
        self.assertEqual(audio["preferredquality"], app.BITRATE)
        self.assertTrue(opts["writethumbnail"])

    def test_ignoreerrors_is_set(self):
        # ignoreerrors lets playlist extraction skip a bad entry instead of
        # aborting the whole run.
        for opts in (app.get_ydl_opts(), app.get_info_opts()):
            self.assertTrue(opts["ignoreerrors"])

    def test_fragment_and_size_limits(self):
        opts = app.get_ydl_opts()
        self.assertTrue(opts["skip_unavailable_fragments"])
        self.assertEqual(opts["max_filesize"], app.MAX_FILE_SIZE_MB * 1024 * 1024)

    def test_player_client_and_skip_list(self):
        for opts in (app.get_ydl_opts(), app.get_info_opts()):
            yt = opts["extractor_args"]["youtube"]
            self.assertEqual(yt["player_client"], ["android", "web"])
            self.assertEqual(yt["skip"], ["hls", "dash", "translated_subs"])

    def test_info_opts_flattens_playlist(self):
        self.assertEqual(app.get_info_opts()["extract_flat"], "in_playlist")

    def test_outtmpl_defaults_to_download_folder(self):
        self.assertEqual(
            app.get_ydl_opts()["outtmpl"],
            str(app.DOWNLOAD_FOLDER / "%(title)s.%(ext)s"),
        )

    def test_outtmpl_uses_provided_output_path(self):
        target = Path("/tmp/out/%(title)s.%(ext)s")
        self.assertEqual(app.get_ydl_opts(target)["outtmpl"], str(target))


class GetMediaDurationTest(unittest.TestCase):
    def test_returns_duration_on_success(self):
        probe = mock.Mock(returncode=0, stdout="4342.0\n")
        with mock.patch.object(app.subprocess, "run", return_value=probe):
            self.assertEqual(app.get_media_duration("podcast.mp3"), 4342.0)

    def test_returns_none_when_probe_fails(self):
        probe = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(app.subprocess, "run", return_value=probe):
            self.assertIsNone(app.get_media_duration("podcast.mp3"))

    def test_returns_none_on_exception(self):
        with mock.patch.object(app.subprocess, "run", side_effect=OSError("boom")):
            self.assertIsNone(app.get_media_duration("podcast.mp3"))


class ConvertToMp3Test(unittest.TestCase):
    def test_returns_true_on_success(self):
        result = mock.Mock(returncode=0)
        with mock.patch.object(app.subprocess, "run", return_value=result):
            self.assertTrue(app.convert_to_mp3("in.webm", "out.mp3"))

    def test_returns_false_on_failure(self):
        result = mock.Mock(returncode=1)
        with mock.patch.object(app.subprocess, "run", return_value=result):
            self.assertFalse(app.convert_to_mp3("in.webm", "out.mp3"))

    def test_returns_false_on_exception(self):
        with mock.patch.object(app.subprocess, "run", side_effect=OSError("boom")):
            self.assertFalse(app.convert_to_mp3("in.webm", "out.mp3"))


class FindMp3Test(unittest.TestCase):
    def test_returns_mp3_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            mp3 = path / "song.mp3"
            mp3.touch()
            (path / "cover.jpg").touch()
            self.assertEqual(app.find_mp3(path), mp3)

    def test_returns_none_when_no_mp3(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            (path / "cover.jpg").touch()
            self.assertIsNone(app.find_mp3(path))


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


class DownloadSingleVideoTest(unittest.TestCase):
    def setUp(self):
        app.download_jobs.clear()
        app.download_history.clear()

    def tearDown(self):
        app.download_jobs.clear()
        app.download_history.clear()

    def test_success_marks_job_completed(self):
        app.download_jobs["job"] = {"status": "queued", "progress": 0}
        with mock.patch.object(
            app.yt_dlp, "YoutubeDL", return_value=_FakeYDL({"title": "My Song"})
        ), mock.patch.object(
            app, "find_mp3", return_value=Path("/tmp/fake/song.mp3")
        ), mock.patch.object(app.shutil, "move") as move_mock, mock.patch.object(
            app, "record_download"
        ) as record_mock:
            app.download_single_video("https://youtu.be/x", "job", "1.2.3.4")

        self.assertEqual(app.download_jobs["job"]["status"], "completed")
        self.assertTrue(app.download_jobs["job"]["file"].endswith("My Song.mp3"))
        self.assertEqual(app.download_jobs["job"]["filename"], "My Song.mp3")
        move_mock.assert_called_once()
        record_mock.assert_called_once_with("1.2.3.4")

    def test_missing_mp3_marks_job_failed(self):
        app.download_jobs["job"] = {"status": "queued", "progress": 0}
        with mock.patch.object(
            app.yt_dlp, "YoutubeDL", return_value=_FakeYDL({"title": "My Song"})
        ), mock.patch.object(app, "find_mp3", return_value=None):
            app.download_single_video("https://youtu.be/x", "job", "1.2.3.4")

        self.assertEqual(app.download_jobs["job"]["status"], "failed")
        self.assertEqual(
            app.download_jobs["job"]["error"], "Failed to download video"
        )


class DownloadStatusEndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def tearDown(self):
        app.download_jobs.clear()

    def test_unknown_job_is_404(self):
        self.assertEqual(self.client.get("/api/download/missing").status_code, 404)

    def test_queued_job_reports_progress(self):
        app.download_jobs["job1"] = {"status": "queued", "progress": 0}
        data = self.client.get("/api/download/job1").get_json()
        self.assertEqual(data["status"], "queued")
        self.assertEqual(data["progress"], 0)

    def test_completed_job_includes_download_url(self):
        app.download_jobs["job1"] = {
            "status": "completed",
            "progress": 100,
            "file": "x",
            "filename": "song.mp3",
        }
        data = self.client.get("/api/download/job1").get_json()
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["download_url"], "/api/download/job1/file")
        self.assertEqual(data["filename"], "song.mp3")

    def test_failed_job_includes_error(self):
        app.download_jobs["job1"] = {
            "status": "failed",
            "progress": 0,
            "error": "Download failed",
        }
        data = self.client.get("/api/download/job1").get_json()
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"], "Download failed")


class FileEndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        fd, self.tmp_file = tempfile.mkstemp(suffix=".mp3")
        os.write(fd, b"fake-mp3")
        os.close(fd)

    def tearDown(self):
        app.download_jobs.clear()
        if os.path.exists(self.tmp_file):
            os.unlink(self.tmp_file)

    def test_completed_job_returns_file(self):
        app.download_jobs["job-file"] = {
            "status": "completed",
            "progress": 100,
            "file": self.tmp_file,
            "filename": "x.mp3",
        }
        resp = self.client.get("/api/download/job-file/file")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"fake-mp3")

    def test_not_ready_job_is_404(self):
        app.download_jobs["job-file"] = {"status": "processing", "progress": 0}
        resp = self.client.get("/api/download/job-file/file")
        self.assertEqual(resp.status_code, 404)

    def test_missing_file_is_404(self):
        app.download_jobs["job-file"] = {
            "status": "completed",
            "progress": 100,
            "file": "/nonexistent/file.mp3",
            "filename": "x.mp3",
        }
        resp = self.client.get("/api/download/job-file/file")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
