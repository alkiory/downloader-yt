FROM python:3.11-slim

# Build-time marker so each rebuilt image exposes its identity at /api/version.
# Pass with `docker build --build-arg IMAGE_BUILD_ID=...`.
ARG IMAGE_BUILD_ID=local
ENV IMAGE_BUILD_ID=${IMAGE_BUILD_ID}

# Optional yt-dlp auth/egress defaults (consumed by app.py: _apply_auth_opts).
# Pass with `docker build --build-arg COOKIE_FILE=...` / `--build-arg YOUTUBE_PROXY=...`,
# or override at runtime via the environment (Render env vars / secrets).
# NOTE: don't bake secrets (proxy credentials) in via --build-arg — they become
# part of the image layers and are visible via `docker history`. Pass those at
# runtime instead.
ARG COOKIE_FILE=
ARG YOUTUBE_PROXY=
ENV COOKIE_FILE=${COOKIE_FILE} \
    YOUTUBE_PROXY=${YOUTUBE_PROXY}

# Install FFmpeg and other dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create downloads directory with proper permissions
RUN mkdir -p downloads && \
    chown -R 1000:1000 /app

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash appuser
USER appuser

EXPOSE 5000

# Use shell form to allow environment variable expansion
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --timeout ${DOWNLOAD_TIMEOUT:-300} --preload app:app"]