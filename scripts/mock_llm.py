#!/usr/bin/env python3
"""Tiny llama.cpp lookalike for testing the proxy without the real server.

Usage:
    python scripts/mock_llm.py --port 9090 [--slow-boot 2]

Serves GET /health and POST /v1/chat/completions (streaming + non-streaming,
with usage), mimicking llama.cpp behaviour closely enough for smoke tests.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiohttp import web  # noqa: E402


def make_chunks(reply: str) -> list[str]:
    words = (reply + " ").split(" ")[: max(6, len(reply) // 4)]
    out = []
    for w in words:
        out.append(
            json.dumps(
                {
                    "id": "cmpl-mock",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}],
                }
            )
            + "\n"
        )
    out.append(
        json.dumps(
            {
                "id": "cmpl-mock",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 42,
                    "completion_tokens": len(words),
                    "total_tokens": 42 + len(words),
                },
            }
        )
        + "\n"
    )
    out.append("data: [DONE]\n")
    return out


def build_app(ready: bool) -> web.Application:
    app = web.Application()
    reply = "This is a mock reply from the llama.cpp stand-in server."

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok" if ready.value else "loading model"})

    async def chat(request: web.Request) -> web.StreamResponse:
        payload = await request.json()
        if payload.get("stream"):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            for chunk in make_chunks(reply):
                await resp.write(chunk.encode())
                await asyncio.sleep(0.01)
            await resp.write_eof()
            return resp
        return web.json_response(
            {
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": payload.get("model", "mock"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54},
            }
        )

    app.router.add_get("/health", health)
    app.router.add_post("/v1/chat/completions", chat)
    return app


async def main(args: argparse.Namespace) -> int:
    ready = type("R", (), {"value": not args.slow_boot > 0})()
    app = build_app(ready)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()
    print(f"mock llama.cpp listening on 127.0.0.1:{args.port} (ready={ready.value})")
    if args.slow_boot:
        await asyncio.sleep(args.slow_boot)
        ready.value = True
        print("mock now ready")
    try:
        await asyncio.get_running_loop().create_future()
    except asyncio.CancelledError:
        pass
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9090)
    p.add_argument("--slow-boot", type=float, default=0)
    a = p.parse_args()
    try:
        asyncio.run(main(a))
    except KeyboardInterrupt:
        pass
