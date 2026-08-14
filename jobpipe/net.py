"""Rate-limited HTTP. Constraint #4.

Minimum 2s between requests to any single host, plus jitter, enforced centrally so
no adapter can forget. Descriptive User-Agent carrying the operator's contact email.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings
from .log import get

log = get("jobpipe.net")


class FetchError(Exception):
    """Non-recoverable fetch failure. Never swallowed — constraint #5."""


class RateLimiter:
    """Per-host minimum interval with jitter. Thread-safe."""

    def __init__(self, min_interval: float, jitter: float) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> float:
        """Block until it is polite to hit `host`. Returns seconds slept."""
        with self._lock:
            now = time.monotonic()
            gap = self.min_interval + random.uniform(0, self.jitter)
            last = self._last.get(host)
            sleep_for = 0.0 if last is None else max(0.0, (last + gap) - now)
            # Reserve the slot before releasing the lock so concurrent callers
            # queue behind us instead of all measuring against the same `last`.
            self._last[host] = now + sleep_for
        if sleep_for > 0:
            log.debug("rate limit: sleeping %.2fs before %s", sleep_for, host)
            time.sleep(sleep_for)
        return sleep_for


class Http:
    """Thin httpx wrapper: rate limiting, identifying UA, bounded retries."""

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.limiter = RateLimiter(settings.min_interval, settings.jitter)
        self.user_agent = (
            f"jobpipe/0.1 (+mailto:{settings.contact_email}) "
            "personal job-search tool; contact to request exclusion"
        )
        self._client = httpx.Client(
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Http":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        host = urlparse(url).netloc
        if not host:
            raise FetchError(f"not an absolute URL: {url!r}")

        last_exc: Exception | None = None
        for attempt in range(1, self.settings.http_retries + 1):
            self.limiter.wait(host)
            try:
                resp = self._client.get(url, **kwargs)
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning("GET %s failed (attempt %d): %s", url, attempt, exc)
            else:
                if resp.status_code in self.RETRY_STATUS:
                    last_exc = FetchError(f"HTTP {resp.status_code} from {url}")
                    log.warning(
                        "GET %s -> HTTP %d (attempt %d)", url, resp.status_code, attempt
                    )
                elif resp.status_code >= 400:
                    # 4xx other than 429 will not fix itself. Fail immediately.
                    raise FetchError(f"HTTP {resp.status_code} from {url}")
                else:
                    log.debug("GET %s -> %d (%d bytes)", url, resp.status_code, len(resp.content))
                    return resp

            if attempt < self.settings.http_retries:
                backoff = 2.0**attempt + random.uniform(0, 1)
                log.info("retrying %s in %.1fs", url, backoff)
                time.sleep(backoff)

        raise FetchError(f"GET {url} failed after {self.settings.http_retries} attempts: {last_exc}")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = self.get(url, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            ct = resp.headers.get("content-type", "?")
            raise FetchError(
                f"{url} returned {ct}, not JSON (first 200 bytes: {resp.text[:200]!r})"
            ) from exc

    def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        host = urlparse(url).netloc
        if not host:
            raise FetchError(f"not an absolute URL: {url!r}")
        self.limiter.wait(host)
        try:
            resp = self._client.post(url, json=payload, **kwargs)
        except httpx.HTTPError as exc:
            raise FetchError(f"POST {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError(f"{url} did not return JSON") from exc
