import ipaddress
import socket
from urllib.parse import urlparse, parse_qs
import logging

logger = logging.getLogger(__name__)

ALLOWED_DOMAINS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "music.youtube.com",
}


def is_public_ip(ip_str):
    """Return True if the address is globally routable (safe to fetch)."""
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
    ):
        return False

    if ip_obj.version == 4:
        # Block documentation ranges
        if ip_obj in ipaddress.ip_network("192.0.2.0/24"):
            return False
        if ip_obj in ipaddress.ip_network("198.51.100.0/24"):
            return False
        if ip_obj in ipaddress.ip_network("203.0.113.0/24"):
            return False
        # Block CGNAT range
        if ip_obj in ipaddress.ip_network("100.64.0.0/10"):
            return False

    return True


def validate_youtube_url(url):
    """Validate YouTube URL with comprehensive SSRF protection"""
    try:
        parsed = urlparse(url)

        # Check scheme
        if parsed.scheme not in ("http", "https"):
            return False, "Only HTTP and HTTPS URLs are allowed"

        # Check domain
        domain = parsed.netloc.lower()
        if domain not in ALLOWED_DOMAINS:
            return False, "Only YouTube URLs are allowed"

        # Comprehensive IP validation
        hostname = parsed.hostname
        if not hostname:
            return False, "Invalid URL"

        # Get ALL IP addresses (both IPv4 and IPv6)
        try:
            ip_addresses = set()

            # Get IPv4 addresses
            try:
                ipv4_info = socket.getaddrinfo(hostname, None, socket.AF_INET)
                ip_addresses.update(info[4][0] for info in ipv4_info)
            except socket.gaierror:
                pass

            # Get IPv6 addresses
            try:
                ipv6_info = socket.getaddrinfo(hostname, None, socket.AF_INET6)
                ip_addresses.update(info[4][0] for info in ipv6_info)
            except socket.gaierror:
                pass

            if not ip_addresses:
                return False, "Could not resolve URL"

            # Check ALL resolved IPs
            for ip_str in ip_addresses:
                if not is_public_ip(ip_str):
                    return False, "Invalid YouTube URL"

            return True, ""

        except socket.gaierror:
            return False, "Could not resolve URL"
        except Exception as e:
            logger.error(f"IP validation error: {str(e)}")
            return False, "Invalid URL"

    except Exception as e:
        logger.error(f"URL validation error: {str(e)}")
        return False, "Invalid URL"


def get_playlist_id(url):
    """Extract the YouTube playlist id from a URL's 'list' query parameter."""
    try:
        query = parse_qs(urlparse(url).query)
        return (query.get("list") or [None])[0]
    except Exception:
        return None


def is_playlist_url(url):
    """Whether a URL points to a playlist (has a 'list' query parameter)."""
    return get_playlist_id(url) is not None


def normalize_playlist_url(url):
    """Convert any playlist URL (e.g. watch?v=...&list=...) to the canonical
    playlist URL so yt-dlp extracts every entry instead of a single video."""
    playlist_id = get_playlist_id(url)
    if playlist_id:
        return f"https://www.youtube.com/playlist?list={playlist_id}"
    return url


_SSRF_GUARD_INSTALLED = False


def install_ssrf_guard():
    """Patch socket.getaddrinfo so YouTube-domain lookups are validated.

    yt-dlp re-resolves the URL host at fetch time, after validate_youtube_url
    has already run. An attacker controlling DNS could rebind a YouTube domain
    to a private IP between those two resolutions. This guard validates the
    resolution yt-dlp actually uses, closing that window.

    Only allow-listed YouTube hostnames are checked; IP literals (e.g. the
    server's own "0.0.0.0" bind) and unrelated hosts (CDN, Redis, ...) are
    passed through unchanged.
    """
    global _SSRF_GUARD_INSTALLED
    if _SSRF_GUARD_INSTALLED:
        return

    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        results = real_getaddrinfo(host, port, family, type, proto, flags)
        if isinstance(host, str) and host.lower().rstrip(".") in ALLOWED_DOMAINS:
            for result in results:
                if not is_public_ip(result[4][0]):
                    raise socket.gaierror(f"blocked non-public address for {host}")
        return results

    socket.getaddrinfo = guarded_getaddrinfo
    _SSRF_GUARD_INSTALLED = True
