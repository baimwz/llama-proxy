"""Authentication and limit enforcement (phase 2).

Resolves the caller of a request (master key, user key, or the open dev
mode) and checks the per-user rate limits before a job is enqueued.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiohttp import web

from .keys import KeyStore, UserKey
from .ratelimit import RateLimiter
from .usage import UsageTracker


@dataclass
class Identity:
    name: str
    master: bool = False
    key: UserKey | None = None


def token_from_header(header: str) -> str:
    return header[7:] if header.startswith("Bearer ") else header


class Auth:
    def __init__(self, keys: KeyStore, limiter: RateLimiter, usage: UsageTracker) -> None:
        self._keys = keys
        self._limiter = limiter
        self._usage = usage

    def auth_required(self, master_key: str) -> bool:
        """Auth is required once any key (master or user) is configured."""
        return bool(master_key) or len(self._keys) > 0

    def resolve(self, request: web.Request, master_key: str) -> Identity | None:
        """Return the caller's identity, or None when the key is invalid."""
        token = token_from_header(request.headers.get("Authorization", ""))
        return self.resolve_token(token, master_key)

    def resolve_token(self, token: str, master_key: str) -> Identity | None:
        """Resolve a raw bearer token to an identity (no request needed)."""
        if not self.auth_required(master_key):
            return Identity(name="default")  # dev mode: no keys configured at all
        if master_key and secrets.compare_digest(token, master_key):
            return Identity(name="master", master=True)
        user = self._keys.lookup(token)
        if user is None or user.disabled:
            return None
        return Identity(name=user.name, master=False, key=user)

    def rate_limit_error(self, ident: Identity, est_tokens: int) -> tuple[bool, int]:
        """Check the per-hour and per-day limits.

        Returns (violated, retry_after_s).
        """
        key = ident.key
        if ident.master or key is None:
            return False, 0
        ok, retry_after = self._limiter.allow(ident.name, key.requests_per_hour)
        if not ok:
            return True, retry_after
        if key.tokens_per_day > 0:
            used = self._usage.today_total(ident.name)
            if used + est_tokens > key.tokens_per_day:
                now = datetime.now()
                midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
                return True, max(1, int((midnight - now).total_seconds()))
        return False, 0
