# YouTube Audio Downloader

A web application to download YouTube videos as MP3 audio files for offline listening.

## Features

- 🎵 Download YouTube videos as high-quality MP3 (with embedded cover art)
- 📋 Playlist support (up to 50 videos)
- 📱 Responsive design for all devices
- 🚀 Fast downloads using yt-dlp
- 🐳 Easy deployment with Docker
- 🔒 Security features: rate limiting, SSRF protection, XSS prevention

## Quick Start

### Using Docker (Recommended)

```bash
docker-compose up -d
```

Then open <http://localhost:5000>.

### Manual Installation

Install Python 3.11+ and FFmpeg, then:

```bash
cd backend
pip install -r requirements.txt
python app.py
```

## Configuration

Copy `.env.example` to `.env` and adjust as needed:

- `MAX_PLAYLIST_SIZE`: Maximum videos per playlist (default: 50)
- `MAX_DOWNLOADS_PER_HOUR`: Hourly download limit per IP (default: 10)
- `MAX_DOWNLOADS_PER_DAY`: Daily download limit per IP (default: 50)
- `MAX_FILE_SIZE_MB`: Maximum file size (default: 50)
- `MAX_CONCURRENT_DOWNLOADS`: Concurrent download workers (default: 3)
- `DOWNLOAD_TIMEOUT`: Download timeout in seconds (default: 300)
- `BITRATE`: MP3 bitrate in kbps (default: 192)
- `PORT`: Application port (default: 5000)
- `DOWNLOAD_DIR`: Directory for finished downloads
- `RATE_LIMIT_ENABLED`: Enable rate limiting (`true`/`false`, default: `false` — set `true` when running online)

## Tests

Run the test suite from the `backend/` directory:

```bash
cd backend
python3 -m unittest discover -s tests -v
```

Tests cover URL validation, playlist routing, and rate limiting.

## Deploy

```bash
# Clean up
docker-compose down

# Rebuild
docker-compose build --no-cache

# Start
docker-compose up -d

# Check logs
docker-compose logs -f
```

## Legal Notice

This tool is for personal use only. Downloading videos or audio from YouTube may violate YouTube's Terms of Service. You are solely responsible for ensuring you have the rights to any content you download and for complying with all applicable laws. Do not download or distribute copyrighted material without permission.

## License

MIT License
