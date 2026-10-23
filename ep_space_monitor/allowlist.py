"""Hard network allowlist enforcement.

Every outgoing request must target a host explicitly listed in the
``[network] allowlist`` section of the configuration. This is a safety
belt for future phases: new source modules cannot accidentally reach
hosts outside the approved set.
"""

from __future__ import annotations

from urllib.parse import urlsplit


class AllowlistError(RuntimeError):
    """Raised when a URL targets a host outside the allowlist."""


def host_of(url: str) -> str:
    """Return the lowercased host part of a URL, or an empty string."""
    return (urlsplit(url).hostname or "").lower()


def assert_allowed(url: str, allowlist: tuple[str, ...] | list[str]) -> str:
    """Validate a URL against the allowlist; return its host.

    Raises AllowlistError on any non-HTTP(S) scheme or non-listed host.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise AllowlistError(f"Blocked: scheme {parts.scheme!r} is not allowed for {url!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise AllowlistError(f"Blocked: URL has no host: {url!r}")
    allowed = {h.lower() for h in allowlist}
    if host not in allowed:
        raise AllowlistError(f"Blocked: host {host!r} is not in the network allowlist")
    return host
