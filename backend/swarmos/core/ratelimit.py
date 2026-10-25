"""Database-shared fixed-window limiter for callers on all API replicas.

Only a digest of each caller key is persisted. In-memory fallback is reserved for SQLite in-memory databases.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from hashlib import sha256

from fastapi import Request
from sqlalchemy import text

from swarmos.config import get_settings
from swarmos.core.errors import ApiError
from swarmos.db import get_engine


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._last_cleanup = 0.0

    def _shared_check(self, key: str, limit: int, window_s: float) -> None:
        window = max(1, int(window_s))
        now = int(time.time())
        window_start = (now // window) * window
        digest = sha256(key.encode("utf-8", errors="replace")).hexdigest()
        bucket_key = f"{digest}:{window_start}"
        expires_at = window_start + window
        engine = get_engine()
        cleanup = False
        monotonic_now = time.monotonic()
        with self._cleanup_lock:
            if monotonic_now - self._last_cleanup >= 300:
                self._last_cleanup = monotonic_now
                cleanup = True
        with engine.begin() as conn:
            if cleanup:
                conn.execute(text("DELETE FROM shared_rate_limit_buckets WHERE expires_at < :now"), {"now": now})
            hit_count = conn.execute(text(
                "INSERT INTO shared_rate_limit_buckets (bucket_key, hits, expires_at) "
                "VALUES (:bucket_key, 1, :expires_at) "
                "ON CONFLICT (bucket_key) DO UPDATE "
                "SET hits = shared_rate_limit_buckets.hits + 1 "
                "WHERE shared_rate_limit_buckets.hits < :limit "
                "RETURNING hits"
            ), {"bucket_key": bucket_key, "expires_at": expires_at, "limit": limit}).scalar_one_or_none()
            if hit_count is None or hit_count > limit:
                current = conn.execute(text(
                    "SELECT hits FROM shared_rate_limit_buckets WHERE bucket_key = :bucket_key"
                ), {"bucket_key": bucket_key}).scalar_one_or_none() or limit
                retry = max(1, expires_at - now)
                raise ApiError(429, "rate_limited", "Too many requests, slow down", {
                    "retry_after_s": retry, "limit": limit, "window_s": window, "current": current,
                })

    def check(self, key: str, limit: int, window_s: float = 60.0) -> None:
        settings = get_settings()
        if settings.database_url.startswith("sqlite:///:memory:"):
            self._check_local(key, limit, window_s)
            return
        try:
            self._shared_check(key, limit, window_s)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(503, "rate_limit_storage_unavailable", "Shared request limiting is temporarily unavailable") from exc

    def _check_local(self, key: str, limit: int, window_s: float) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, int(window_s - (now - q[0])))
                raise ApiError(429, "rate_limited", "Too many requests, slow down", {"retry_after_s": retry})
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
        try:
            if not get_settings().database_url.startswith("sqlite:///:memory:"):
                with get_engine().begin() as conn:
                    conn.execute(text("DELETE FROM shared_rate_limit_buckets"))
        except Exception as exc:
            # Reset is a test helper; a missing database or pre-migration schema must not mask the real test.
            log.debug("rate_limit_reset_skipped", extra={"error_type": type(exc).__name__})
        with self._cleanup_lock:
            self._last_cleanup = 0.0


limiter = RateLimiter()
log = logging.getLogger("swarmos.ratelimit")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
