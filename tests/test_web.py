"""Web page tests: identity model, endpoints, privacy/isolation, Rechenzeit."""
from __future__ import annotations

import asyncio
import json
import time

import aiohttp.web_request
import pytest

from conftest import chat_payload
from llama_proxy.llmclient import compute_seconds_from_timings, phase_seconds_from_timings
from llama_proxy.load import RecentLoad
from llama_proxy.power import ProxyState
from llama_proxy.routes_web import resolve_web_identity
from llama_proxy.state import Job, JobState


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
        # prompt column shows FRESH tokens (prompt - cached), cached separate
        assert "Prompt (neu)" in html
        assert "freshPrompt" in html
        # distribution bar (today's server-on time) on both pages
        for el in ("dist-card", "dist-track", "dist-prefill", "dist-generation",
                   "dist-idle", "dist-legend"):
            assert f'id="{el}"' in html


async def test_usage_me_lan_fallback(app_env, lan_remote):
    app, client = app_env
    _setup_keys(app)
    app["usage"].record("anton", 10, 5, 1, cached_tokens=2, compute_seconds=1.25,
                        prefill_seconds=0.25, generation_seconds=1.0, idle_seconds=120)
    resp = await client.get("/usage/me")  # no key, remote = LAN
    assert resp.status == 200
    body = await resp.json()
    assert body["user"] == "anton"
    assert body["totals"]["prompt"] == 10
    assert body["totals"]["compute_seconds"] == 1.25
    assert body["daily"][0]["prompt"] == 10
    # distribution bar: today's server-on time for this user only
    assert body["today"] == {
        "prefill_seconds": 0.25, "generation_seconds": 1.0, "idle_seconds": 120.0,
    }
    # isolation: no other user may leak
    assert "falko" not in json.dumps(body)


async def test_admin_usage_today_bar_sums_all_users(app_env):
    # the admin distribution bar gets today's server-on time summed over
    # ALL users (Anton: "Ja, nur gesamt")
    app, client = app_env
    _setup_keys(app)
    app["usage"].record("falko", 10, 5, 1, prefill_seconds=0.5,
                        generation_seconds=1.0, idle_seconds=60)
    app["usage"].record("leo", 2, 1, 1, prefill_seconds=0.1,
                        generation_seconds=0.2, idle_seconds=30)
    resp = await client.get("/admin/usage", headers={"Authorization": "Bearer mkey"})
    assert resp.status == 200
    body = await resp.json()
    assert body["today"]["prefill_seconds"] == pytest.approx(0.6)
    assert body["today"]["generation_seconds"] == pytest.approx(1.2)
    assert body["today"]["idle_seconds"] == pytest.approx(90.0)


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


def test_phase_seconds_from_timings_helper():
    # prompt_ms -> prefill, predicted_ms -> generation
    assert phase_seconds_from_timings({"prompt_ms": 100, "predicted_ms": 200}) == (0.1, 0.2)
    assert phase_seconds_from_timings({"prompt_ms": 0, "predicted_ms": 0}) == (0.0, 0.0)
    assert phase_seconds_from_timings(None) is None
    assert phase_seconds_from_timings("garbage") is None
    assert phase_seconds_from_timings({"prompt_ms": "x"}) is None
    # the sum must equal the old total
    p, g = phase_seconds_from_timings({"prompt_ms": 100, "predicted_ms": 200})
    assert p + g == pytest.approx(compute_seconds_from_timings(
        {"prompt_ms": 100, "predicted_ms": 200}
    ))


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
    for field in ("day", "prompt", "completion", "cached", "requests",
                  "compute_seconds", "prefill_seconds", "generation_seconds",
                  "idle_seconds", "energy_wh"):
        assert field in row


# --- recent load (12 h graph) -------------------------------------------------


def test_load_graph_aggregation():
    rl = RecentLoad()
    bucket = 300.0
    now = 1_000_000.0
    boundary = (int(now) // bucket + 1) * bucket
    rl.record("anton", 30.0, now=now)                  # current bucket
    rl.record("falko", 60.0, now=boundary - bucket - 1)  # previous bucket
    rl.record("leo", 90.0, now=boundary - 2 * bucket - 1)  # outside the window
    g = rl.graph(2 * bucket, bucket, now=now)
    assert g["window_s"] == 2 * bucket
    assert g["bucket_s"] == bucket
    assert len(g["buckets"]) == 2
    assert g["buckets"][0]["busy_seconds"] == pytest.approx(60.0)
    assert g["buckets"][1]["busy_seconds"] == pytest.approx(30.0)
    assert g["busy_seconds"] == pytest.approx(90.0)
    assert g["jobs"] == 2
    assert g["buckets"][0]["utilization_pct"] == pytest.approx(20.0)  # 60/300
    assert g["buckets"][1]["utilization_pct"] == pytest.approx(10.0)  # 30/300
    # bucket boundaries are stable between polls (aligned to the bucket size)
    for b in g["buckets"]:
        assert b["start"] % bucket == 0
    # total only: no per-user breakdown in the public graph
    assert "users" not in g


def test_load_graph_empty_and_window_filter():
    rl = RecentLoad()
    now = 2_000_000.0
    g = rl.graph(43200, 300, now=now)
    assert g["busy_seconds"] == 0.0 and g["jobs"] == 0
    rl.record("anton", 12.0, now=now - 1500)
    g = rl.graph(600, 300, now=now)   # 10-min window: the job is too old
    assert g["jobs"] == 0 and g["busy_seconds"] == 0.0
    g = rl.graph(1800, 300, now=now)  # 30-min window: the job counts
    assert g["jobs"] == 1 and g["busy_seconds"] == pytest.approx(12.0)


def test_load_graph_utilization_capped():
    rl = RecentLoad()
    now = 3_000_000.0
    rl.record("anton", 300.0, now=now)  # one full 5-min bucket of busy time
    g = rl.graph(300, 300, now=now)
    assert g["buckets"][-1]["utilization_pct"] == 100.0
    assert g["utilization_pct"] == 100.0


def test_load_graph_prunes_beyond_retention():
    rl = RecentLoad()
    now = 4_000_000.0
    rl.record("anton", 30.0, now=now - 13 * 3600)  # older than the 12 h retention
    g = rl.graph(43200, 300, now=now)
    assert g["busy_seconds"] == 0.0
    assert g["jobs"] == 0


async def test_usage_recent_endpoint(app_env):
    app, client = app_env
    resp = await client.get("/usage/recent")  # open, no key needed
    assert resp.status == 200
    body = await resp.json()
    n = body["window_s"] // body["bucket_s"]
    assert len(body["buckets"]) == n
    assert body["busy_seconds"] == 0.0
    assert "users" not in body  # total only: identical for everyone
    app["load"].record("falko", 30.0)
    body = await (await client.get("/usage/recent")).json()
    assert body["busy_seconds"] == pytest.approx(30.0)
    assert body["jobs"] == 1
    last = body["buckets"][-1]
    assert last["busy_seconds"] == pytest.approx(30.0)
    assert last["utilization_pct"] == pytest.approx(10.0)  # 30 s / 300 s bucket


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
    g = app["load"].graph(43200, 300)
    assert g["jobs"] == 1
    assert g["busy_seconds"] == pytest.approx(0.3)
    assert g["buckets"][-1]["busy_seconds"] == pytest.approx(0.3)


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


async def test_status_busy_shows_zero_idle_and_full_countdown(app_env):
    # Anton: while a job runs, idle must stay 0:00 and the countdown 20:00
    app, client = app_env
    timeout = app["cfg"].proxy.idle_timeout_s
    app["power"]._last_activity = time.time() - 300  # old activity
    job = Job(id=None, kind="chat", payload={}, prompt_text="x", user="anton")
    app["queue"].add(job)
    job.state = JobState.RUNNING
    app["queue"]._in_flight.add(job.id)
    try:
        body = await (await client.get("/status")).json()
        assert body["busy"] is True
        assert body["idle_seconds"] == 0
        assert body["shutdown_in_s"] == timeout
    finally:
        app["queue"]._in_flight.discard(job.id)
    # not busy anymore -> idle counting resumes from the old activity
    body = await (await client.get("/status")).json()
    assert body["busy"] is False
    assert body["idle_seconds"] == pytest.approx(300, abs=2.0)
    assert body["shutdown_in_s"] == pytest.approx(timeout - 300, abs=2.0)


async def test_idle_gap_even_split_recorded_in_usage(app_env):
    # Alice's job ended, the server idles ~2 min, then Bob's job starts:
    # the idle gap must be split 50/50 and booked with idle energy (80 W).
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, alice = app["keys"].create("alice")
    _, bob = app["keys"].create("bob")
    # keep the watchdog from touching the gap state while we set it up
    watchdog = next(t for t in app["tasks"] if t.get_name() == "idle-watchdog")
    watchdog.cancel()
    app["power"].note_user_activity("alice")
    app["power"]._last_activity = time.time() - 120  # 2 min of idleness
    app["power"].observe_idle(False)                  # gap opens
    resp = await client.post(
        "/v1/chat/completions", json=chat_payload(),
        headers={"Authorization": f"Bearer {bob}"},
    )
    assert resp.status == 200
    await _wait_for_usage(app, "bob")
    totals = app["usage"].totals()
    assert totals["alice"]["idle_seconds"] == pytest.approx(60, abs=5)
    assert totals["bob"]["idle_seconds"] == pytest.approx(60, abs=5)
    # idle energy: 60 s * 80 W / 3600 = 1.33 Wh each
    assert totals["alice"]["energy_wh"] == pytest.approx(60 * 80 / 3600, rel=0.1)
    assert totals["alice"]["requests"] == 0  # idle time is not a request
    assert totals["alice"]["prompt"] == 0


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


async def test_reset_idle_postpones_shutdown(app_env):
    app, client = app_env
    timeout = app["cfg"].proxy.idle_timeout_s
    # simulate 15 minutes of idleness
    app["power"].note_activity()
    app["power"]._last_activity = time.time() - 900
    assert app["power"].idle_seconds > 800
    resp = await client.post("/reset-idle")
    assert resp.status == 200
    body = await resp.json()
    assert body["idle_seconds"] == 0.0
    assert body["shutdown_in_s"] == timeout
    assert "20:00" in body["message"]  # 1200 s default -> MM:SS in the feedback
    assert app["power"].idle_seconds < 5
    # /status agrees: full countdown again
    body = await (await client.get("/status")).json()
    assert body["shutdown_in_s"] == pytest.approx(timeout, abs=2.0)


# --- stop button (POST /stop) ----------------------------------------------


async def test_stop_requires_identity(app_env):
    app, client = app_env
    _setup_keys(app)
    resp = await client.post("/stop")  # no key, remote is loopback (tailnet-like)
    assert resp.status == 401
    body = await resp.json()
    assert body["error"]["login_required"] is True
    assert not app["power"].stop_pending


async def test_stop_lan_fallback_waits_for_falko(app_env, lan_remote):
    app, client = app_env
    _setup_keys(app)
    app["power"].note_user_activity("anton")
    app["power"]._user_activity["falko"] = time.time()  # falko active within the window
    resp = await client.post("/stop")  # no key, LAN -> identity "anton"
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["immediate"] is False
    assert body["waiting_for"] == ["falko"]
    assert "falko" in body["message"]
    assert app["power"].stop_pending


async def test_stop_all_consented_is_immediate(app_env, lan_remote):
    app, client = app_env
    _setup_keys(app)
    app["power"].note_user_activity("anton")
    resp = await client.post("/stop")
    body = await resp.json()
    assert body["ok"] is True
    assert body["immediate"] is True
    assert body["waiting_for"] == []
    assert app["power"].stop_pending


async def test_stop_with_master_key(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    app["keys"].create("falko")
    app["power"].note_user_activity("master")
    resp = await client.post("/stop", headers={"Authorization": "Bearer mkey"})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["waiting_for"] == []
    assert app["power"].stop_pending


async def test_status_exposes_stop_fields(app_env, lan_remote):
    app, client = app_env
    _setup_keys(app)
    body = await (await client.get("/status")).json()
    assert body["stop_pending"] is False
    assert body["stop_waiting_for"] == []
    app["power"].note_user_activity("anton")
    app["power"]._user_activity["falko"] = time.time()
    await client.post("/stop")  # anton consents, falko does not
    body = await (await client.get("/status")).json()
    assert body["stop_pending"] is True
    assert body["stop_waiting_for"] == ["falko"]


async def test_stop_noop_when_server_off(app_env, lan_remote):
    app, client = app_env
    _setup_keys(app)
    app["power"].state = ProxyState.OFF
    resp = await client.post("/stop")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["immediate"] is False
    assert not app["power"].stop_pending
