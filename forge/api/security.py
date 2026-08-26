"""API authentication and rate limiting.

An endpoint that executes tools and spends money is not something to leave
open. This is deliberately simple - API keys, compared in constant time,
supplied by the environment - because the alternative is either nothing or a
half-implemented OAuth flow, and of those three the simple one is the only
honest option for a service at this stage.

Keys are read from `FORGE_API_KEYS` (comma-separated) and never logged: the
principal's *label* appears in logs, the key never does.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status

__all__ = ["ApiKeyAuth", "Principal", "RateLimiter", "load_api_keys"]


@dataclass(frozen=True)
class Principal:
    """Who is calling. `anonymous` only when auth is explicitly disabled."""

    label: str
    anonymous: bool = False


ANONYMOUS = Principal(label="anonymous", anonymous=True)


def load_api_keys(env: dict[str, str] | None = None) -> dict[str, str]:
    """Parse `FORGE_API_KEYS`.

    Accepts `key` or `label:key`, comma-separated. Returns digest -> label, so
    the process never holds raw keys in a structure that could be logged or
    serialised by accident.
    """
    raw = (env if env is not None else dict(os.environ)).get("FORGE_API_KEYS", "")
    keys: dict[str, str] = {}
    for index, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry:
            continue
        label, _, secret = entry.rpartition(":")
        secret = secret or entry
        keys[_digest(secret)] = label or f"key-{index + 1}"
    return keys


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class ApiKeyAuth:
    """Validates a bearer token against the configured key set."""

    def __init__(self, keys: dict[str, str] | None = None, *, required: bool = True) -> None:
        self.keys = keys if keys is not None else load_api_keys()
        self.required = required

    @property
    def enabled(self) -> bool:
        return self.required and bool(self.keys)

    @property
    def misconfigured(self) -> bool:
        """Auth demanded but no keys supplied - fail closed, loudly."""
        return self.required and not self.keys

    def authenticate(self, request: Request) -> Principal:
        if not self.required:
            return ANONYMOUS
        if not self.keys:
            # Never silently fall back to open. A deployment that asked for
            # auth and supplied no keys is misconfigured, not public.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="authentication is required but no API keys are configured "
                       "(set FORGE_API_KEYS, or start with require_auth=False)",
            )

        presented = _extract_token(request)
        if presented is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        candidate = _digest(presented)
        # Compare every key in constant time: returning early on the first
        # mismatch leaks, through timing, how much of a key was correct.
        matched: str | None = None
        for known, label in self.keys.items():
            if hmac.compare_digest(candidate, known):
                matched = label
        if matched is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key"
            )
        return Principal(label=matched)


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.headers.get("x-api-key") or None


@dataclass
class RateLimiter:
    """A fixed-window limiter, per principal.

    In-process, so it bounds one replica rather than a fleet. That is stated
    plainly rather than implied: behind several replicas this is a safety net
    against a runaway client, not a quota system. A real quota belongs in the
    gateway or in shared state.
    """

    limit: int = 60
    window_s: float = 60.0
    _hits: dict[str, deque[float]] = field(default_factory=dict, init=False)

    def check(self, principal: Principal) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic()
        window = self._hits.setdefault(principal.label, deque())
        while window and now - window[0] > self.window_s:
            window.popleft()
        if len(window) >= self.limit:
            retry_after = max(1, int(self.window_s - (now - window[0])))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"rate limit exceeded ({self.limit} per {int(self.window_s)}s)",
                headers={"Retry-After": str(retry_after)},
            )
        window.append(now)

    def reset(self) -> None:
        self._hits.clear()
