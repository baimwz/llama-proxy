"""Shared fixtures: mock llama.cpp server + full proxy app."""
from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from llama_proxy.config import Config
from llama_proxy.server import create_app

MOCK_WORDS = ["mock", "reply", "from", "the", "test", "llm"]


class MockLLM:
    """Emulates llama.cpp: /health + /v1/chat/completions (stream + non-stream)."""

    def __init__(self) -> None:
        self.ready = True
        self.requests: list[dict] = []

    def make_app(self) -> aiohttp.web.Application:
        mock = self
        app = aiohttp.web.Application()

        async def health(_req: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"status": "ok" if mock.ready else "loading model"}
            )

        async def chat(req: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
            payload = await req.json()
            mock.requests.append(payload)
            if payload.get("stream"):
                resp = aiohttp.web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await resp.prepare(req)
                for w in MOCK_WORDS:
                    chunk = {
                        "id": "x",
                        "object": "chat.completion.chunk",
                        "choices": [
                            {"index": 0, "delta": {"content": w + " "}, "finish_reason": None}
                        ],
                    }
                    await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    await asyncio.sleep(0.005)
                final = {
                    "id": "x",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": len(MOCK_WORDS),
                        "total_tokens": 100 + len(MOCK_WORDS),
                    },
                }
                await resp.write(f"data: {json.dumps(final)}\n\n".encode())
                await resp.write(b"data: [DONE]\n\n")
                await resp.write_eof()
                return resp
            return aiohttp.web.json_response(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": " ".join(MOCK_WORDS)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": len(MOCK_WORDS),
                        "total_tokens": 100 + len(MOCK_WORDS),
                    },
                }
            )

        app.router.add_get("/health", health)
        app.router.add_post("/v1/chat/completions", chat)
        return app

    async def start(self) -> None:
        self._runner = aiohttp.web.AppRunner(self.make_app())
        await self._runner.setup()
        self._site = aiohttp.web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        self.url = f"http://127.0.0.1:{self._site._server.sockets[0].getsockname()[1]}"

    async def stop(self) -> None:
        await self._runner.cleanup()


@pytest.fixture
async def mock_llm():
    m = MockLLM()
    await m.start()
    yield m
    await m.stop()


@pytest.fixture
async def app_env(tmp_path, mock_llm):
    cfg = Config.defaults()
    cfg.llm.base_url = mock_llm.url
    cfg.llm.boot_timeout_s = 5.0
    cfg.llm.health_interval_s = 0.2
    cfg.usage.data_dir = str(tmp_path / "data")
    cfg.usage.flush_interval_s = 60
    cfg.tokenizer.url = ""  # force heuristic counting in tests (no network)
    app = create_app(cfg)
    app["mock_llm"] = mock_llm
    client = TestClient(TestServer(app))
    await client.start_server()
    yield app, client
    await client.close()


def chat_payload(**extra) -> dict:
    payload = {"messages": [{"role": "user", "content": "hello proxy"}]}
    payload.update(extra)
    return payload
