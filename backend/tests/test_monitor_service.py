"""
Unit tests for monitor_service.py — consecutive-failure state machine and SSRF guard.
All tests from TRD §9.1.

These tests do not require a database — they test pure functions.
"""
import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.monitor_service import (
    BLOCKED_NETWORKS,
    ValidationError,
    apply_check_result,
    validate_target,
)


# ---------------------------------------------------------------------------
# Helper: build a mock Monitor with minimal fields
# ---------------------------------------------------------------------------

def _monitor(state: str, failures: int):
    return SimpleNamespace(current_state=state, consecutive_failures=failures)


# ---------------------------------------------------------------------------
# Consecutive-failure state machine (TRD §9.1)
# ---------------------------------------------------------------------------

class TestApplyCheckResult:
    def test_new_monitor_first_failure_does_not_flip_to_down(self):
        """
        A monitor in 'unknown' state with 0 prior failures, given one failed
        check, ends in 'unknown' state with consecutive_failures = 1.
        """
        m = _monitor("unknown", 0)
        new_state = apply_check_result(m, success=False)
        assert new_state == "unknown"
        assert m.consecutive_failures == 1

    def test_second_consecutive_failure_flips_to_down(self):
        """
        A monitor with consecutive_failures = 1, given another failed check,
        ends in 'down' state.
        """
        m = _monitor("unknown", 1)
        new_state = apply_check_result(m, success=False)
        assert new_state == "down"
        assert m.consecutive_failures == 2

    def test_success_resets_failure_count(self):
        """
        A monitor in 'down' state with consecutive_failures = 3, given a
        successful check, ends in 'up' state with consecutive_failures = 0.
        """
        m = _monitor("down", 3)
        new_state = apply_check_result(m, success=True)
        assert new_state == "up"
        assert m.consecutive_failures == 0

    def test_third_consecutive_failure_stays_down(self):
        """
        A monitor already 'down', given another failure, remains 'down'.
        The state is idempotent regardless of further failures.
        """
        m = _monitor("down", 2)
        new_state = apply_check_result(m, success=False)
        assert new_state == "down"
        assert m.consecutive_failures == 3

    def test_up_monitor_first_failure_does_not_flip(self):
        """An 'up' monitor getting its first failure stays 'up'."""
        m = _monitor("up", 0)
        new_state = apply_check_result(m, success=False)
        assert new_state == "up"
        assert m.consecutive_failures == 1

    def test_success_from_unknown_state(self):
        """A brand-new monitor's first successful check goes to 'up'."""
        m = _monitor("unknown", 0)
        new_state = apply_check_result(m, success=True)
        assert new_state == "up"
        assert m.consecutive_failures == 0


# ---------------------------------------------------------------------------
# SSRF guard (TRD §9.1)
# ---------------------------------------------------------------------------

def _make_url(scheme: str, host: str):
    """Build a minimal object that validate_target can inspect."""
    return SimpleNamespace(scheme=scheme, host=host)


@pytest.mark.asyncio
class TestValidateTarget:
    async def test_ssrf_guard_rejects_non_http_scheme(self):
        """ftp:// URL raises ValidationError before any DNS lookup."""
        url = _make_url("ftp", "example.com")
        with pytest.raises(ValidationError, match="only http/https"):
            await validate_target(url)

    async def test_ssrf_guard_blocks_private_ip_literal(self):
        """A hostname that resolves to 192.168.x.x is blocked."""
        url = _make_url("http", "192.168.1.1")
        # Patch getaddrinfo to return the private IP directly
        fake_infos = [(socket.AF_INET, None, None, None, ("192.168.1.1", 80))]
        with patch("app.services.monitor_service.asyncio") as mock_asyncio:
            mock_loop = AsyncMock()
            mock_asyncio.get_event_loop.return_value = mock_loop
            mock_loop.getaddrinfo = AsyncMock(return_value=fake_infos)
            with pytest.raises(ValidationError, match="blocked network"):
                await validate_target(url)

    async def test_ssrf_guard_blocks_resolved_metadata_endpoint(self):
        """A public-looking hostname resolving to 169.254.169.254 is blocked."""
        url = _make_url("https", "totally-legit-site.com")
        fake_infos = [(socket.AF_INET, None, None, None, ("169.254.169.254", 443))]
        with patch("app.services.monitor_service.asyncio") as mock_asyncio:
            mock_loop = AsyncMock()
            mock_asyncio.get_event_loop.return_value = mock_loop
            mock_loop.getaddrinfo = AsyncMock(return_value=fake_infos)
            with pytest.raises(ValidationError, match="blocked network"):
                await validate_target(url)

    async def test_ssrf_guard_allows_public_target(self):
        """A hostname resolving to a public IP passes validation."""
        url = _make_url("https", "example.com")
        fake_infos = [(socket.AF_INET, None, None, None, ("93.184.216.34", 443))]
        with patch("app.services.monitor_service.asyncio") as mock_asyncio:
            mock_loop = AsyncMock()
            mock_asyncio.get_event_loop.return_value = mock_loop
            mock_loop.getaddrinfo = AsyncMock(return_value=fake_infos)
            # Should not raise
            await validate_target(url)

    async def test_ssrf_guard_checks_all_resolved_ips(self):
        """
        A hostname resolving to BOTH a public and a private IP must be rejected.
        (TRD §6.3: all resolved addresses must be checked.)
        """
        url = _make_url("https", "dual-stack.example.com")
        fake_infos = [
            (socket.AF_INET, None, None, None, ("93.184.216.34", 443)),   # public
            (socket.AF_INET, None, None, None, ("10.0.0.1", 443)),         # private!
        ]
        with patch("app.services.monitor_service.asyncio") as mock_asyncio:
            mock_loop = AsyncMock()
            mock_asyncio.get_event_loop.return_value = mock_loop
            mock_loop.getaddrinfo = AsyncMock(return_value=fake_infos)
            with pytest.raises(ValidationError, match="blocked network"):
                await validate_target(url)

    async def test_ssrf_guard_raises_on_dns_failure(self):
        """An unresolvable hostname raises ValidationError."""
        url = _make_url("https", "this-does-not-resolve.invalid")
        with patch("app.services.monitor_service.asyncio") as mock_asyncio:
            mock_loop = AsyncMock()
            mock_asyncio.get_event_loop.return_value = mock_loop
            mock_loop.getaddrinfo = AsyncMock(side_effect=socket.gaierror("Name not found"))
            with pytest.raises(ValidationError, match="could not resolve"):
                await validate_target(url)
