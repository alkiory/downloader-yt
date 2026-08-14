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
USE_X_FORWARDED_FOR = os.environ.get("USE_X_FORWARDED_FOR", "false").lower() == "true"
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "false").lower() == "true"


def get_client_ip():
    """Get client IP address safely, respecting trusted proxies"""
    if USE_X_FORWARDED_FOR:
        x_forwarded_for = request.headers.get("X-Forwarded-For")
        if x_forwarded_for:
            if TRUSTED_PROXIES:
                remote_addr = request.remote_addr
                if remote_addr in TRUSTED_PROXIES:
                    return x_forwarded_for.split(",")[0].strip()
            else:
                return x_forwarded_for.split(",")[0].strip()

    return request.remote_addr or "unknown"


def create_limiter(app):
    """Create rate limiter with proper configuration"""
    storage_type = os.environ.get("RATE_LIMIT_STORAGE", "memory")

    if storage_type == "redis":
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        # Remove any existing prefix to avoid double-prefix
        redis_url = redis_url.replace("redis://", "", 1).replace("rediss://", "", 1)
        storage_uri = f"redis://{redis_url}"
    else:
        storage_uri = "memory://"

    return Limiter(
        app=app,
        key_func=get_client_ip,
        default_limits=[
            os.environ.get("DEFAULT_RATE_LIMIT", "100 per day, 30 per hour")
        ],
        storage_uri=storage_uri,
        headers_enabled=True,
        enabled=RATE_LIMIT_ENABLED,
    )
