"""Local token counting (HuggingFace `tokenizers` + model tokenizer.json).

Falls back to a conservative character heuristic when the tokenizer library
or the tokenizer file is unavailable, so the proxy keeps working.
"""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path

log = logging.getLogger("llama_proxy.tokenizer")


class TokenCounter:
    def __init__(self, url: str, cache_file: str | os.PathLike | None = None) -> None:
        self._url = url
        self._cache = Path(cache_file) if cache_file else None
        self._tok = None
        self._error: str | None = None

    @property
    def backend(self) -> str:
        return "huggingface" if self._tok is not None else "heuristic"

    @property
    def load_error(self) -> str | None:
        return self._error

    async def load(self) -> None:
        try:
            from tokenizers import Tokenizer
        except ImportError:
            self._error = "python package 'tokenizers' is not installed"
            log.warning("%s", self._error)
            return
        # 1) cached file
        if self._cache is not None and self._cache.is_file():
            if self._cache.stat().st_size > 10_000:
                try:
                    self._tok = Tokenizer.from_file(str(self._cache))
                    return
                except Exception as exc:
                    log.warning("cached tokenizer unreadable (%s); re-downloading", exc)
        # 2) download
        if not self._url:
            self._error = "no tokenizer URL configured"
            log.info("%s", self._error)
            return
        try:
            import httpx

            # follow_redirects: HuggingFace `resolve/...` URLs 302-redirect to a
            # CDN; without this the client reads the redirect body, not the file.
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                resp = await client.get(self._url)
                resp.raise_for_status()
            text = resp.text
            if self._cache is not None:
                try:
                    self._cache.parent.mkdir(parents=True, exist_ok=True)
                    self._cache.write_text(text)
                except OSError as exc:
                    log.warning("could not cache tokenizer: %s", exc)
            self._tok = Tokenizer.from_str(text)
        except Exception as exc:
            self._error = f"tokenizer download failed: {exc}"
            log.warning("%s", self._error)

    def count(self, text: str) -> int:
        if self._tok is not None:
            try:
                return max(1, len(self._tok.encode(text, add_special_tokens=False)))
            except Exception as exc:  # noqa: BLE001 - heuristic is a safe fallback
                log.debug("tokenize failed (%s); using heuristic", exc)
        # Conservative over-estimate (~3 chars per token for mixed text).
        return max(1, math.ceil(len(text) / 3))
