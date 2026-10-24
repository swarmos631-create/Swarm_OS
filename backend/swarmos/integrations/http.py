"""HTTP helper with bounded retries, backoff, Retry-After handling and error classification."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from swarmos.integrations.base import IntegrationError

RETRYABLE = {429, 500, 502, 503, 504}


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float = 15.0,  # noqa: ASYNC109 — per-request httpx timeout, not a coroutine deadline
    retries: int = 2,
    backoff_s: float = 0.6,
) -> Any:
    last: IntegrationError | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.request(method, url, params=params, data=data, timeout=timeout)
        except httpx.TimeoutException:
            last = IntegrationError("timeout", f"Timed out after {timeout:.0f}s contacting {httpx.URL(url).host}")
        except httpx.HTTPError as exc:
            last = IntegrationError("network", f"Network error contacting {httpx.URL(url).host}: {type(exc).__name__}")
        else:
            if resp.status_code in (401, 403):
                raise IntegrationError("auth", f"{httpx.URL(url).host} rejected the credentials (HTTP {resp.status_code})")
            if resp.status_code in RETRYABLE:
                kind = "rate_limited" if resp.status_code == 429 else "http_error"
                last = IntegrationError(kind, f"{httpx.URL(url).host} returned HTTP {resp.status_code}")
                retry_after = resp.headers.get("retry-after")
                if retry_after and retry_after.isdigit() and attempt < retries:
                    await asyncio.sleep(min(float(retry_after), 10.0))
                    continue
            elif resp.status_code >= 400:
                raise IntegrationError("http_error", f"{httpx.URL(url).host} returned HTTP {resp.status_code}")
            else:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise IntegrationError("schema", f"{httpx.URL(url).host} returned a non-JSON response") from exc
        if attempt < retries:
            await asyncio.sleep(backoff_s * (2**attempt) + random.uniform(0, 0.2))
    assert last is not None
    raise last
