"""URL validation + SSRF protection for outbound provider calls.

Every provider base URL is checked before use: scheme allowlist + hostname
resolution rejecting private/loopback/link-local ranges (unless the operator
explicitly allows private networks for local development).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.config import settings

_SCHEMES = ("http", "https")
_PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
]


class UnsafeUrlError(ValueError):
    """Raised when a URL fails scheme or SSRF validation."""


def normalize_base_url(url: str) -> str:
    """Prepend ``https://`` to scheme-less base URLs (``api.example.com/v1`` → ``https://…``).

    Keeps already-schemed URLs untouched so explicit ``http://`` (e.g. local
    Qdrant) survives. Empty input stays empty — callers fall back to their
    provider default.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    return url


def validate_url(url: str, *, allow_private: bool | None = None) -> str:
    """Return the normalized URL when safe; raise UnsafeUrlError otherwise."""
    if not url or not url.strip():
        raise UnsafeUrlError("URL is empty")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in _SCHEMES:
        raise UnsafeUrlError(f"unsupported URL scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise UnsafeUrlError("URL has no hostname")

    allow_private = settings.allow_private_networks if allow_private is None else allow_private
    if allow_private:
        return url

    # Resolve the hostname and reject private/loopback/link-local addresses.
    try:
        infos = socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"cannot resolve host: {parsed.hostname}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise UnsafeUrlError(
                f"refusing connection to non-public address {ip} (host {parsed.hostname}). "
                "Set ALLOW_PRIVATE_NETWORKS=1 to enable for local development."
            )
        if any(ip in network for network in _PRIVATE_RANGES):
            raise UnsafeUrlError(f"refusing connection to {ip} (host {parsed.hostname})")
    return url
