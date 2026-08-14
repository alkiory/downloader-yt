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
> treat differently from a cloud datacenter IP. A local setup can also use a
> browser session when cookies are configured. The same code may be blocked on
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
- `MAX_FILE_SIZE_MB`: Maximum source file size in MB (default: 250; long podcasts need more than 50 MB)
- `MAX_CONCURRENT_DOWNLOADS`: Concurrent download workers (default: 3)
- `DOWNLOAD_TIMEOUT`: Download timeout in seconds (default: 300)
- `DOWNLOAD_LIVESTREAM_FROM_START`: Start supported YouTube livestream downloads from the beginning (default: `true`)
- `BITRATE`: MP3 bitrate in kbps (default: 192)
- `PORT`: Application port (default: 5000)
- `DOWNLOAD_DIR`: Directory for finished downloads
- `RATE_LIMIT_ENABLED`: Enable rate limiting (`true`/`false`, default: `false` for local runs and local Docker; set `true` explicitly on online deployments like Render)
- `DEFAULT_RATE_LIMIT`: Default limit for endpoints without an explicit one, e.g. job status polling (default: `60 per minute, 3600 per hour`)
- `YOUTUBE_PLAYER_CLIENTS`: YouTube player clients yt-dlp tries, in order (default: `android,ios`)
- `COOKIE_FILE`: Path to a Netscape-format `cookies.txt` from a logged-in browser session (for datacenter hosts)
- `YOUTUBE_COOKIES_B64`: Base64-encoded Netscape `cookies.txt` content (useful as a Render secret environment variable; takes precedence over `COOKIE_FILE`)
- `YOUTUBE_COOKIE_BEHAVIOR`: Cookie policy: `disabled`, `when_needed` (default and recommended), or `all`
- `YOUTUBE_USER_AGENT`: Optional browser User-Agent matching the browser that produced the cookies
- `YOUTUBE_PROXY`: Proxy URL (e.g. `http://user:pass@residential-proxy:port`) to escape datacenter-IP flagging
- `HEALTH_CHECK_URL`: Canary video `/api/health` probes to test extraction (default: `jNQXAC9IVRw`)
- `HEALTH_CHECK_TTL_SECONDS`: How long `/api/health` caches its result (default: `60`)

## Tests

Run the test suite from the `backend/` directory:

```bash
cd backend
python3 -m unittest discover -s tests -v
```

Tests cover URL validation, playlist routing, rate limiting, download option safety, and truncated-output detection.

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

`GET /api/health` reports whether YouTube extraction currently works (returns
`status: ok | blocked | error`, plus cookie behavior, cookie availability, and
proxy configuration). It probes a canary video and caches the result for
`HEALTH_CHECK_TTL_SECONDS`.

Long audio downloads are supported up to `MAX_FILE_SIZE_MB`. The default is
250 MB because a 72-minute MP3 at 192 kbps is approximately 104 MB. Downloads
with missing media fragments are rejected instead of being silently
delivered as truncated files, and the completed audio duration is checked
against the source duration. YouTube livestream archives are requested from the
beginning when yt-dlp supports it; this is important for live URLs such as the
reported example. Broadcasts that are still **live** are rejected with a clear
message, because only the already-aired portion exists; retry once the stream
has ended.

Long downloads are tracked by polling `/api/download/<job_id>` once per second.
That endpoint uses the `DEFAULT_RATE_LIMIT` so one long download cannot exhaust
the old 30-per-hour default limit. The page also honors `Retry-After` on 429
responses and resumes polling instead of failing.

Rate limiting is **off by default**, including when running locally with Docker
Compose, so downloads are never throttled during development. Online deployments
must opt in explicitly: `render.yaml` sets `RATE_LIMIT_ENABLED=true` for the
deployed service only. `python app.py` also skips limits unless the environment
variable is set.

> **YouTube bot check on cloud hosts:** datacenter IPs (Render, AWS, etc.) are often
> asked to *"Sign in to confirm you're not a bot."* This is an upstream YouTube
> restriction, not a Flask error, and it cannot be guaranteed away by changing a
> player-client option. The [Pinchflat cookie guidance](https://github.com/kieraneglin/pinchflat/wiki/YouTube-Cookies)
> also warns that cookies can cause IP bans and recommends minimizing cookie use.
> This project therefore defaults to `YOUTUBE_COOKIE_BEHAVIOR=when_needed`: it
> tries anonymously first and retries with cookies only for authentication-like
> errors. For a Render deployment:
>
> 1. Export a fresh Netscape-format `cookies.txt` from a YouTube session. yt-dlp's
>    [cookie guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)
>    recommends an incognito session that is closed after exporting; never commit
>    the file or paste it into application logs.
> 2. Either add `cookies.txt` as a Render Secret File (the blueprint points
>    `COOKIE_FILE` at `/etc/secrets/cookies.txt`) **or** set the secret
>    `YOUTUBE_COOKIES_B64` environment variable to `base64 -w 0 cookies.txt`.
>    The application copies read-only secret files into a private writable temp
>    file before giving them to yt-dlp. Set `YOUTUBE_COOKIE_BEHAVIOR=all` only
>    when every operation must be authenticated; use `disabled` to turn cookie
>    use off entirely.
> 3. If YouTube still blocks the request, set `YOUTUBE_USER_AGENT` to the matching
>    browser User-Agent and route YouTube through a residential proxy with
>    `YOUTUBE_PROXY`. Cookies can be bound to the session/IP that created them,
>    so a cookie exported locally may still fail from Render without matching
>    egress.
> 4. Check `/api/health` after each deployment. It reports whether the canary is
>    `ok`, `blocked`, or `error`, and whether cookie/proxy configuration is
>    available. `/api/version` confirms the deployed yt-dlp build.
>
> Keep yt-dlp current — YouTube changes frequently — and treat cookies as
> credentials for a throwaway account with limited activity. These measures are
> operational requirements for a cloud deployment, not a promise that YouTube
> will permit every video or account.

## Legal Notice

This tool is for personal use only. Downloading videos or audio from YouTube may violate YouTube's Terms of Service. You are solely responsible for ensuring you have the rights to any content you download and for complying with all applicable laws. Do not download or distribute copyrighted material without permission.

## License

MIT License
