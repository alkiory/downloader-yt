from flask import request
from flask_limiter import Limiter
import os
import logging

logger = logging.getLogger(__name__)

# Configuration
TRUSTED_PROXIES = (
    set(os.environ.get("TRUSTED_PROXIES", "").split(","))
    if os.environ.get("TRUSTED_PROXIES")
    else set()
)
USE_X_FORWARDED_FOR = (
    os.environ.get("USE_X_FORWARDED_FOR", "true").lower() == "true"
)  # Enable for Render
# Local runs (`python app.py` and `docker compose up`) must not throttle
# development or long downloads. Only online deployments that set this
# explicitly (see render.yaml) enable rate limiting.
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "false").lower() == "true"


def get_client_ip():
    """Get the client IP, trusting only the reverse proxy directly in front of us.

    X-Forwarded-For is appended to by each hop, so the *leftmost* entry is
    client-supplied and spoofable; the *rightmost* entry is the IP the proxy
    in front of us actually saw. On Render there is exactly one trusted proxy,
    so we take the rightmost entry. When TRUSTED_PROXIES is set we additionally
    require the immediate peer (request.remote_addr) to be one of them.
    """
    if USE_X_FORWARDED_FOR:
        x_forwarded_for = request.headers.get("X-Forwarded-For")
        if x_forwarded_for:
            if TRUSTED_PROXIES:
                if request.remote_addr in TRUSTED_PROXIES:
                    return x_forwarded_for.split(",")[-1].strip()
            else:
                return x_forwarded_for.split(",")[-1].strip()

    return request.remote_addr or "unknown"


def create_limiter(app):
    """Create rate limiter with proper configuration"""
    storage_type = os.environ.get("RATE_LIMIT_STORAGE", "memory")

    if storage_type == "redis":
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")

        # Preserve the scheme (redis:// or rediss://)
        if redis_url.startswith("rediss://"):
            storage_uri = redis_url
        elif redis_url.startswith("redis://"):
            storage_uri = redis_url
        else:
            storage_uri = f"redis://{redis_url}"
    else:
        storage_uri = "memory://"

    return Limiter(
        app=app,
        key_func=get_client_ip,
        # Applies to everything without an explicit @limiter.limit, e.g. job
        # status polling (/api/download/<job_id>) during a long download. 60
        # per minute is 1 poll/second while staying bounded. /api/info and
        # /api/download keep their own stricter limits.
        default_limits=[
            os.environ.get("DEFAULT_RATE_LIMIT", "60 per minute, 3600 per hour")
        ],
        storage_uri=storage_uri,
        headers_enabled=True,
        enabled=RATE_LIMIT_ENABLED,
    )
