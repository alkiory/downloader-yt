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

> **Local versus cloud deployments:** YouTube can work perfectly when the app runs
> locally because requests come from a normal residential IP, which YouTube may
> treat differently from a cloud datacenter IP. The same code may be blocked on
> Render or another cloud host because its datacenter IP is challenged as
> automated traffic. A local success therefore does not guarantee that anonymous
> YouTube extraction will work online.

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
- `MAX_FILE_SIZE_MB`: Maximum source file size in MB (default: 50)
- `MAX_CONCURRENT_DOWNLOADS`: Concurrent download workers (default: 3)
- `DOWNLOAD_TIMEOUT`: Download timeout in seconds (default: 300)
- `BITRATE`: MP3 bitrate in kbps (default: 192)
- `PORT`: Application port (default: 5000)
- `DOWNLOAD_DIR`: Directory for finished downloads
- `RATE_LIMIT_ENABLED`: Enable rate limiting (`true`/`false`, default: `false` for local runs and local Docker; set `true` explicitly on online deployments like Render)
- `DEFAULT_RATE_LIMIT`: Default limit for endpoints without an explicit one, e.g. job status polling (default: `60 per minute, 3600 per hour`)

## Tests

Run the test suite from the `backend/` directory:

```bash
cd backend
python3 -m unittest discover -s tests -v
```

Tests cover URL validation, playlist routing, rate limiting, download option safety, media processing, and download endpoints.

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

Audio downloads are capped at `MAX_FILE_SIZE_MB` (default 50 MB). Increase this
limit if you need longer recordings.

Long downloads are tracked by polling `/api/download/<job_id>` once per second.
That endpoint falls under the default `DEFAULT_RATE_LIMIT` rather than the
stricter per-endpoint limits, so polling a long download is not throttled. The
page also honors `Retry-After` on 429 responses and resumes polling instead of
failing.

Rate limiting is **off by default**, including when running locally with Docker
Compose, so downloads are never throttled during development. Online deployments
must opt in explicitly: `render.yaml` sets `RATE_LIMIT_ENABLED=true` for the
deployed service only. `python app.py` also skips limits unless the environment
variable is set.

> **YouTube bot check on cloud hosts:** datacenter IPs (Render, AWS, etc.) may be
> asked to *"Sign in to confirm you're not a bot."* This is an upstream YouTube
> restriction, not a Flask error, and it cannot be worked around from the
> application. Keep yt-dlp current — YouTube changes frequently.

## Security Features

This application implements multiple security measures:

### SSRF Protection
- Domain allowlist (YouTube domains only)
- Comprehensive IP validation (blocks private, loopback, link-local, reserved IPs)
- IPv4 and IPv6 support
- Documentation and CGNAT range blocking

### Rate Limiting
- Configurable hourly and daily limits per IP
- Flask-Limiter integration
- Redis support for distributed rate limiting

### Input Validation
- URL validation before processing
- Filename sanitization to prevent path traversal
- Max file size limits

### XSS Prevention
- DOM-based rendering (no innerHTML)
- textContent for all dynamic content
- No raw HTML injection

### Container Security
- Non-root user in Docker
- Capability dropping (cap_drop: ALL)
- no-new-privileges flag
- Minimal base image

### Error Handling
- Generic error messages (no internal details leaked)
- Proper logging
- No stack traces exposed to clients

## Legal Notice

This tool is for personal use only. Downloading videos or audio from YouTube may violate YouTube's Terms of Service. You are solely responsible for ensuring you have the rights to any content you download and for complying with all applicable laws. Do not download or distribute copyrighted material without permission.

## License

MIT License
