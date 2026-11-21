"""Async client for the llama.cpp (OpenAI-compatible) server."""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from .config import LlmConfig
from .state import Job

log = logging.getLogger("llama_proxy.llmclient")


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg: LlmConfig) -> None:
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        self._model = cfg.model_name
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(connect=5.0, read=cfg.read_timeout_s, write=30.0, pool=5.0),
        )

    def _url(self, kind: str) -> str:
        return "/v1/chat/completions" if kind == "chat" else "/v1/completions"

    async def health(self) -> bool:
        try:
            resp = await self._http.get("/health", timeout=3.0)
            if resp.status_code != 200:
                return False
            try:
                data = resp.json()
            except json.JSONDecodeError:
                return True
            return data.get("status", "ok") == "ok"
        except httpx.HTTPError:
            return False

    async def forward(self, job: Job) -> dict:
        """Non-streaming request; stores usage info on the job."""
        resp = await self._http.post(self._url(job.kind), json=job.payload)
        if resp.status_code >= 400:
            raise LLMError(f"upstream {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        usage = data.get("usage") or {}
        job.prompt_tokens_actual = usage.get("prompt_tokens")
        job.completion_tokens_actual = usage.get("completion_tokens")
        job.cached_tokens_actual = (usage.get("prompt_tokens_details") or {}).get(
            "cached_tokens"
        )
        return data

    @asynccontextmanager
    async def stream(self, job: Job) -> AsyncIterator[httpx.Response]:
        payload = dict(job.payload)
        payload["stream"] = True
        opts = payload.get("stream_options")
        if not isinstance(opts, dict):
            opts = {}
        opts.setdefault("include_usage", True)
        payload["stream_options"] = opts
        async with self._http.stream("POST", self._url(job.kind), json=payload) as resp:
            yield resp

    async def close(self) -> None:
        await self._http.aclose()
