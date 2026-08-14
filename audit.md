# Audit — YouTube Audio Downloader

**Date:** 2026-08-14 (updated after fourth review — rate-limit toggle, thumbnail embedding, playlist fix, credit footer)
**Scope:** Full repository (`backend/app.py`, `backend/url_validator.py`, `backend/rate_limiter.py`, `backend/tests/`, `backend/templates/index.html`, `backend/requirements.txt`, `backend/Dockerfile`, `docker-compose.yml`, `README.md`, `.gitignore`, `backend/.dockerignore`, `.env.example`)

---

## 0. Change Review (what changed since the last pass)

**Fixed / improved this round**

- ✅ **Frontend restored (was C1)** — `displayInfo()`, `showMessage()`, and `formatDuration()` are back in `index.html`, built with DOM/`textContent` (no `innerHTML` injection). The download flow works again.
- ✅ **Rate limiting toggled by environment** — `RATE_LIMIT_ENABLED` (default `false`) is the kill switch for both `flask-limiter` (`enabled=...`) and the custom `check_rate_limit`/`record_download`. Docker sets `true`; local `python app.py` skips limits. `/api/stats` reports the state.
- ✅ **Thumbnail embedded into MP3** — `get_ydl_opts()` now writes the thumbnail and runs `EmbedThumbnail` + `FFmpegMetadata`, so each MP3 carries cover art and title/artist metadata. `find_mp3()` correctly skips the downloaded image and returns the `.mp3` (also removed the old "first file wins" bug).
- ✅ **Playlist bug fixed** — `is_playlist_url()` detects the `list` query param so `watch?v=…&list=…` routes to the playlist path, and `normalize_playlist_url()` rewrites it to the canonical `/playlist?list=…` URL so yt-dlp extracts **all** entries instead of one.
- ✅ **Job TTL / pruning** — `JOB_TTL_SECONDS` env (default 3600) added; `clean_old_downloads()` now prunes expired *jobs* in addition to files, and is called on every `/api/download` (so single-video files are cleaned too — was C8/C11).
- ✅ **`PORT`/`DOWNLOAD_TIMEOUT` now honored by gunicorn** — Dockerfile CMD uses `${PORT:-5000}` / `${DOWNLOAD_TIMEOUT:-300}` (was C12).
- ✅ **`geo_bypass` removed** (was C7) — the circumvention flag is gone from yt-dlp opts.
- ✅ **Unused imports removed** — `Response`, `stream_with_context`, `json`, `queue` dropped (was C13).

**Fixed after this review**

- ✅ **Dockerfile renamed** — `backend/dockerfile` → `backend/Dockerfile`; `docker compose build` now succeeds (verified).
- ✅ **`.dockerignore` relocated** — moved to `backend/.dockerignore` (the build context) with context-appropriate rules, so `__pycache__`/local artifacts stay out of the image.
- ✅ **Persistent downloads volume** — `docker-compose.yml` now mounts a named volume at `/app/downloads` and sets `DOWNLOAD_DIR=/app/downloads`, so finished files survive container restarts.
- ✅ **Unit tests added** — `backend/tests/` covers URL validation, playlist routing, and rate limiting (42 tests, all passing).
- ✅ **Playlist helpers moved to `url_validator.py`** — `get_playlist_id`/`is_playlist_url`/`normalize_playlist_url` extracted from `app.py` (pure stdlib, now unit-tested in isolation).
- ✅ **README cleaned up** — malformed code fences, missing headings, and the stray `text` line fixed.
- ✅ **SSRF rebinding closed (C3)** — `install_ssrf_guard()` validates YouTube-domain DNS at fetch time.
- ✅ **File endpoint auth (C6)** — the download file endpoint now checks requester IP against the job owner.
- ✅ **Code nits fixed** — removed unused `import io` (C12), clean zip entry names without UUID (C18), dropped the `[TEMPLATE]` header in `.env.example` (C17).

**New issues introduced (details in §3)**

None.

---

## 1. Overview

A Flask web app that converts YouTube videos/playlists to MP3 using `yt-dlp` + FFmpeg. Downloads run in a background thread pool and are fetched by the client via a job-based status API. Rate limiting, thumbnail embedding, and playlist handling are now configurable/correct.

**Stack:** Python 3.11 · Flask 3 · yt-dlp · FFmpeg · gunicorn · flask-limiter · ThreadPoolExecutor · Docker Compose

---

## 2. Pros (Strengths)

| # | Area | What's good |
|---|------|-------------|
| P1 | Tooling | Uses **yt-dlp** (actively maintained fork) + FFmpeg. |
| P2 | Architecture | **Non-blocking download jobs** — the request thread returns immediately; gunicorn timeout no longer kills long downloads. |
| P3 | Concurrency | `ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)` enforces a real concurrency cap. |
| P4 | Security | SSRF guard (domain allow-list + full A/AAAA IP checks + doc/CGNAT ranges), CORS same-origin, non-root user, `cap_drop: ALL`, `0o700` dirs, `debug=False`. |
| P5 | Security | Rate limiting aligned on one safe client-IP function, storage configurable (memory/Redis), and globally killable via `RATE_LIMIT_ENABLED` (good local-vs-online ergonomics). |
| P6 | Security | `subprocess.run()` with an argument list (no shell injection); generic error messages; no raw exceptions leaked. |
| P7 | UX | Real progress polling, job states, responsive UI, semantic HTML, ARIA labels; cover art + metadata embedded in output files. |
| P8 | Maintainability | Logic split into `url_validator.py` / `rate_limiter.py`; `get_info_opts()`/`get_ydl_opts()` dedupe options; env-driven config; `is_playlist_url()`/`normalize_playlist_url()` encapsulate URL routing. |
| P9 | Hygiene | `sanitize_filename()`, `clean_old_downloads()` (files **and** jobs, with TTL), `tempfile.TemporaryDirectory`, `.gitignore`/`.dockerignore`. |
| P10 | Observability | Structured `logging`; `/api/stats` exposes limits, usage, and whether rate limiting is enabled. |

---

## 3. Cons (Weaknesses)

### 3.1 Functional / Correctness

| # | Severity | Status | Issue | Detail |
|---|----------|--------|-------|--------|
| C1 | ✅ Fixed | Resolved | **Dockerfile filename mismatch** | Renamed `backend/dockerfile` → `backend/Dockerfile`; `docker compose build` now succeeds. |
| C2 | 🟡 Medium | Still open | **Duplicate info extraction** | `/api/info` (client), `/api/download` routing, and the worker each call `extract_info` — up to 3× per download, increasing latency and YouTube rate-limit exposure. |

### 3.2 Security

| # | Severity | Status | Issue | Detail |
|---|----------|--------|-------|--------|
| C3 | ✅ Fixed | Resolved | **SSRF TOCTOU (DNS rebinding)** | `install_ssrf_guard()` patches `socket.getaddrinfo` to validate YouTube-domain lookups at fetch time, so yt-dlp's actual resolution is checked the same way — closing the rebinding window. Only allow-listed hostnames are validated (IP literals and CDN/Redis hosts pass through). |
| C4 | 🟡 Medium | ⚠️ Partial | **Custom limiter still in-memory** | `download_history` is a process-local `defaultdict` — lost on restart and not shared across workers, unlike the configurable `flask-limiter` storage. |
| C5 | 🟢 Low | ⚠️ Partial | **Redis URL handling** | `create_limiter()` strips then re-adds `redis://`. `rediss://` (TLS) gets downgraded to `redis://`; only `redis://` is preserved correctly. |
| C6 | ✅ Fixed | Resolved | **File endpoint has no auth/expiry** | `/api/download/<job_id>/file` now verifies the requester's IP matches the job's owner IP and returns 403 otherwise. |

### 3.3 Reliability & Resource Management

| # | Severity | Status | Issue | Detail |
|---|----------|--------|-------|--------|
| C7 | 🟡 Medium | Still open | **In-memory jobs break when scaled** | `download_jobs` and the executor are per-process; with `--workers 1` it works, but a different worker than the creator can't resolve a job (returns 404). |
| C8 | 🟡 Medium | Still open | **Unbounded executor queue** | `ThreadPoolExecutor.submit()` queues without limit; a burst within rate-limit windows can pile up an unbounded backlog. |
| C9 | 🟢 Low | ⚠️ Partial | **Job pruning is lazy** | `clean_old_downloads()` only runs when a new download is requested, so stale jobs/files linger until the next request. Acceptable, but a periodic sweep would be cleaner. |
| C10 | ✅ Fixed | Resolved | **`/tmp` is ephemeral** | Compose now mounts a named `downloads` volume at `/app/downloads` with `DOWNLOAD_DIR=/app/downloads`, so finished files persist across restarts. |

### 3.4 Maintainability & Code Quality

| # | Severity | Status | Issue | Detail |
|---|----------|--------|-------|--------|
| C11 | ✅ Fixed | Resolved | **`.dockerignore` in wrong location** | Moved to `backend/.dockerignore` with context-appropriate rules, so `__pycache__`/`.env`/local artifacts are excluded from the image. |
| C12 | ✅ Fixed | Resolved | **`import io` unused** | Removed the unused `io` import from `app.py`. |
| C13 | 🟢 Low | ⚠️ Residual | **Redundant ffmpeg fallback** | yt-dlp's `FFmpegExtractAudio` already yields MP3; the manual `convert_to_mp3()` fallback remains, now only reachable via `find_mp3()`'s fallback branch. |
| C14 | ✅ Fixed | Resolved | **No tests** | Added `backend/tests/` with unit tests for URL validation, playlist routing, rate limiting, SSRF, file-endpoint auth, and zip naming (42 tests, run via `python3 -m unittest discover -s tests`). CI still not configured. |
| C15 | 🟢 Low | ⚠️ Partial | **Hardcoded config** | Timeouts/retries/route rate limits remain hardcoded; only a subset is env-driven. |
| C16 | ✅ Fixed | Resolved | **README formatting broken** | Rewrote the quick-start/config/deploy sections with correct fences and headings; removed the stray `text` line.
| C17 | ✅ Fixed | Resolved | **`.env.example` header** | Removed the literal `[TEMPLATE]` line. |
| C18 | ✅ Fixed | Resolved | **Zip entry filenames carry UUID** | Zip entries now use clean `{title}.mp3` names (deduped with a numeric suffix on repeated titles). |

### 3.5 Compliance

| # | Severity | Status | Issue | Detail |
|---|----------|--------|-------|--------|
| C19 | 🟠 High | Still open | **YouTube ToS / copyright** | Downloading/ripping YouTube audio violates YouTube's ToS; public deployment exposes the operator to DMCA/legal risk regardless of the disclaimer. The new in-app disclaimer ("only download videos you own") is good hygiene but not legal protection. |

---

## 4. Summary

This round **closed most of the previously open functional bugs**: the frontend is working again, rate limiting is now correctly environment-gated (online vs. local), thumbnails/metadata are embedded into the output, the `watch?v=…&list=…` playlist case is handled, jobs are TTL-pruned, and `PORT`/timeout are honored by gunicorn. The app is functionally close to what it claims to do.

The Docker build blocker (C1), the misplaced `.dockerignore` (C11), the ephemeral download folder (C10), the missing test coverage (C14), the SSRF rebinding window (C3), and the unauthenticated file endpoint (C6) are all resolved. The remaining risks are in-memory state that won't survive scaling (C4, C7, C8) and the still-open ToS risk (C19).

**Verdict:** Functionally solid and reasonably hardened for a single-instance, personal-use deployment; remaining concerns for public use are scale/state (single-worker) and the ToS risk.

---

## 5. Suggestions (prioritized)

### 🔴 Must fix now

1. ~~Fix the Dockerfile mismatch (C1)~~ — **done**: `backend/Dockerfile` renamed and `docker compose build` verified.

### 🟠 Strongly recommended

2. ~~Move `.dockerignore` into `backend/` (C11) and point `DOWNLOAD_DIR` at a persistent volume (C10)~~ — **done**.
3. ~~Eliminate the SSRF rebinding window (C3)~~ — **done**: fetch-time DNS validation via `install_ssrf_guard()`. Also added IP-ownership checks on the file endpoint (C6).
4. **Persist jobs + rate-limit state (C4, C7, C8).** Move `download_jobs`/`download_history` to shared storage (e.g. Redis), add a periodic cleanup sweep (C9), and cap the executor queue so bursts degrade gracefully.
5. **Revisit compliance (C19).** Keep the disclaimer, document personal-use-only, and consider legal review before any public deployment.

### 🟢 Nice to have

6. ~~Add tests (C14)~~ — **done**: unit tests for URL validation, rate limiting, playlist routing, SSRF, file-endpoint auth, and zip naming added (42 passing). Add lightweight CI as a follow-up.
7. **Reduce duplicate info extraction (C2)** — share one `extract_info` result between preview and download.
8. **Clean code nits** — remove `import io` (C12) and strip UUID from zip entries (C18) are done; dedupe the ffmpeg fallback (C13) and externalize remaining config (C15) remain.
9. ~~Fix docs~~ — **done**: README formatting (C16) and the `.env.example` header (C17) are both fixed.

---

*Generated by Codebuff 🤖*
