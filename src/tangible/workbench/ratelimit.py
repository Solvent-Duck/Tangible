"""A small in-process rate limiter for the public, unauthenticated routes (the invite preview and
accept). Per client IP, fixed one-minute windows. One process per deployment is assumed; behind
several replicas each keeps its own count, which still bounds token guessing per replica."""

import threading
import time
from collections.abc import Callable

from fastapi import HTTPException, Request


class RateLimit:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, int], int] = {}

    def check(self, key: str) -> None:
        window = int(self.clock() // 60)
        with self._lock:
            if len(self._hits) > 10_000:  # drop old windows
                self._hits = {k: v for k, v in self._hits.items() if k[1] == window}
            n = self._hits.get((key, window), 0) + 1
            self._hits[(key, window)] = n
        if n > self.per_minute:
            raise HTTPException(429, "too many requests; try again in a minute")

    def __call__(self, request: Request) -> None:
        self.check(request.client.host if request.client else "unknown")


def public_limit(request: Request) -> None:
    """Dependency: the app's limiter for public routes (`app.state.public_limit`)."""
    limiter: RateLimit = request.app.state.public_limit
    limiter(request)
