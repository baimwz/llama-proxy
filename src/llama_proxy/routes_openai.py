"""OpenAI-compatible API routes (/v1/*) plus job/queue extension routes."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import web

from .auth import Identity
from .power import ProxyState
from .render import render_prompt
from .state import Job, JobState

log = logging.getLogger("llama_proxy.routes")

PRIORITY_HEADER = "x-priority"


def err(message: str, status: int, **extra) -> web.Response:
    body = {"error": {"message": message, "type": "proxy_error", **extra}}
    return web.json_response(body, status=status)


def rate_limited(retry_after: int) -> web.Response:
    resp = web.json_response(
        {
            "error": {
                "message": f"rate limit exceeded - retry in about {retry_after}s",
                "type": "rate_limit_exceeded",
                "code": "rate_limit_exceeded",
            }
        },
        status=429,
    )
    resp.headers["Retry-After"] = str(retry_after)
    return resp


def _resolve(request: web.Request) -> Identity | None:
    return request.app["auth"].resolve(request, request.app["cfg"].proxy.master_api_key)


def parse_priority(request: web.Request, ident: Identity) -> int:
    """X-Priority (0-10) if present, else the user's default priority.

    For user keys the header value is capped at the key's max_priority;
    without a header the key's own priority applies.
    """
    raw = request.headers.get(PRIORITY_HEADER, "")
    default, cap = 0, 10
    if ident.key is not None:
        default, cap = ident.key.priority, ident.key.max_priority
    if raw:
        try:
            return max(0, min(cap, int(raw)))
        except (TypeError, ValueError):
            return default
    return default


def make_job(request: web.Request, kind: str, payload: dict, ident: Identity) -> Job:
    cfg = request.app["cfg"]
    if "model" not in payload or not payload.get("model"):
        payload["model"] = cfg.llm.model_name
    prompt_text = render_prompt(kind, payload)
    return Job(
        id=None,
        kind=kind,
        payload=payload,
        prompt_text=prompt_text,
        user=ident.name,
        priority=parse_priority(request, ident),
        is_stream=bool(payload.get("stream")),
    )


def _setup(app: web.Application) -> None:
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_post("/v1/completions", completions)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/v1/jobs/{job_id}", get_job)
    app.router.add_get("/v1/queue", queue_view)


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


async def chat_completions(request: web.Request) -> web.StreamResponse:
    return await _handle(request, "chat")


async def completions(request: web.Request) -> web.StreamResponse:
    return await _handle(request, "completions")


async def _handle(request: web.Request, kind: str) -> web.StreamResponse:
    app = request.app
    ident = _resolve(request)
    if ident is None:
        return err("invalid or missing API key", 401)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return err("body must be valid JSON", 400)
    if not isinstance(payload, dict):
        return err("body must be a JSON object", 400)

    job = make_job(request, kind, payload, ident)
    job.prompt_tokens_est = app["counter"].count(job.prompt_text)
    violated, retry_after = app["auth"].rate_limit_error(ident, job.prompt_tokens_est)
    if violated:
        return rate_limited(retry_after)

    app["power"].note_activity()
    app["scheduler"].enqueue(job)

    if app["power"].state is ProxyState.READY:
        if job.is_stream:
            return await stream_response(request, app, job)
        return await wait_for_job(request, app, job)
    # Server is off/starting:
    #  - streaming clients wait on a live SSE stream with heartbeats
    #  - non-streaming clients get a 202 + job id to poll (clean contract)
    if job.is_stream:
        return await stream_response(request, app, job)
    return web.json_response(
        {
            "job_id": job.id,
            "status": "queued",
            "state": app["power"].state.value,
            "poll": f"/v1/jobs/{job.id}",
        },
        status=202,
    )


async def models(request: web.Request) -> web.Response:
    if _resolve(request) is None:
        return err("invalid or missing API key", 401)
    name = request.app["cfg"].llm.model_name
    return web.json_response(
        {
            "object": "list",
            "data": [{"id": name, "object": "model", "owned_by": "llama-proxy"}],
        }
    )


async def get_job(request: web.Request) -> web.Response:
    if _resolve(request) is None:
        return err("invalid or missing API key", 401)
    job = request.app["queue"].get(request.match_info["job_id"])
    if job is None:
        return err("job not found", 404)
    return web.json_response(request.app["queue"].public(job, include_result=True))


async def queue_view(request: web.Request) -> web.Response:
    if _resolve(request) is None:
        return err("invalid or missing API key", 401)
    app = request.app
    queue = app["queue"]
    return web.json_response(
        {
            "server_state": app["power"].state.value,
            "idle_seconds": round(app["power"].idle_seconds, 1),
            "active": [queue.public(j) for j in queue.active()],
            "queued": [queue.public(j) for j in queue.pending()],
        }
    )


# ---------------------------------------------------------------------------
# response modes
# ---------------------------------------------------------------------------


async def wait_for_job(request: web.Request, app: web.Application, job: Job) -> web.Response:
    deadline = time.time() + app["cfg"].proxy.client_wait_timeout_s
    while job.state in (JobState.QUEUED, JobState.RUNNING):
        if time.time() > deadline:
            return err(
                "timed out waiting for the LLM server",
                503,
                job_id=job.id,
                poll=f"/v1/jobs/{job.id}",
            )
        await asyncio.sleep(0.2)
    if job.state is JobState.DONE:
        data = dict(job.result or {})
        data["job_id"] = job.id
        return web.json_response(data)
    return err(job.error or "job failed", 502, job_id=job.id)


def _comment(text: str) -> bytes:
    return f": {text}\n\n".encode()


async def stream_response(
    request: web.Request, app: web.Application, job: Job
) -> web.StreamResponse:
    job.init_stream()
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Proxy-Job-Id": job.id,
        },
    )
    await resp.prepare(request)
    hb_s = max(5.0, app["cfg"].proxy.sse_heartbeat_s)
    try:
        async for chunk in _pump(request, app, job, resp, hb_s):
            await resp.write(chunk)
        await resp.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        log.info("client disconnected during stream for job %s", job.id)
    finally:
        # client went away before the job finished -> stop burning server time
        if job.state in (JobState.QUEUED, JobState.RUNNING):
            job.request_cancel()
    return resp


async def _pump(request, app, job, resp, hb_s):
    """Yield upstream chunks; emit SSE heartbeats while the job is queued.

    Single writer task so stream frames and heartbeats never interleave.
    """
    agen = job.chunks()
    pending = asyncio.ensure_future(agen.__anext__())
    hb_due = time.monotonic() + hb_s
    try:
        while True:
            timeout = max(0.05, hb_due - time.monotonic())
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            if pending in done:
                try:
                    yield pending.result()
                except StopAsyncIteration:
                    return
                pending = asyncio.ensure_future(agen.__anext__())
                hb_due = time.monotonic() + hb_s
                continue
            # heartbeat due: keep the connection alive while the job waits
            # (queued, or running but still before the first byte, e.g. while
            # the server boots)
            if job.ttfb_at is None:
                yield _comment(f"waiting (state={app['power'].state.value}, job={job.state.value})")
                hb_due = time.monotonic() + hb_s
            await asyncio.sleep(0.2)
    finally:
        if not pending.done():
            pending.cancel()
            # Wait for the in-flight __anext__ to actually settle; aclose()
            # while it is still running raises "asynchronous generator is
            # already running" (observed when a client disconnects mid-stream).
            await asyncio.gather(pending, return_exceptions=True)
        try:
            await agen.aclose()
        except RuntimeError:
            pass  # generator already torn down by the cancelled __anext__


def setup(app: web.Application) -> None:
    _setup(app)
