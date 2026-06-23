"""
Business logic shared by both the API and the worker.
- apply_check_result: consecutive-failure state machine (TRD §5.3)
- validate_target: SSRF guard (TRD §6.3)
"""
import asyncio
import socket
from ipaddress import ip_address, ip_network
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Monitor


# ---------------------------------------------------------------------------
# Blocked networks — TRD §6.3 (includes fc00::/7 from TRD, absent in PRD draft)
# ---------------------------------------------------------------------------

BLOCKED_NETWORKS = [
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),   # AWS/GCP metadata endpoint range
    ip_network("::1/128"),
    ip_network("fc00::/7"),          # IPv6 unique local
]


class ValidationError(Exception):
    """Raised when URL registration validation fails."""


# ---------------------------------------------------------------------------
# Consecutive-failure state machine (TRD §5.3)
# ---------------------------------------------------------------------------

def apply_check_result(monitor: "Monitor", success: bool) -> str:
    """
    Returns the new current_state for a monitor given one check result.

    Rules (TRD §5.3):
    - success → reset consecutive_failures to 0, return 'up'
    - first failure → increment consecutive_failures, keep current_state
    - second+ consecutive failure → return 'down'

    Mutates monitor.consecutive_failures in-place.
    """
    if success:
        monitor.consecutive_failures = 0
        return "up"
    monitor.consecutive_failures += 1
    return "down" if monitor.consecutive_failures >= 2 else monitor.current_state


# ---------------------------------------------------------------------------
# SSRF guard (TRD §6.3)
# ---------------------------------------------------------------------------

async def validate_target(url) -> None:
    """
    Validates that a URL is safe to register as a monitor target.

    Checks (in order):
    1. Scheme must be http or https
    2. Hostname must resolve via DNS
    3. ALL resolved IPs must be outside the BLOCKED_NETWORKS list

    Raises ValidationError on any failure.
    Note: does not protect against DNS rebinding after registration
    (explicitly scoped out as MVP limitation).
    """
    # url is a pydantic HttpUrl object
    scheme = url.scheme if hasattr(url, "scheme") else str(url).split("://")[0]
    host = url.host if hasattr(url, "host") else None
    if not host:
        # Fallback: parse from string
        raw = str(url)
        try:
            host = raw.split("://", 1)[1].split("/")[0].split(":")[0]
        except IndexError:
            raise ValidationError("could not parse host from URL")

    if scheme not in ("http", "https"):
        raise ValidationError("only http/https URLs are allowed")

    try:
        loop = asyncio.get_event_loop()
        addr_infos = await loop.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValidationError("could not resolve host")

    if not addr_infos:
        raise ValidationError("could not resolve host")

    # All resolved addresses must be checked (TRD §6.3)
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        try:
            ip = ip_address(sockaddr[0])
        except ValueError:
            continue
        if any(ip in net for net in BLOCKED_NETWORKS):
            raise ValidationError(
                f"target resolves to a blocked network ({ip})"
            )
