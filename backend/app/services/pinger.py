"""
Async HTTP pinger using httpx.

Error taxonomy maps exactly to TRD §7.1:
- null error  → is_up=True, status_code < 400
- http_error  → is_up=False, status_code >= 400
- timeout     → is_up=False, no status_code
- dns_error   → is_up=False, no status_code
- connection_refused → is_up=False, no status_code
- unknown_error → is_up=False, no status_code (catch-all)

Invariant enforced: error IS NULL iff is_up=True.
"""
import socket
import time
from datetime import datetime, timezone

import httpx


async def ping_url(url: str, timeout_ms: int = 5000) -> dict:
    """
    Ping a URL and return a structured check result dict.

    Returns dict with keys:
        status_code, response_time_ms, is_up, error, checked_at
    """
    start = time.monotonic()
    timeout_s = timeout_ms / 1000.0

    try:
        async with httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
            # Don't verify SSL strictly — monitoring should detect the server
            # being up even with a misconfigured cert. For production, this
            # should be configurable per-monitor.
        ) as client:
            response = await client.get(url)

        elapsed_ms = (time.monotonic() - start) * 1000
        is_up = response.status_code < 400

        return {
            "status_code": response.status_code,
            "response_time_ms": round(elapsed_ms, 2),
            "is_up": is_up,
            "error": None if is_up else "http_error",
            "checked_at": datetime.now(timezone.utc),
        }

    except httpx.TimeoutException:
        return {
            "status_code": None,
            "response_time_ms": float(timeout_ms),  # worst-case observed latency
            "is_up": False,
            "error": "timeout",
            "checked_at": datetime.now(timezone.utc),
        }

    except (socket.gaierror, httpx.ConnectError) as exc:
        # Distinguish DNS failure from connection refused / reset
        exc_str = str(exc).lower()
        if "name or service not known" in exc_str or "nodename nor servname" in exc_str or "getaddrinfo" in exc_str:
            error_code = "dns_error"
        else:
            error_code = "connection_refused"
        return {
            "status_code": None,
            "response_time_ms": None,
            "is_up": False,
            "error": error_code,
            "checked_at": datetime.now(timezone.utc),
        }

    except Exception:
        # SSL errors, too-many-redirects, malformed response, etc.
        return {
            "status_code": None,
            "response_time_ms": None,
            "is_up": False,
            "error": "unknown_error",
            "checked_at": datetime.now(timezone.utc),
        }
