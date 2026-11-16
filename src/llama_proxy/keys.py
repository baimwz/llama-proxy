"""Per-user API keys (phase 2).

Keys live in one small JSON file (``data/keys.json``) as salted SHA-256
hashes - the plaintext key is shown exactly once, at creation time. No
database, and the file is git-ignored. The running proxy picks up file
changes automatically (mtime check), so the CLI and the API can both
manage keys while the proxy keeps running.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("llama_proxy.keys")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
KEY_PREFIX = "lp-"
KEY_BYTES = 24


def clamp_priority(value: object) -> int:
    try:
        return max(0, min(10, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


@dataclass
class UserKey:
    name: str
    prefix: str  # "lp-ab12..." - only for display, not secret
    priority: int = 3  # default request priority when X-Priority is absent
    max_priority: int = 10  # ceiling for the X-Priority header (0-10)
    requests_per_hour: int = 0  # 0 = unlimited
    tokens_per_day: int = 0  # 0 = unlimited
    disabled: bool = False
    created_at: str = ""  # ISO date


class KeyStore:
    """Load/save/lookup of user keys with hot reload on file change."""

    UPDATABLE = ("priority", "max_priority", "requests_per_hour", "tokens_per_day", "disabled")

    def __init__(
        self,
        path: str | os.PathLike,
        default_priority: int = 3,
        default_max_priority: int = 10,
        default_requests_per_hour: int = 0,
        default_tokens_per_day: int = 0,
    ) -> None:
        self._path = Path(path)
        self._defaults = {
            "priority": clamp_priority(default_priority),
            "max_priority": clamp_priority(default_max_priority),
            "requests_per_hour": max(0, int(default_requests_per_hour)),
            "tokens_per_day": max(0, int(default_tokens_per_day)),
        }
        self._salt = b""
        self._by_hash: dict[str, UserKey] = {}
        self._stat: tuple[float, int] = (0.0, 0)
        self._load()

    # -- file I/O -----------------------------------------------------------

    def _load(self) -> None:
        if not self._path.is_file():
            self._salt = b""
            self._by_hash = {}
            self._stat = (0.0, 0)
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            salt = bytes.fromhex(raw.get("salt", ""))
            users = {h: UserKey(**meta) for h, meta in (raw.get("users") or {}).items()}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("key file unreadable (%s); starting with no user keys", exc)
            self._salt = b""
            self._by_hash = {}
            self._stat = (0.0, 0)
            return
        self._salt = salt
        self._by_hash = users
        st = self._path.stat()
        self._stat = (st.st_mtime, st.st_size)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "salt": self._salt.hex(),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "users": {h: asdict(u) for h, u in self._by_hash.items()},
        }
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, self._path)
        st = self._path.stat()
        self._stat = (st.st_mtime, st.st_size)

    def reload_if_changed(self) -> None:
        try:
            st = self._path.stat()
        except OSError:
            return
        if (st.st_mtime, st.st_size) != self._stat:
            log.info("key file changed; reloading user keys")
            self._load()

    # -- hashing / lookup ---------------------------------------------------

    def hash_key(self, key: str) -> str:
        return hashlib.sha256(self._salt + key.encode("utf-8")).hexdigest()

    def lookup(self, key: str) -> UserKey | None:
        """Resolve a plaintext API key to its record (or None)."""
        if not key:
            return None
        self.reload_if_changed()
        return self._by_hash.get(self.hash_key(key))

    @staticmethod
    def _norm(name: str) -> str:
        return name.strip().lower()

    def get(self, name: str) -> UserKey | None:
        name = self._norm(name)
        for user in self._by_hash.values():
            if user.name == name:
                return user
        return None

    def list(self) -> list[UserKey]:
        return sorted(self._by_hash.values(), key=lambda u: u.name)

    def __len__(self) -> int:
        return len(self._by_hash)

    # -- mutation (CLI + admin API) ----------------------------------------

    def create(
        self,
        name: str,
        priority: int | None = None,
        max_priority: int | None = None,
        requests_per_hour: int | None = None,
        tokens_per_day: int | None = None,
    ) -> tuple[UserKey, str]:
        """Create a key; returns (record, plaintext). Plaintext is one-shot."""
        name = self._norm(name)
        if not NAME_RE.match(name):
            raise ValueError(f"invalid user name {name!r} (use a-z 0-9 . _ -, max 32 chars)")
        if self.get(name) is not None:
            raise ValueError(f"user {name!r} already exists")
        if not self._salt:
            self._salt = secrets.token_bytes(16)
        key = KEY_PREFIX + secrets.token_hex(KEY_BYTES)
        record = UserKey(
            name=name,
            prefix=key[:12],
            priority=clamp_priority(
                self._defaults["priority"] if priority is None else priority
            ),
            max_priority=clamp_priority(
                self._defaults["max_priority"] if max_priority is None else max_priority
            ),
            requests_per_hour=max(
                0,
                int(
                    self._defaults["requests_per_hour"]
                    if requests_per_hour is None
                    else requests_per_hour
                ),
            ),
            tokens_per_day=max(
                0,
                int(self._defaults["tokens_per_day"] if tokens_per_day is None else tokens_per_day),
            ),
            created_at=datetime.now().strftime("%Y-%m-%d"),
        )
        record.priority = min(record.priority, record.max_priority)
        self._by_hash[self.hash_key(key)] = record
        self._save()
        log.info("created key for user %s", name)
        return record, key

    def update(self, name: str, **fields: object) -> UserKey:
        user = self.get(name)
        if user is None:
            raise KeyError(name)
        for field, value in fields.items():
            if field not in self.UPDATABLE:
                raise ValueError(f"field {field!r} is not updatable")
            if field in ("priority", "max_priority"):
                setattr(user, field, clamp_priority(value))
            elif field in ("requests_per_hour", "tokens_per_day"):
                setattr(user, field, max(0, int(value)))  # type: ignore[arg-type]
            elif field == "disabled":
                user.disabled = bool(value)
        user.priority = min(user.priority, user.max_priority)
        self._save()
        return user

    def set_disabled(self, name: str, disabled: bool) -> UserKey:
        return self.update(name, disabled=disabled)

    def remove(self, name: str) -> None:
        name = self._norm(name)
        for key_hash, user in list(self._by_hash.items()):
            if user.name == name:
                del self._by_hash[key_hash]
                break
        else:
            raise KeyError(name)
        self._save()
        log.info("removed key for user %s", name)
