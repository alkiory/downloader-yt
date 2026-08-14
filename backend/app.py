from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import base64
import binascii
from url_validator import (
    validate_youtube_url,
    is_playlist_url,
    normalize_playlist_url,
    install_ssrf_guard,
)
from rate_limiter import get_client_ip, create_limiter, RATE_LIMIT_ENABLED
import yt_dlp
import os
import uuid
import zipfile
import threading
from pathlib import Path
import re
import subprocess
import tempfile
import shutil
import time
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Restrict CORS to same-origin only
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "https://downloader-yt-latest.onrender.com",
                "http://localhost:5000",
                "http://127.0.0.1:5000",
            ]
        }
    },
)

# Initialize rate limiter
limiter = create_limiter(app)

# Validate YouTube-domain DNS resolutions at fetch time too, closing the
# SSRF DNS-rebinding window (validation + download use the same check).
install_ssrf_guard()

# Configuration from environment variables
DOWNLOAD_FOLDER = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/youtube_downloader"))
DOWNLOAD_FOLDER.mkdir(exist_ok=True, mode=0o700)

TEMP_DIR = Path(tempfile.gettempdir()) / "youtube_downloader"
TEMP_DIR.mkdir(exist_ok=True, mode=0o700)
COOKIE_CACHE_FILE = TEMP_DIR / "cookies.txt"

# Limits
MAX_PLAYLIST_SIZE = int(os.environ.get("MAX_PLAYLIST_SIZE", "50"))
MAX_DOWNLOADS_PER_HOUR = int(os.environ.get("MAX_DOWNLOADS_PER_HOUR", "10"))
MAX_DOWNLOADS_PER_DAY = int(os.environ.get("MAX_DOWNLOADS_PER_DAY", "50"))
# 250 MB accommodates long podcasts at the configured 192 kbps bitrate
# without selecting a shorter fallback format because of the size cap.
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "250"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "300"))
DOWNLOAD_LIVESTREAM_FROM_START = (
    os.environ.get("DOWNLOAD_LIVESTREAM_FROM_START", "true").lower() == "true"
)
PORT = int(os.environ.get("PORT", "5000"))
BITRATE = os.environ.get("BITRATE", "192")
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "3600"))  # 1 hour

# Which YouTube player clients yt-dlp should try, in order. The `web` client
# now requires a PO token and is the one most aggressively bot-checked from
# datacenter IPs (Render), so it is NOT in the default. This is a moving
# target — override via YOUTUBE_PLAYER_CLIENTS="android,ios,tv" if needed.
YOUTUBE_PLAYER_CLIENTS = [
    c.strip()
    for c in os.environ.get("YOUTUBE_PLAYER_CLIENTS", "android,ios").split(",")
    if c.strip()
]

# Optional auth/egress knobs for getting past YouTube's bot check on
# datacenter IPs. See _apply_auth_opts() for details. Cookie content can be
# supplied either as a mounted Netscape file or as base64 in a secret env var.
COOKIE_FILE = os.environ.get("COOKIE_FILE", "").strip()
YOUTUBE_COOKIES_B64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
YOUTUBE_PROXY = os.environ.get("YOUTUBE_PROXY", "").strip()
YOUTUBE_USER_AGENT = os.environ.get("YOUTUBE_USER_AGENT", "").strip()
YOUTUBE_COOKIE_BEHAVIOR = os.environ.get(
    "YOUTUBE_COOKIE_BEHAVIOR", "when_needed"
).strip().lower()
if YOUTUBE_COOKIE_BEHAVIOR not in {"disabled", "when_needed", "all"}:
    logger.warning(
        "Invalid YOUTUBE_COOKIE_BEHAVIOR=%r; using when_needed",
        YOUTUBE_COOKIE_BEHAVIOR,
    )
    YOUTUBE_COOKIE_BEHAVIOR = "when_needed"
_cookie_cache_lock = threading.Lock()

# /api/health probes this stable public video to see if YouTube extraction
# currently works. Results are cached to avoid re-hitting YouTube on every poll.
HEALTH_CHECK_URL = os.environ.get(
    "HEALTH_CHECK_URL", "https://www.youtube.com/watch?v=jNQXAC9IVRw"
)
HEALTH_CHECK_TTL_SECONDS = int(os.environ.get("HEALTH_CHECK_TTL_SECONDS", "60"))

# When true, /api/info surfaces the underlying yt-dlp exception so the
# exact failure mode (403 / bot check / extractor error / etc.) is visible.
# Toggle on while debugging, off for normal users.
DEBUG_INFO = os.environ.get("DEBUG_INFO", "false").lower() == "true"

# Thread pool for downloads
download_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)

# Store download jobs with TTL
download_jobs = {}
jobs_lock = threading.Lock()

# Store download history for rate limiting
download_history = defaultdict(list)
history_lock = threading.Lock()

# Cached result of the last /api/health YouTube probe.
health_cache = {"checked_at": None, "result": None}
health_cache_lock = threading.Lock()


def sanitize_filename(filename):
    """Remove invalid characters from filename"""
    filename = re.sub(r'[<>:"/\\|?*]', "", filename)
    filename = filename.strip(". ")
    if len(filename) > 200:
        filename = filename[:200]
    return filename


def unique_zip_name(title, used_names):
    """Return a collision-free zip entry name for a video title.

    Repeated titles in a playlist get a numeric suffix so they don't
    overwrite each other inside the archive.
    """
    name = f"{title}.mp3"
    counter = 2
    while name in used_names:
        name = f"{title} ({counter}).mp3"
        counter += 1
    used_names.add(name)
    return name


def clean_old_downloads():
    """Clean downloads and jobs older than TTL"""
    current_time = time.time()

    # Clean files
    for file in DOWNLOAD_FOLDER.glob("*"):
        if current_time - file.stat().st_mtime > JOB_TTL_SECONDS:
            try:
                if file.is_file():
                    file.unlink()
                elif file.is_dir():
                    shutil.rmtree(file)
            except Exception as e:
                logger.warning(f"Error cleaning {file}: {str(e)}")

    # Clean jobs
    with jobs_lock:
        expired_jobs = [
            job_id
            for job_id, job in download_jobs.items()
            if current_time - job["created"].timestamp() > JOB_TTL_SECONDS
        ]
        for job_id in expired_jobs:
            del download_jobs[job_id]


def check_rate_limit(ip_address):
    """Check if IP has exceeded rate limits"""
    if not RATE_LIMIT_ENABLED:
        return True, ""

    with history_lock:
        current_time = datetime.now()
        download_history[ip_address] = [
            timestamp
            for timestamp in download_history[ip_address]
            if current_time - timestamp < timedelta(days=1)
        ]

        hourly_downloads = [
            timestamp
            for timestamp in download_history[ip_address]
            if current_time - timestamp < timedelta(hours=1)
        ]

        if len(hourly_downloads) >= MAX_DOWNLOADS_PER_HOUR:
            return (
                False,
                f"Hourly download limit reached ({MAX_DOWNLOADS_PER_HOUR} downloads/hour)",
            )

        if len(download_history[ip_address]) >= MAX_DOWNLOADS_PER_DAY:
            return (
                False,
                f"Daily download limit reached ({MAX_DOWNLOADS_PER_DAY} downloads/day)",
            )

        return True, ""


def record_download(ip_address):
    """Record a download for rate limiting"""
    if not RATE_LIMIT_ENABLED:
        return

    with history_lock:
        download_history[ip_address].append(datetime.now())


def make_progress_hook(job_id, video_index=0, total_videos=1):
    """Build a yt-dlp progress hook that reports real download progress.

    For playlists, `video_index`/`total_videos` fold each video's local
    progress (0..1) into an overall 0..100 percentage.
    """

    def hook(d):
        status = d.get("status")
        local = 0.0
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            if total:
                local = min(1.0, downloaded / total)
        elif status == "finished":
            local = 1.0

        overall = ((video_index + local) / total_videos) * 100
        with jobs_lock:
            job = download_jobs.get(job_id)
            if job is not None:
                job["progress"] = round(min(100.0, overall), 1)

    return hook


def get_ydl_opts(output_path=None, progress_hook=None, use_cookies=None):
    """Get yt-dlp options with proper format selection."""
    opts = {
        "format": "bestaudio/best",
        "outtmpl": (
            str(output_path)
            if output_path
            else str(DOWNLOAD_FOLDER / "%(title)s.%(ext)s")
        ),
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": BITRATE,
            },
            # Convert the (usually WebP) thumbnail to JPEG so it can be
            # embedded as MP3 cover art — FFmpeg can't embed WebP into MP3.
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "EmbedThumbnail"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "force_generic_extractor": False,
        "no_color": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        # Never return a silently truncated file when a media fragment fails.
        "skip_unavailable_fragments": False,
        "keepvideo": False,
        # The reported example is a YouTube live broadcast. Without this,
        # yt-dlp starts at the current live point instead of the beginning.
        "live_from_start": DOWNLOAD_LIVESTREAM_FROM_START,
        "noplaylist": False,
        "concurrent_fragment_downloads": 4,
        "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
        "extractor_args": {
            "youtube": {
                "player_client": YOUTUBE_PLAYER_CLIENTS,
                "skip": ["hls", "dash", "translated_subs"],
            }
        },
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return _apply_auth_opts(opts, use_cookies=use_cookies)


def get_info_opts(use_cookies=None):
    """Get yt-dlp options for info extraction."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "force_generic_extractor": False,
        "no_color": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_args": {
            "youtube": {
                "player_client": YOUTUBE_PLAYER_CLIENTS,
                "skip": ["hls", "dash", "translated_subs"],
            }
        },
    }
    return _apply_auth_opts(opts, use_cookies=use_cookies)


def _file_is_writable(path):
    """True if `path` can be opened for append (writable mount + perms).

    Render mounts secret files on a read-only filesystem, which makes any
    write return EROFS even if the mode bits look writable. An actual open-for-
    append probe is the only reliable way to detect that.
    """
    try:
        with open(path, "a"):
            pass
        return True
    except OSError:
        return False


def _write_env_cookie_file():
    """Decode base64 Netscape cookies into a private, writable temp file.

    Environment variables are useful on Render because they avoid relying on
    the exact mount path and preserve the cookie file's newlines. The decoded
    file never appears in logs and is restricted to the application user.
    """
    if not YOUTUBE_COOKIES_B64:
        return None

    try:
        encoded = "".join(YOUTUBE_COOKIES_B64.split())
        cookie_data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as e:
        logger.warning(f"YOUTUBE_COOKIES_B64 is not valid base64: {e}")
        return None

    if not cookie_data.startswith(
        (b"# HTTP Cookie File", b"# Netscape HTTP Cookie File")
    ):
        logger.warning(
            "YOUTUBE_COOKIES_B64 is not a Netscape-format cookies.txt file; "
            "continuing without cookies"
        )
        return None

    with _cookie_cache_lock:
        try:
            COOKIE_CACHE_FILE.write_bytes(cookie_data)
            os.chmod(COOKIE_CACHE_FILE, 0o600)
            return str(COOKIE_CACHE_FILE)
        except OSError as e:
            logger.warning(
                f"Could not write decoded YouTube cookies to {COOKIE_CACHE_FILE}: {e}"
            )
            return None


def _writable_cookie_file():
    """Return a writable path to configured cookies, or None.

    yt-dlp saves the cookie jar back to the file after requests, so a Render
    secret file mounted read-only is copied into the writable temp directory.
    The base64 env-var source is preferred when both sources are configured.
    """
    env_cookie_file = _write_env_cookie_file()
    if env_cookie_file:
        return env_cookie_file

    if not COOKIE_FILE or not os.path.isfile(COOKIE_FILE):
        if COOKIE_FILE:
            logger.warning(
                f"COOKIE_FILE is set but not found at {COOKIE_FILE}; "
                "continuing without cookies"
            )
        return None

    if _file_is_writable(COOKIE_FILE):
        return COOKIE_FILE

    try:
        shutil.copyfile(COOKIE_FILE, COOKIE_CACHE_FILE)
        os.chmod(COOKIE_CACHE_FILE, 0o600)
        return str(COOKIE_CACHE_FILE)
    except OSError as e:
        logger.warning(
            f"COOKIE_FILE is read-only and could not be copied to "
            f"{COOKIE_CACHE_FILE}: {e}; continuing without cookies"
        )
        return None


def _apply_auth_opts(opts, use_cookies=None):
    """Attach optional cookies.txt / proxy settings to yt-dlp options.

    On datacenter IPs (Render) YouTube bot-checks anonymous requests, so a
    deployed instance can authenticate with browser cookies and/or route
    through a residential proxy to escape IP flagging.

    COOKIE_FILE       — path to a Netscape-format cookies.txt exported from a
                         logged-in browser session (mounted into the container).
    YOUTUBE_COOKIES_B64 — base64-encoded Netscape cookie content.
    YOUTUBE_PROXY     — e.g. "http://user:pass@residential-proxy:port".
    YOUTUBE_USER_AGENT — optional browser User-Agent matching the cookies.
    """
    if use_cookies is None:
        use_cookies = YOUTUBE_COOKIE_BEHAVIOR == "all"
    if use_cookies and YOUTUBE_COOKIE_BEHAVIOR != "disabled":
        cookie_path = _writable_cookie_file()
        if cookie_path:
            opts["cookiefile"] = cookie_path
    if YOUTUBE_PROXY:
        opts["proxy"] = YOUTUBE_PROXY
    if YOUTUBE_USER_AGENT:
        opts["http_headers"] = {"User-Agent": YOUTUBE_USER_AGENT}
    return opts


def _is_cookie_retryable_error(exc):
    """Return whether a failure may be resolved by authenticated cookies."""
    msg = (str(exc) or "").lower()
    return any(
        phrase in msg
        for phrase in (
            "not a bot",
            "sign in",
            "login",
            "age-restricted",
            "age restricted",
            "http error 403",
        )
    )


def _extract_info_with_cookie_retry(url, opts_factory, download=False):
    """Extract with the configured cookie policy.

    ``when_needed`` deliberately makes the first request anonymous and retries
    only authentication-like failures with cookies. This limits cookie use and
    follows yt-dlp's warning that YouTube cookies can be rotated or associated
    with an IP address. ``all`` starts authenticated, while ``disabled`` never
    supplies cookies.
    """
    use_cookies = YOUTUBE_COOKIE_BEHAVIOR == "all"
    try:
        with yt_dlp.YoutubeDL(opts_factory(use_cookies)) as ydl:
            return ydl.extract_info(url, download=download)
    except Exception as exc:
        if (
            YOUTUBE_COOKIE_BEHAVIOR != "when_needed"
            or not _is_cookie_retryable_error(exc)
            or not _writable_cookie_file()
        ):
            raise

        logger.info("Retrying YouTube extraction with cookies after an auth-related failure")
        with yt_dlp.YoutubeDL(opts_factory(True)) as ydl:
            return ydl.extract_info(url, download=download)


def _friendly_ytdlp_error(exc):
    """Map a yt-dlp failure to a clear, user-safe message.

    Returns None when no specific hint applies, so callers can fall back to a
    generic message. The cookie hints matter most: if a bot check still fires
    while COOKIE_FILE is configured, the cookies are almost certainly stale or
    invalid and should be refreshed.
    """
    msg = (str(exc) or "").lower()

    if "not a bot" in msg:
        if (
            YOUTUBE_COOKIE_BEHAVIOR != "disabled"
            and (COOKIE_FILE or YOUTUBE_COOKIES_B64)
        ):
            return (
                "YouTube is still bot-checking the server. The configured "
                "cookies are likely expired or invalid — refresh cookies.txt."
            )
        return "YouTube is blocking this server with a bot check. Try again later."

    if "sign in" in msg or "login" in msg:
        if (
            YOUTUBE_COOKIE_BEHAVIOR != "disabled"
            and (COOKIE_FILE or YOUTUBE_COOKIES_B64)
        ):
            return "Your YouTube session has expired. Refresh cookies.txt and try again."
        return "This video requires sign-in (it may be age-restricted or members-only)."

    if "http error 403" in msg or "forbidden" in msg:
        return "YouTube rejected the request (403). The server IP may be rate-limited."

    if "video unavailable" in msg or "not available" in msg or "private" in msg:
        return "This video is unavailable (private, region-locked, or removed)."

    return None


def _run_health_check():
    """Probe a canary YouTube video and report extraction status.

    Returns a dict with status "ok" (extraction works), "blocked" (bot check)
    or "error" (anything else), plus the friendly detail when it fails.
    """
    result = {
        "status": "ok",
        "detail": None,
        "exception": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "cookie_behavior": YOUTUBE_COOKIE_BEHAVIOR,
        "cookies_configured": bool(COOKIE_FILE or YOUTUBE_COOKIES_B64),
        "cookies_available": bool(
            YOUTUBE_COOKIE_BEHAVIOR != "disabled" and _writable_cookie_file()
        ),
        "proxy_configured": bool(YOUTUBE_PROXY),
        "canary_url": HEALTH_CHECK_URL,
        "ttl_seconds": HEALTH_CHECK_TTL_SECONDS,
    }
    try:
        info = _extract_info_with_cookie_retry(
            HEALTH_CHECK_URL,
            get_info_opts,
        )
        if not info or not info.get("id"):
            result["status"] = "error"
            result["detail"] = "Extraction returned no result"
        return result
    except Exception as e:
        msg = str(e) or ""
        result["exception"] = type(e).__name__
        result["detail"] = _friendly_ytdlp_error(e) or msg[:300]
        result["status"] = "blocked" if "not a bot" in msg.lower() else "error"
        return result


def _media_duration_seconds(media_path):
    """Read a media file's duration with ffprobe, or return None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def validate_media_duration(media_path, expected_seconds):
    """Return an error for a suspiciously truncated output, otherwise None."""
    try:
        expected = float(expected_seconds or 0)
    except (TypeError, ValueError):
        return None

    if expected <= 0:
        return None

    actual = _media_duration_seconds(media_path)
    if actual is None:
        return "Could not verify the downloaded file duration"

    # FFprobe duration and YouTube's reported duration can differ by a small
    # amount on a complete file (container overhead, rounding, bitrate
    # estimation). Only reject when the file is materially shorter; log small
    # differences so a legitimate download is never blocked.
    if actual >= expected:
        return None

    shortfall = expected - actual
    tolerance = max(60.0, expected * 0.03)
    if shortfall > tolerance:
        return (
            f"Incomplete download detected ({actual / 60:.1f} minutes of "
            f"approximately {expected / 60:.1f})"
        )

    logger.warning(
        "Downloaded duration is %.1fs shorter than expected %.1fs for %s; "
        "accepting as a small metadata difference",
        shortfall,
        expected,
        media_path,
    )
    return None


def convert_to_mp3(input_file, output_file):
    """Convert video/audio file to MP3 using ffmpeg"""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(input_file),
                "-vn",
                "-acodec",
                "libmp3lame",
                "-ab",
                f"{BITRATE}k",
                "-ar",
                "44100",
                "-y",
                str(output_file),
            ],
            capture_output=True,
            text=True,
            timeout=DOWNLOAD_TIMEOUT,
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"FFmpeg conversion error: {str(e)}")
        return False


THUMBNAIL_SUFFIXES = {".webp", ".jpg", ".jpeg", ".png"}


def find_mp3(temp_path, title):
    """Locate the MP3 produced in temp_path, converting if necessary.

    With writethumbnail enabled, the temp dir also contains a thumbnail
    image, so we must look specifically for the .mp3 output.
    """
    for file in temp_path.glob("*.mp3"):
        if file.is_file():
            return file

    # Fallback: yt-dlp didn't produce an mp3; convert the first media file.
    for file in temp_path.glob("*"):
        if file.is_file() and file.suffix.lower() not in THUMBNAIL_SUFFIXES:
            output = temp_path / f"{title}.mp3"
            if convert_to_mp3(file, output):
                return output
    return None


def download_single_video(url, job_id, client_ip):
    """Download a single video in background"""
    try:
        with jobs_lock:
            download_jobs[job_id]["status"] = "processing"
            download_jobs[job_id]["progress"] = 0

        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temp_dir:
            temp_path = Path(temp_dir)
            output_path = temp_path / "%(title)s.%(ext)s"
            progress_hook = make_progress_hook(job_id)
            video_info = _extract_info_with_cookie_retry(
                url,
                lambda use_cookies: get_ydl_opts(
                    output_path,
                    progress_hook=progress_hook,
                    use_cookies=use_cookies,
                ),
                download=True,
            )

            if video_info:
                title = sanitize_filename(video_info.get("title", "video"))
                result_path = find_mp3(temp_path, title)

                if result_path:
                    duration_error = validate_media_duration(
                        result_path, video_info.get("duration")
                    )
                    if duration_error:
                        result_path.unlink(missing_ok=True)
                        with jobs_lock:
                            download_jobs[job_id]["status"] = "failed"
                            download_jobs[job_id]["error"] = duration_error
                        return

                    # Move to download folder
                    final_file = DOWNLOAD_FOLDER / f"{uuid.uuid4().hex}_{title}.mp3"
                    shutil.move(str(result_path), str(final_file))

                    with jobs_lock:
                        download_jobs[job_id]["status"] = "completed"
                        download_jobs[job_id]["progress"] = 100
                        download_jobs[job_id]["file"] = str(final_file)
                        download_jobs[job_id]["filename"] = f"{title}.mp3"

                    record_download(client_ip)
                    return

        with jobs_lock:
            download_jobs[job_id]["status"] = "failed"
            download_jobs[job_id]["error"] = "Failed to download video"

    except Exception as e:
        logger.exception(f"Download error for job {job_id}: {str(e)}")
        with jobs_lock:
            download_jobs[job_id]["status"] = "failed"
            download_jobs[job_id]["error"] = (
                _friendly_ytdlp_error(e) or "Download failed"
            )


def download_playlist(url, job_id, client_ip):
    """Download a playlist in background"""
    try:
        with jobs_lock:
            download_jobs[job_id]["status"] = "processing"
            download_jobs[job_id]["progress"] = 0

        # Normalize watch?v=...&list=... to a playlist URL so yt-dlp
        # extracts every entry instead of a single video.
        playlist_url = normalize_playlist_url(url)

        # Get playlist info
        info = _extract_info_with_cookie_retry(playlist_url, get_info_opts)

        if not info or "entries" not in info:
            with jobs_lock:
                download_jobs[job_id]["status"] = "failed"
                download_jobs[job_id]["error"] = "Invalid playlist"
            return

        total_videos = len(info["entries"])
        if total_videos > MAX_PLAYLIST_SIZE:
            with jobs_lock:
                download_jobs[job_id]["status"] = "failed"
                download_jobs[job_id][
                    "error"
                ] = f"Playlist too large. Maximum {MAX_PLAYLIST_SIZE} videos"
            return

        playlist_title = sanitize_filename(info.get("title", "playlist"))
        downloaded_files = []
        used_names = set()

        for i, entry in enumerate(info["entries"]):
            video_url = entry.get("url") or entry.get("webpage_url")
            if not video_url:
                continue

            with jobs_lock:
                download_jobs[job_id]["progress"] = (i / total_videos) * 100
                download_jobs[job_id]["current_video"] = i + 1
                download_jobs[job_id]["total_videos"] = total_videos

            try:
                with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temp_dir:
                    temp_path = Path(temp_dir)
                    output_path = temp_path / "%(title)s.%(ext)s"
                    progress_hook = make_progress_hook(
                        job_id, video_index=i, total_videos=total_videos
                    )

                    def make_video_opts(use_cookies):
                        opts = get_ydl_opts(
                            output_path,
                            progress_hook=progress_hook,
                            use_cookies=use_cookies,
                        )
                        opts["noplaylist"] = True
                        return opts

                    video_info = _extract_info_with_cookie_retry(
                        video_url,
                        make_video_opts,
                        download=True,
                    )

                    if video_info:
                        title = sanitize_filename(
                            video_info.get("title", f"video_{i}")
                        )
                        result_path = find_mp3(temp_path, title)
                        if result_path:
                            duration_error = validate_media_duration(
                                result_path, video_info.get("duration")
                            )
                            if duration_error:
                                result_path.unlink(missing_ok=True)
                                raise ValueError(duration_error)

                            final_path = (
                                DOWNLOAD_FOLDER
                                / f"{uuid.uuid4().hex}_{title}.mp3"
                            )
                            shutil.move(str(result_path), str(final_path))
                            downloaded_files.append(
                                {
                                    "title": title,
                                    "filename": unique_zip_name(title, used_names),
                                    "file": str(final_path),
                                }
                            )
            except Exception as e:
                logger.error(f"Error downloading video {i}: {str(e)}")
                continue

        if not downloaded_files:
            with jobs_lock:
                download_jobs[job_id]["status"] = "failed"
                download_jobs[job_id]["error"] = "No videos could be downloaded"
            return

        # Create zip if multiple files
        if len(downloaded_files) > 1:
            zip_path = DOWNLOAD_FOLDER / f"{uuid.uuid4().hex}_{playlist_title}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for file_info in downloaded_files:
                    zip_file.write(file_info["file"], file_info["filename"])
                    os.unlink(file_info["file"])  # Clean up individual files

            with jobs_lock:
                download_jobs[job_id]["status"] = "completed"
                download_jobs[job_id]["progress"] = 100
                download_jobs[job_id]["file"] = str(zip_path)
                download_jobs[job_id]["filename"] = f"{playlist_title}.zip"
        else:
            file_info = downloaded_files[0]
            with jobs_lock:
                download_jobs[job_id]["status"] = "completed"
                download_jobs[job_id]["progress"] = 100
                download_jobs[job_id]["file"] = file_info["file"]
                download_jobs[job_id]["filename"] = file_info["filename"]

        record_download(client_ip)

    except Exception as e:
        logger.exception(f"Playlist download error for job {job_id}: {str(e)}")
        with jobs_lock:
            download_jobs[job_id]["status"] = "failed"
            download_jobs[job_id]["error"] = (
                _friendly_ytdlp_error(e) or "Download failed"
            )


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/robots.txt")
def robots_txt():
    """Opt out of search-engine indexing."""
    return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}


@app.route("/api/info", methods=["POST"])
@limiter.limit("30 per minute")
def get_video_info():
    data = request.json
    url = data.get("url")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    is_valid, error_message = validate_youtube_url(url)
    if not is_valid:
        return jsonify({"error": error_message}), 400

    try:
        info = _extract_info_with_cookie_retry(
            normalize_playlist_url(url),
            get_info_opts,
        )

        if info is None:
            return jsonify({"error": "Could not fetch video information"}), 500

        if info.get("is_live"):
            return (
                jsonify(
                    {
                        "error": (
                            "This video is currently live. Wait for the stream to "
                            "end before downloading so the full broadcast is available."
                        )
                    }
                ),
                400,
            )

        if "entries" in info and info.get("_type") == "playlist":
            total_videos = len(info["entries"])
            is_limited = total_videos > MAX_PLAYLIST_SIZE

            videos = []
            for entry in info["entries"][:10]:
                if entry:
                    videos.append(
                        {
                            "title": entry.get("title", "Unknown"),
                            "duration": entry.get("duration", 0),
                        }
                    )

            return jsonify(
                {
                    "type": "playlist",
                    "title": info.get("title", "Unknown Playlist"),
                    "count": total_videos,
                    "is_limited": is_limited,
                    "max_allowed": MAX_PLAYLIST_SIZE,
                    "videos": videos,
                }
            )
        else:
            return jsonify(
                {
                    "type": "video",
                    "title": info.get("title", "Unknown"),
                    "duration": info.get("duration", 0),
                    "thumbnail": info.get("thumbnail", ""),
                    "uploader": info.get("uploader", "Unknown"),
                }
            )

    except Exception as e:
        logger.exception(f"Info error [{type(e).__name__}]: {str(e)}")
        body = {
            "error": _friendly_ytdlp_error(e) or "Could not fetch video information"
        }
        if DEBUG_INFO:
            body["exception"] = type(e).__name__
            body["detail"] = (str(e) or "")[:500]
        return jsonify(body), 500


@app.route("/api/version", methods=["GET"])
def version_info():
    """Self-report image build marker and baked-in dependency versions."""
    import sys

    try:
        ytdlp_version = yt_dlp.version.__version__
    except AttributeError:
        ytdlp_version = getattr(yt_dlp, "__version__", "unknown")

    return jsonify(
        {
            "yt_dlp_version": ytdlp_version,
            "python_version": sys.version.split()[0],
            "image_build_id": os.environ.get("IMAGE_BUILD_ID", "unset"),
        }
    )


@app.route("/api/health", methods=["GET"])
def health_check():
    """Report whether YouTube extraction currently works or is blocked.

    Probes a canary video through the normal extraction path and caches the
    result for HEALTH_CHECK_TTL_SECONDS so repeated polls don't hammer YouTube.
    """
    now = time.time()
    with health_cache_lock:
        checked_at = health_cache["checked_at"]
        if checked_at is not None and (now - checked_at) < HEALTH_CHECK_TTL_SECONDS:
            resp = dict(health_cache["result"])
            resp["cached"] = True
            return jsonify(resp)

    result = _run_health_check()

    with health_cache_lock:
        health_cache["checked_at"] = time.time()
        health_cache["result"] = result

    resp = dict(result)
    resp["cached"] = False
    return jsonify(resp)


@app.route("/api/download", methods=["POST"])
@limiter.limit("10 per minute")
def download_video():
    data = request.json
    url = data.get("url")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    is_valid, error_message = validate_youtube_url(url)
    if not is_valid:
        return jsonify({"error": error_message}), 400

    # Refuse downloads of broadcasts that are still live: only the portion
    # already aired would be downloadable, which the duration check rejects.
    try:
        info = _extract_info_with_cookie_retry(
            normalize_playlist_url(url),
            get_info_opts,
        )
        if info and info.get("is_live"):
            return (
                jsonify(
                    {
                        "error": (
                            "This video is currently live. Wait for the stream to "
                            "end before downloading so the full broadcast is available."
                        )
                    }
                ),
                400,
            )
    except Exception as e:
        logger.warning(f"Could not check live status for {url}: {e}")

    client_ip = get_client_ip()

    # Check rate limit
    allowed, message = check_rate_limit(client_ip)
    if not allowed:
        return jsonify({"error": message}), 429

    # Clean old jobs periodically
    clean_old_downloads()

    # Create job
    job_id = str(uuid.uuid4())
    with jobs_lock:
        download_jobs[job_id] = {
            "status": "queued",
            "progress": 0,
            "created": datetime.now(),
            "ip": client_ip,
            "url": url,
        }

    # Route to the right job based on the URL: a 'list' query parameter
    # indicates a playlist (including watch?v=...&list=... URLs).
    try:
        if is_playlist_url(url):
            download_executor.submit(download_playlist, url, job_id, client_ip)
        else:
            download_executor.submit(download_single_video, url, job_id, client_ip)

    except Exception as e:
        logger.error(f"Error submitting download: {str(e)}")
        with jobs_lock:
            del download_jobs[job_id]
        return jsonify({"error": "Could not process request"}), 500

    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/api/download/<job_id>", methods=["GET"])
def get_download_status(job_id):
    """Get download status"""
    with jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        response = {
            "status": job["status"],
            "progress": job.get("progress", 0),
        }

        if job["status"] == "completed":
            response["download_url"] = f"/api/download/{job_id}/file"
            response["filename"] = job.get("filename", "download")
        elif job["status"] == "failed":
            response["error"] = job.get("error", "Download failed")
        elif job["status"] == "processing":
            if "current_video" in job:
                response["current_video"] = job["current_video"]
                response["total_videos"] = job["total_videos"]

        return jsonify(response)


@app.route("/api/download/<job_id>/file", methods=["GET"])
def get_download_file(job_id):
    """Get the downloaded file"""
    client_ip = get_client_ip()
    with jobs_lock:
        job = download_jobs.get(job_id)
        if not job or job["status"] != "completed":
            return jsonify({"error": "Download not ready"}), 404

        # Only the IP that created the job may download the file.
        if job.get("ip") != client_ip:
            return jsonify({"error": "Forbidden"}), 403

        file_path = job.get("file")
        filename = job.get("filename", "download")

    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404

    return send_file(file_path, as_attachment=True, download_name=filename)


@app.route("/api/stats")
def get_stats():
    """Get rate limit and usage statistics"""
    client_ip = get_client_ip()
    with history_lock:
        current_time = datetime.now()
        hourly_downloads = len(
            [
                timestamp
                for timestamp in download_history.get(client_ip, [])
                if current_time - timestamp < timedelta(hours=1)
            ]
        )
        daily_downloads = len(
            [
                timestamp
                for timestamp in download_history.get(client_ip, [])
                if current_time - timestamp < timedelta(days=1)
            ]
        )

    return jsonify(
        {
            "hourly_downloads": hourly_downloads,
            "hourly_limit": MAX_DOWNLOADS_PER_HOUR,
            "daily_downloads": daily_downloads,
            "daily_limit": MAX_DOWNLOADS_PER_DAY,
            "remaining_hourly": max(0, MAX_DOWNLOADS_PER_HOUR - hourly_downloads),
            "remaining_daily": max(0, MAX_DOWNLOADS_PER_DAY - daily_downloads),
            "max_playlist_size": MAX_PLAYLIST_SIZE,
            "active_jobs": len(download_jobs),
            "rate_limiting_enabled": RATE_LIMIT_ENABLED,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
