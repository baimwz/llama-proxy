"""Conversation tracking for delta-based job classification.

A "large" job is one whose *new* prompt processing (prefill) is large. When a
conversation (e.g. a coding agent) re-sends most of its context that is
already in the server's KV cache, only the delta needs prefill, so it counts
as small. We approximate the cache state by tracking the longest common
prefix against the conversation's last observed prompt.
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

# The conversation key is the hash of the prompt HEAD. Conversations grow by
# appending, so the head stays stable; 256 chars is safely inside the stable
# head of any realistic conversation while still being unique per conversation.
KEY_PREFIX_CHARS = 256
STORE_CAP_CHARS = 262_144  # cap stored rendered text per conversation
MIN_MATCH_CHARS = 32


@dataclass
class ConvState:
    rendered: str
    total_tokens: int
    ts: float


def common_prefix_len(a: str, b: str) -> int:
    """Length of the longest common prefix of a and b (chunked compare)."""
    n = min(len(a), len(b))
    i = 0
    while i < n:
        step = min(65536, n - i)
        if a[i : i + step] == b[i : i + step]:
            i += step
            continue
        lo, hi = i, i + step
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if a[:mid] == b[:mid]:
                lo = mid
            else:
                hi = mid - 1
        return lo
    return n


class ConversationTracker:
    def __init__(self, ttl_s: float, max_states: int) -> None:
        self._ttl = ttl_s
        self._max = max_states
        self._states: OrderedDict[str, ConvState] = OrderedDict()

    def key(self, rendered: str) -> str:
        head = rendered[:KEY_PREFIX_CHARS].encode("utf-8", "replace")
        return hashlib.sha256(head).hexdigest()

    def delta_for(self, rendered: str, total_tokens: int, counter) -> int:
        """Estimated tokens that still need prefill (<= total_tokens)."""
        state = self._states.get(self.key(rendered))
        if state is not None and (time.time() - state.ts) <= self._ttl:
            lcp = common_prefix_len(state.rendered, rendered)
            if lcp >= MIN_MATCH_CHARS:
                return max(0, total_tokens - counter.count(rendered[:lcp]))
        return total_tokens

    def note(self, rendered: str, total_tokens: int) -> None:
        key = self.key(rendered)
        self._states[key] = ConvState(rendered[:STORE_CAP_CHARS], total_tokens, time.time())
        self._states.move_to_end(key)
        while len(self._states) > self._max:
            self._states.popitem(last=False)

    def invalidate(self) -> None:
        """Call after a server (re)start: the KV cache is empty again."""
        self._states.clear()

    def __len__(self) -> int:
        return len(self._states)
