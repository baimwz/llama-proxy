"""Web page tests: identity model, endpoints, privacy/isolation, Rechenzeit."""
from __future__ import annotations

import asyncio
import json
import time

import aiohttp.web_request
import pytest

from conftest import chat_payload
from llama_proxy.llmclient import compute_seconds_from_timings
from llama_proxy.load import RecentLoad
from llama_proxy.power import ProxyState
from llama_proxy.routes_web import resolve_web_identity


@pytest.fixture
def lan_remote(monkeypatch):
    """Make every request look like it comes from the LAN (192.168.179.0/24)."""
    monkeypatch.setattr(
        aiohttp.web_request.BaseRequest,
        "remote",
        property(lambda self: "192.168.179.50"),
    )


async def _wait_for_usage(app, user: str, timeout: float = 5.0) -> dict:
    """Usage is recorded in finalize, which can land slightly after the
    client's response (esp. streaming: [DONE] is sent before finalize)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        totals = app["usage"].totals()
        if user in totals and totals[user]["requests"] >= 1:
            return totals[user]
        await asyncio.sleep(0.05)
    raise AssertionError(f"usage for {user!r} not recorded within {timeout}s")


def _setup_keys(app) -> None:
    app["cfg"].proxy.master_api_key = "mkey"
    app["keys"].create("falko")
    app["keys"].create("leo")


# --- identity resolution (pure function, any source IP) -------------------


def test_identity_lan_fallback(app_env):
    app, _ = app_env
    _setup_keys(app)
    ident = resolve_web_identity(app["auth"], app["cfg"], "", "192.168.179.50")
    assert ident is not None
    assert ident.name == "anton"
    assert not ident.master


def test_identity_tailnet_without_key(app_env):
    app, _ = app_env
    _setup_keys(app)
    # tailnet via serve arrives from 127.0.0.1 (loopback, not LAN prefix)
    assert resolve_web_identity(app["auth"], app["cfg"], "", "127.0.0.1") is None


def test_identity_key_wins_over_lan(app_env):
    app, _ = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, key = app["keys"].create("falko")
    # from the LAN, but with a valid key -> the key's identity wins
    ident = resolve_web_identity(app["auth"], app["cfg"], key, "192.168.179.50")
    assert ident is not None
    assert ident.name == "falko"


def test_identity_master_key(app_env):
    app, _ = app_env
    _setup_keys(app)
    ident = resolve_web_identity(app["auth"], app["cfg"], "mkey", "127.0.0.1")
    assert ident is not None
    assert ident.master
    assert ident.name == "master"


def test_identity_invalid_key_fails_closed(app_env):
    # an invalid key from the LAN must NOT fall back to the LAN identity
    app, _ = app_env
    _setup_keys(app)
    assert (
        resolve_web_identity(app["auth"], app["cfg"], "lp-wrong-key", "192.168.179.50")
        is None
    )


def test_identity_lan_prefix_disabled(app_env):
    app, _ = app_env
    _setup_keys(app)
    app["cfg"].proxy.lan_prefix = ""
    assert resolve_web_identity(app["auth"], app["cfg"], "", "192.168.179.50") is None


# --- endpoints -------------------------------------------------------------


async def test_status_open(app_env):
    app, client = app_env
    resp = await client.get("/status")
    assert resp.status == 200
    body = await resp.json()
    assert body["up"] is True
    assert body["server_state"] == "ready"
    assert body["model"]
    assert "active" in body["queue"]
    assert "queued" in body["queue"]
    assert "idle_seconds" in body


async def test_pages_served(app_env):
    app, client = app_env
    for path in ("/", "/admin"):
        resp = await client.get(path)
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        html = await resp.text()
        assert "llama-proxy" in html
        assert "login" in html.lower()
        # build-time placeholders must be replaced
        assert "__CSS__" not in html and "__STATUS_JS__" not in html


async def test_usage_me_lan_fallback(app_env, lan_remote):
    app, client = app_env
    _setup_keys(app)
    app["usage"].record("anton", 10, 5, 1, cached_tokens=2, compute_seconds=1.25)
    resp = await client.get("/usage/me")  # no key, remote = LAN
    assert resp.status == 200
    body = await resp.json()
    assert body["user"] == "anton"
    assert body["totals"]["prompt"] == 10
    assert body["totals"]["compute_seconds"] == 1.25
    assert body["daily"][0]["prompt"] == 10
    # isolation: no other user may leak
    assert "falko" not in json.dumps(body)


async def test_usage_me_tailnet_requires_key(app_env):
    app, client = app_env
    _setup_keys(app)
    resp = await client.get("/usage/me")  # remote = 127.0.0.1, no key
    assert resp.status == 401
    body = await resp.json()
    assert body["error"]["login_required"] is True


async def test_usage_me_with_key_isolation(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, falko = app["keys"].create("falko")
    app["keys"].create("leo")
    app["usage"].record("falko", 100, 50, 2, cached_tokens=10, compute_seconds=3.0)
    app["usage"].record("leo", 7, 3, 1, compute_seconds=0.5)
    resp = await client.get(
        "/usage/me?days=30", headers={"Authorization": f"Bearer {falko}"}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["user"] == "falko"
    assert body["totals"]["prompt"] == 100
    assert body["totals"]["requests"] == 2
    # hard isolation check: the whole payload must not mention the other user
    assert "leo" not in json.dumps(body)
    assert "0.5" not in json.dumps(body)


async def test_admin_usage_master_only(app_env):
    app, client = app_env
    _setup_keys(app)
    app["usage"].record("falko", 10, 5, 1, compute_seconds=1.5)
    # no key -> rejected
    resp = await client.get("/admin/usage")
    assert resp.status == 403
    resp = await client.get("/admin/usage", headers={"Authorization": "Bearer mkey"})
    assert resp.status == 200
    body = await resp.json()
    assert body["users"]["falko"]["compute_seconds"] == 1.5
    # admin page needs the per-day breakdown too
    assert body["daily"]["falko"][0]["compute_seconds"] == 1.5


async def test_admin_usage_with_user_key_rejected(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, falko = app["keys"].create("falko")
    resp = await client.get(
        "/admin/usage", headers={"Authorization": f"Bearer {falko}"}
    )
    assert resp.status == 403


# --- Rechenzeit (compute seconds) ------------------------------------------


def test_compute_seconds_from_timings_helper():
    assert compute_seconds_from_timings({"prompt_ms": 100, "predicted_ms": 200}) == pytest.approx(0.3)
    assert compute_seconds_from_timings({"prompt_ms": 0, "predicted_ms": 0}) == 0.0
    assert compute_seconds_from_timings(None) is None
    assert compute_seconds_from_timings("garbage") is None
    assert compute_seconds_from_timings({"prompt_ms": "x"}) is None


async def test_compute_seconds_recorded_non_stream(app_env):
    # mock LLM reports timings: 100 ms prompt + 200 ms predicted = 0.3 s
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, alice = app["keys"].create("alice")
    resp = await client.post(
        "/v1/chat/completions", json=chat_payload(),
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert resp.status == 200
    totals = await _wait_for_usage(app, "alice")
    assert totals["compute_seconds"] == pytest.approx(0.3)


async def test_compute_seconds_recorded_stream(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, alice = app["keys"].create("alice")
    resp = await client.post(
        "/v1/chat/completions", json=chat_payload(stream=True),
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert resp.status == 200
    await resp.read()  # drain the full SSE stream
    totals = await _wait_for_usage(app, "alice")
    assert totals["compute_seconds"] == pytest.approx(0.3)


async def test_stream_client_disconnect_cleans_up(app_env, caplog):
    """Client goes away mid-stream: the server must cancel the job without
    an unhandled 'aclose(): asynchronous generator is already running'."""
    import logging

    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, alice = app["keys"].create("alice")
    with caplog.at_level(logging.ERROR, logger="aiohttp.server"):
        resp = await client.post(
            "/v1/chat/completions", json=chat_payload(stream=True),
            headers={"Authorization": f"Bearer {alice}"},
        )
        assert resp.status == 200
        first = await resp.content.read(64)
        assert first
        resp.close()  # disconnect mid-stream
        await asyncio.sleep(0.3)  # let the server process the disconnect
    # job reached a terminal state (cancelled or done) - no stuck RUNNING job
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and app["queue"].active():
        await asyncio.sleep(0.05)
    assert not app["queue"].active()
    assert "already running" not in caplog.text
    assert "RuntimeError" not in caplog.text


async def test_daily_shape(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    app["usage"].record("anton", 1, 1, 1, compute_seconds=0.5)
    resp = await client.get("/admin/usage?days=7", headers={"Authorization": "Bearer mkey"})
    body = await resp.json()
    row = body["daily"]["anton"][0]
    for field in ("day", "prompt", "completion", "cached", "requests", "compute_seconds"):
        assert field in row


# --- recent load (20-min window) --------------------------------------------


def test_load_snapshot_aggregation():
    rl = RecentLoad()
    now = 1_000_000.0
    rl.record("anton", 30.0, now=now)
    rl.record("anton", 10.0, now=now)
    rl.record("falko", 20.0, now=now)
    rl.record("ghost", 500.0, now=now - 7200)  # outside any valid window
    s = rl.snapshot(1200, now=now)
    assert s["busy_seconds"] == pytest.approx(60.0)
    assert s["jobs"] == 3
    assert s["utilization_pct"] == pytest.approx(5.0)  # 60 s / 1200 s
    assert [u["name"] for u in s["users"]] == ["anton", "falko"]
    assert s["users"][0]["pct"] == pytest.approx(66.7)
    assert s["users"][0]["jobs"] == 2
    assert s["users"][1]["pct"] == pytest.approx(33.3)


def test_load_snapshot_empty_and_window_filter():
    rl = RecentLoad()
    now = 2_000_000.0
    assert rl.snapshot(1200, now=now)["users"] == []
    rl.record("anton", 12.0, now=now - 1500)  # older than the 20-min window
    s = rl.snapshot(1200, now=now)
    assert s["jobs"] == 0 and s["busy_seconds"] == 0.0


def test_load_snapshot_utilization_capped():
    rl = RecentLoad()
    now = 3_000_000.0
    rl.record("anton", 2000.0, now=now)  # busy > window => cap at 100 %
    assert rl.snapshot(1200, now=now)["utilization_pct"] == 100.0


async def test_usage_recent_endpoint(app_env):
    app, client = app_env
    resp = await client.get("/usage/recent")  # open, no key needed
    assert resp.status == 200
    assert (await resp.json())["users"] == []
    app["load"].record("falko", 30.0)
    resp = await client.get("/usage/recent")
    body = await resp.json()
    assert body["busy_seconds"] == pytest.approx(30.0)
    assert body["jobs"] == 1
    assert body["users"][0]["name"] == "falko"
    assert body["users"][0]["pct"] == pytest.approx(100.0)


async def test_load_recorded_on_finished_job(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, alice = app["keys"].create("alice")
    resp = await client.post(
        "/v1/chat/completions", json=chat_payload(),
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert resp.status == 200
    await _wait_for_usage(app, "alice")
    s = app["load"].snapshot(1200)
    assert s["jobs"] == 1
    assert s["users"][0]["name"] == "alice"
    assert s["users"][0]["busy_seconds"] == pytest.approx(0.3)


# --- shutdown countdown ------------------------------------------------------


async def test_status_countdown_fields(app_env):
    app, client = app_env
    timeout = app["cfg"].proxy.idle_timeout_s
    body = await (await client.get("/status")).json()
    assert body["idle_timeout_s"] == timeout
    assert 0 <= body["shutdown_in_s"] <= timeout
    # simulate 300 s of idleness -> the countdown must shrink accordingly
    app["power"].note_activity()
    app["power"]._last_activity = time.time() - 300
    body = await (await client.get("/status")).json()
    assert body["shutdown_in_s"] == pytest.approx(timeout - 300, abs=2.0)


# --- wake button (POST /wake) -------------------------------------------------


async def test_wake_noop_when_ready(app_env):
    app, client = app_env
    assert app["power"].state is ProxyState.READY  # mock LLM is healthy
    resp = await client.post("/wake")
    assert resp.status == 200
    body = await resp.json()
    assert body["waking"] is False
    assert app["power"].state is ProxyState.READY


async def test_wake_starts_and_completes(app_env):
    app, client = app_env
    calls: list[int] = []

    async def fake_wol() -> None:
        calls.append(1)

    app["power"].set_backends(power_on=fake_wol)
    app["power"].state = ProxyState.OFF  # simulate a powered-off server
    resp = await client.post("/wake")
    assert resp.status == 200
    body = await resp.json()
    assert body["waking"] is True
    # the mock LLM is healthy -> the wake task reaches READY right away
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and app["power"].state is not ProxyState.READY:
        await asyncio.sleep(0.05)
    assert app["power"].state is ProxyState.READY
    assert calls  # the WoL magic packet was actually sent


async def test_wake_click_twice_starts_single_wake(app_env):
    app, client = app_env

    async def slow_wol() -> None:
        await asyncio.sleep(2.0)

    app["power"].set_backends(power_on=slow_wol)
    app["power"].state = ProxyState.OFF
    r1 = await client.post("/wake")
    assert (await r1.json())["waking"] is True
    first_task = app["power"]._wake_task
    r2 = await client.post("/wake")  # second click while in flight
    assert (await r2.json())["waking"] is True
    assert app["power"]._wake_task is first_task
