from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
from url_validator import validate_youtube_url, is_playlist_url, normalize_playlist_url
from rate_limiter import get_client_ip, create_limiter
import yt_dlp
import os
import uuid
import zipfile
import threading
from pathlib import Path
import re
import subprocess  # nosec B404 - used with argument lists, no shell
import tempfile
import shutil
import time
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Restrict CORS to same-origin only
CORS(app, resources={r"/api/*": {"origins": []}})

# Initialize rate limiter
limiter = create_limiter(app)

# Configuration from environment variables
# Use tempfile.gettempdir() instead of hardcoded /tmp
DOWNLOAD_FOLDER = Path(
    os.environ.get(
        "DOWNLOAD_DIR", str(Path(tempfile.gettempdir()) / "youtube_downloader")
    )
)
DOWNLOAD_FOLDER.mkdir(exist_ok=True, mode=0o700)

# Temporary directory for downloads
TEMP_DIR = Path(tempfile.gettempdir()) / "youtube_downloader"
TEMP_DIR.mkdir(exist_ok=True, mode=0o700)

# Limits
MAX_PLAYLIST_SIZE = int(os.environ.get("MAX_PLAYLIST_SIZE", "50"))
MAX_DOWNLOADS_PER_HOUR = int(os.environ.get("MAX_DOWNLOADS_PER_HOUR", "10"))
MAX_DOWNLOADS_PER_DAY = int(os.environ.get("MAX_DOWNLOADS_PER_DAY", "50"))
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "300"))
PORT = int(os.environ.get("PORT", "5000"))
BITRATE = os.environ.get("BITRATE", "192")
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "3600"))
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "false").lower() == "true"

# Thread pool for downloads
download_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)

# Store download jobs with TTL
download_jobs = {}
jobs_lock = threading.Lock()

# Store download history for rate limiting
download_history = defaultdict(list)
history_lock = threading.Lock()


def sanitize_filename(filename):
    """Remove invalid characters from filename"""
    filename = re.sub(r'[<>:"/\\|?*]', "", filename)
    filename = filename.strip(". ")
    if len(filename) > 200:
        filename = filename[:200]
    return filename


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


def get_ydl_opts(output_path=None):
    """Get yt-dlp options with proper format selection"""
    return {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": BITRATE,
            },
            {
                "key": "EmbedThumbnail",
            },
            {
                "key": "FFmpegMetadata",
            },
        ],
        "writethumbnail": True,
        "outtmpl": (
            str(output_path)
            if output_path
            else str(DOWNLOAD_FOLDER / "%(title)s.%(ext)s")
        ),
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "force_generic_extractor": False,
        "ignoreerrors": True,
        "no_color": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "skip_unavailable_fragments": True,
        "keepvideo": False,
        "noplaylist": False,
        "concurrent_fragment_downloads": 4,
        "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "skip": ["hls", "dash", "translated_subs"],
            }
        },
    }


def get_info_opts():
    """Get yt-dlp options for info extraction"""
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "force_generic_extractor": False,
        "ignoreerrors": True,
        "no_color": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "skip": ["hls", "dash", "translated_subs"],
            }
        },
    }


def get_media_duration(media_path):
    """Get media duration using ffprobe"""
    try:
        result = subprocess.run(  # nosec B603 - no shell, validated input
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
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception as e:
        logger.error(f"Error getting duration: {str(e)}")
    return None


def convert_to_mp3(input_file, output_file):
    """Convert video/audio file to MP3 using ffmpeg"""
    try:
        result = subprocess.run(  # nosec B603 - no shell, validated input
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


def find_mp3(directory):
    """Find MP3 file in directory, skipping thumbnails and other files"""
    mp3_files = list(directory.glob("*.mp3"))
    if mp3_files:
        return mp3_files[0]
    return None


def download_single_video(url, job_id, client_ip):
    """Download a single video in background"""
    try:
        with jobs_lock:
            download_jobs[job_id]["status"] = "processing"
            download_jobs[job_id]["progress"] = 0

        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temp_dir:
            temp_path = Path(temp_dir)
            ydl_opts = get_ydl_opts(temp_path / "%(title)s.%(ext)s")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                video_info = ydl.extract_info(url, download=True)

                if video_info:
                    title = sanitize_filename(video_info.get("title", "video"))
                    mp3_file = find_mp3(temp_path)

                    if mp3_file:
                        # Move to download folder
                        final_file = DOWNLOAD_FOLDER / f"{uuid.uuid4().hex}_{title}.mp3"
                        shutil.move(str(mp3_file), str(final_file))

                        with jobs_lock:
                            download_jobs[job_id]["status"] = "completed"
                            download_jobs[job_id]["file"] = str(final_file)
                            download_jobs[job_id]["filename"] = f"{title}.mp3"

                        record_download(client_ip)
                        return

        with jobs_lock:
            download_jobs[job_id]["status"] = "failed"
            download_jobs[job_id]["error"] = "Failed to download video"

    except Exception as e:
        logger.error(f"Download error for job {job_id}: {str(e)}")
        with jobs_lock:
            download_jobs[job_id]["status"] = "failed"
            download_jobs[job_id]["error"] = "Download failed"


def download_playlist(url, job_id, client_ip):
    """Download a playlist in background"""
    try:
        with jobs_lock:
            download_jobs[job_id]["status"] = "processing"
            download_jobs[job_id]["progress"] = 0

        # Get playlist info
        with yt_dlp.YoutubeDL(get_info_opts()) as ydl:
            info = ydl.extract_info(url, download=False)

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
                        ydl_opts = get_ydl_opts(temp_path / "%(title)s.%(ext)s")
                        ydl_opts["noplaylist"] = True

                        with yt_dlp.YoutubeDL(ydl_opts) as ydl_video:
                            video_info = ydl_video.extract_info(
                                video_url, download=True
                            )

                            if video_info:
                                title = sanitize_filename(
                                    video_info.get("title", f"video_{i}")
                                )
                                mp3_file = find_mp3(temp_path)

                                if mp3_file:
                                    final_path = (
                                        DOWNLOAD_FOLDER
                                        / f"{uuid.uuid4().hex}_{title}.mp3"
                                    )
                                    shutil.move(str(mp3_file), str(final_path))
                                    downloaded_files.append(
                                        {"title": title, "file": str(final_path)}
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
                        zip_entry_name = os.path.basename(file_info["file"])
                        if "_" in zip_entry_name:
                            zip_entry_name = zip_entry_name.split("_", 1)[1]
                        zip_file.write(file_info["file"], zip_entry_name)
                        os.unlink(file_info["file"])

                with jobs_lock:
                    download_jobs[job_id]["status"] = "completed"
                    download_jobs[job_id]["file"] = str(zip_path)
                    download_jobs[job_id]["filename"] = f"{playlist_title}.zip"
            else:
                file_info = downloaded_files[0]
                with jobs_lock:
                    download_jobs[job_id]["status"] = "completed"
                    download_jobs[job_id]["file"] = file_info["file"]
                    download_jobs[job_id]["filename"] = f"{file_info['title']}.mp3"

            record_download(client_ip)

    except Exception as e:
        logger.error(f"Playlist download error for job {job_id}: {str(e)}")
        with jobs_lock:
            download_jobs[job_id]["status"] = "failed"
            download_jobs[job_id]["error"] = "Download failed"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
@limiter.limit("30 per minute", enabled=RATE_LIMIT_ENABLED)
def get_video_info():
    data = request.json
    url = data.get("url")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    is_valid, error_message = validate_youtube_url(url)
    if not is_valid:
        return jsonify({"error": error_message}), 400

    try:
        with yt_dlp.YoutubeDL(get_info_opts()) as ydl:
            info = ydl.extract_info(url, download=False)

            if info is None:
                return jsonify({"error": "Could not fetch video information"}), 500

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
        logger.error(f"Info error: {str(e)}")
        return jsonify({"error": "Could not fetch video information"}), 500


@app.route("/api/download", methods=["POST"])
@limiter.limit("10 per minute", enabled=RATE_LIMIT_ENABLED)
def download_video():
    data = request.json
    url = data.get("url")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Normalize URL if it's a playlist
    if is_playlist_url(url):
        url = normalize_playlist_url(url)

    is_valid, error_message = validate_youtube_url(url)
    if not is_valid:
        return jsonify({"error": error_message}), 400

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

    # Determine if playlist and submit to executor
    try:
        with yt_dlp.YoutubeDL(get_info_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            is_playlist = "entries" in info and info.get("_type") == "playlist"

            if is_playlist:
                if len(info["entries"]) > MAX_PLAYLIST_SIZE:
                    with jobs_lock:
                        del download_jobs[job_id]
                    return (
                        jsonify(
                            {
                                "error": f"Playlist too large. Maximum {MAX_PLAYLIST_SIZE} videos"
                            }
                        ),
                        400,
                    )
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
    with jobs_lock:
        job = download_jobs.get(job_id)
        if not job or job["status"] != "completed":
            return jsonify({"error": "Download not ready"}), 404

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
            "rate_limit_enabled": RATE_LIMIT_ENABLED,
            "hourly_downloads": hourly_downloads,
            "hourly_limit": MAX_DOWNLOADS_PER_HOUR,
            "daily_downloads": daily_downloads,
            "daily_limit": MAX_DOWNLOADS_PER_DAY,
            "remaining_hourly": max(0, MAX_DOWNLOADS_PER_HOUR - hourly_downloads),
            "remaining_daily": max(0, MAX_DOWNLOADS_PER_DAY - daily_downloads),
            "max_playlist_size": MAX_PLAYLIST_SIZE,
            "active_jobs": len(download_jobs),
        }
    )


if __name__ == "__main__":
    # Bind to all interfaces only in development mode
    # In production, this is handled by gunicorn
    app.run(host="0.0.0.0", port=PORT, debug=False)  # nosec B104 - required for Docker
