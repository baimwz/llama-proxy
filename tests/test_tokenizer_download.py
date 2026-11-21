"""Regression: tokenizer download must follow HTTP redirects.

HuggingFace ``resolve/...`` URLs 302-redirect to a CDN. If the httpx client
does not follow redirects, ``load()`` reads the small redirect body instead of
the tokenizer file and silently falls back to the character heuristic.
"""
from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp.web import Application, AppRunner, Response, TCPSite, json_response

from llama_proxy.tokenizer import TokenCounter

# Minimal valid WordLevel tokenizer (unk must be present in the vocab).
MINIMAL_TOKENIZER = {
    "version": "1.0",
    "added_tokens": [],
    "normalizer": None,
    "pre_tokenizer": None,
    "post_processor": None,
    "decoder": None,
    "model": {
        "type": "WordLevel",
        "vocab": {"unk": 0, "hello": 1, "world": 2},
        "unk_token": "unk",
    },
}


@pytest.fixture
async def redirect_server():
    """Local server: /redirect 302-redirects to /real (like HuggingFace CDN)."""

    async def redirect(_req) -> aiohttp.web.StreamResponse:
        return Response(status=302, headers={"Location": "/real"})

    async def real(_req) -> aiohttp.web.StreamResponse:
        return json_response(MINIMAL_TOKENIZER)

    app = Application()
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/real", real)

    runner = AppRunner(app)
    await runner.setup()
    site = TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


async def test_download_follows_redirect(redirect_server, tmp_path) -> None:
    cache = tmp_path / "tokenizer.json"
    counter = TokenCounter(redirect_server + "/redirect", cache)
    await counter.load()
    assert counter.backend == "huggingface", counter.load_error
    assert counter.load_error is None
    assert counter.count("hello") == 1
    # and the file was cached for next start
    assert cache.is_file()
    assert json.loads(cache.read_text())["model"]["type"] == "WordLevel"


async def test_direct_url_still_works(redirect_server, tmp_path) -> None:
    """No-redirect URL must keep working (not only the redirect case)."""
    cache = tmp_path / "tok2.json"
    counter = TokenCounter(redirect_server + "/real", cache)
    await counter.load()
    assert counter.backend == "huggingface", counter.load_error
