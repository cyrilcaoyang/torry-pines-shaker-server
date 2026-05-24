"""Single-holder claim store implementing STATUS_SPEC v1.1.

A *claim* is a short-lived, exclusive lease on the device's ``/control/*``
surface. Exactly one (``owner``, ``session_id``) pair may hold a claim
at a time; while a claim is active, every ``/control/*`` request must
carry ``X-Claim-Token: <claim_token>`` or the device responds 423.

Heartbeat resilience: each successful heartbeat resets ``expires_at``
to ``now + ttl``. Clients are expected to send heartbeats every
``heartbeat_interval_s`` (= ``ttl/3``, clamped to a sane minimum).

Idempotency: a second ``acquire`` from the *same* ``session_id`` while
that session already holds the claim returns the existing token with a
refreshed ``expires_at``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone

from .models import ClaimedBy, ClaimRequest, ClaimResponse

logger = logging.getLogger(__name__)


_MIN_TTL_S = 5.0
_MAX_TTL_S = 600.0
_MIN_HEARTBEAT_S = 2.0


class ClaimStoreError(RuntimeError):
    """Base class. The API layer maps subclasses to HTTP responses."""


class ClaimConflict(ClaimStoreError):
    """Another session currently holds the claim. Maps to HTTP 409."""

    def __init__(self, claimed_by: ClaimedBy, retry_after_s: float) -> None:
        super().__init__(
            f"device is already claimed by session {claimed_by.session_id!r}"
        )
        self.claimed_by = claimed_by
        self.retry_after_s = retry_after_s


class UnknownClaim(ClaimStoreError):
    """Heartbeat / control call referenced an unknown or expired token."""


class ClaimStore:
    """In-memory single-holder claim manager."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._owner: str | None = None
        self._session_id: str | None = None
        self._expires_at: datetime | None = None
        self._heartbeat_interval_s: float = 0.0
        self._ttl_s: float = 0.0

    async def acquire(self, req: ClaimRequest) -> ClaimResponse:
        async with self._lock:
            now = _utcnow()
            self._expire_if_due(now)

            if self._token is not None and self._session_id != req.session_id:
                claimed_by = self._claimed_by_locked()
                assert claimed_by is not None
                retry_after = max(0.0, (claimed_by.expires_at - now).total_seconds())
                raise ClaimConflict(claimed_by, retry_after)

            ttl = _clamp(req.ttl_s, _MIN_TTL_S, _MAX_TTL_S)
            heartbeat = max(_MIN_HEARTBEAT_S, ttl / 3.0)

            if self._token is None:
                self._token = secrets.token_urlsafe(24)
                logger.info(
                    "claim acquired by %s/%s (ttl=%.1fs)",
                    req.owner,
                    req.session_id,
                    ttl,
                )
            else:
                logger.debug(
                    "claim refreshed by same session %s (ttl=%.1fs)",
                    req.session_id,
                    ttl,
                )

            self._owner = req.owner
            self._session_id = req.session_id
            self._ttl_s = ttl
            self._heartbeat_interval_s = heartbeat
            self._expires_at = now + timedelta(seconds=ttl)

            return ClaimResponse(
                claim_token=self._token,
                heartbeat_interval_s=self._heartbeat_interval_s,
                expires_at=self._expires_at,
            )

    async def heartbeat(self, token: str | None) -> ClaimResponse:
        async with self._lock:
            now = _utcnow()
            self._expire_if_due(now)

            if (
                self._token is None
                or token is None
                or not secrets.compare_digest(self._token, token)
            ):
                raise UnknownClaim()

            self._expires_at = now + timedelta(seconds=self._ttl_s)
            return ClaimResponse(
                claim_token=self._token,
                heartbeat_interval_s=self._heartbeat_interval_s,
                expires_at=self._expires_at,
            )

    async def release(self, token: str | None) -> None:
        async with self._lock:
            now = _utcnow()
            self._expire_if_due(now)

            if self._token is None:
                return
            if token is None or not secrets.compare_digest(self._token, token):
                logger.debug("release called with mismatched token; ignored")
                return

            logger.info(
                "claim released by %s/%s", self._owner, self._session_id
            )
            self._clear_locked()

    async def validate(self, token: str | None) -> bool:
        async with self._lock:
            now = _utcnow()
            self._expire_if_due(now)
            if self._token is None or token is None:
                return False
            return secrets.compare_digest(self._token, token)

    async def current(self) -> ClaimedBy | None:
        async with self._lock:
            now = _utcnow()
            self._expire_if_due(now)
            return self._claimed_by_locked()

    async def is_claimed(self) -> bool:
        async with self._lock:
            now = _utcnow()
            self._expire_if_due(now)
            return self._token is not None

    async def force_clear(self) -> None:
        async with self._lock:
            if self._token is not None:
                logger.info(
                    "force_clear dropped claim from %s/%s",
                    self._owner,
                    self._session_id,
                )
            self._clear_locked()

    def _expire_if_due(self, now: datetime) -> None:
        if self._token is None or self._expires_at is None:
            return
        if now >= self._expires_at:
            logger.info(
                "claim expired (was held by %s/%s)",
                self._owner,
                self._session_id,
            )
            self._clear_locked()

    def _clear_locked(self) -> None:
        self._token = None
        self._owner = None
        self._session_id = None
        self._expires_at = None
        self._heartbeat_interval_s = 0.0
        self._ttl_s = 0.0

    def _claimed_by_locked(self) -> ClaimedBy | None:
        if (
            self._token is None
            or self._owner is None
            or self._session_id is None
            or self._expires_at is None
        ):
            return None
        return ClaimedBy(
            session_id=self._session_id,
            owner=self._owner,
            expires_at=self._expires_at,
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


__all__ = [
    "ClaimConflict",
    "ClaimStore",
    "ClaimStoreError",
    "UnknownClaim",
]
