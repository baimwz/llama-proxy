"""Admin API (phase 2): manage user keys and read usage.

All endpoints require the master API key.
"""
from __future__ import annotations

import json

from aiohttp import web

from .keys import KeyStore
from .routes_openai import err


def _master(request: web.Request) -> bool:
    app = request.app
    cfg = app["cfg"]
    ident = app["auth"].resolve(request, cfg.proxy.master_api_key)
    return ident is not None and ident.master


def _keys(request: web.Request) -> KeyStore:
    return request.app["keys"]


async def keys_create(request: web.Request) -> web.Response:
    if not _master(request):
        return err("master API key required", 403)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return err("body must be valid JSON", 400)
    if not isinstance(body, dict):
        return err("body must be a JSON object", 400)
    name = str(body.get("name", "")).strip()
    if not name:
        return err("field 'name' is required", 400)
    try:
        record, plaintext = _keys(request).create(
            name,
            priority=body.get("priority"),
            max_priority=body.get("max_priority"),
            requests_per_hour=body.get("requests_per_hour"),
            tokens_per_day=body.get("tokens_per_day"),
        )
    except ValueError as exc:
        return err(str(exc), 400)
    return web.json_response(
        {
            "name": record.name,
            "key": plaintext,  # shown exactly once
            "prefix": record.prefix,
            "priority": record.priority,
            "max_priority": record.max_priority,
            "requests_per_hour": record.requests_per_hour,
            "tokens_per_day": record.tokens_per_day,
        },
        status=201,
    )


async def keys_list(_request: web.Request) -> web.Response:
    if not _master(_request):
        return err("master API key required", 403)
    return web.json_response({"users": [vars(u) for u in _keys(_request).list()]})


async def keys_update(request: web.Request) -> web.Response:
    if not _master(request):
        return err("master API key required", 403)
    name = request.match_info["name"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return err("body must be valid JSON", 400)
    if not isinstance(body, dict):
        return err("body must be a JSON object", 400)
    allowed = KeyStore.UPDATABLE
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        return err(f"no updatable fields given (allowed: {', '.join(allowed)})", 400)
    try:
        record = _keys(request).update(name, **fields)
    except KeyError:
        return err(f"unknown user {name!r}", 404)
    except ValueError as exc:
        return err(str(exc), 400)
    return web.json_response(vars(record))


async def keys_remove(request: web.Request) -> web.Response:
    if not _master(request):
        return err("master API key required", 403)
    name = request.match_info["name"]
    try:
        _keys(request).remove(name)
    except KeyError:
        return err(f"unknown user {name!r}", 404)
    return web.Response(status=204)


async def wake_server(request: web.Request) -> web.Response:
    """Wake the LLM server on demand (master key) - handy for WoL testing."""
    if not _master(request):
        return err("master API key required", 403)
    power = request.app["power"]
    if power.state.value == "ready":
        return web.json_response({"state": power.state.value, "boot_seconds": None})
    try:
        await power.ensure_ready()
    except Exception as exc:  # noqa: BLE001
        return err(f"wake failed: {exc}", 503)
    return web.json_response(
        {"state": power.state.value, "boot_seconds": power.last_boot_s}
    )


async def usage_view(request: web.Request) -> web.Response:
    if not _master(request):
        return err("master API key required", 403)
    try:
        days = max(1, min(365, int(request.query.get("days", "7"))))
    except ValueError:
        days = 7
    totals = request.app["usage"].totals(days=days)
    return web.json_response(
        {
            "days": days,
            "users": totals,
            "daily": request.app["usage"].daily(days=days),
            # today's server-on time split summed over ALL users
            # (distribution bar on the admin page)
            "today": request.app["usage"].today_phase_seconds(),
        }
    )


def setup(app: web.Application) -> None:
    app.router.add_post("/admin/keys", keys_create)
    app.router.add_get("/admin/keys", keys_list)
    app.router.add_patch("/admin/keys/{name}", keys_update)
    app.router.add_delete("/admin/keys/{name}", keys_remove)
    app.router.add_get("/admin/usage", usage_view)
    app.router.add_post("/admin/wake", wake_server)
