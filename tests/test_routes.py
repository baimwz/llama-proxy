from conftest import chat_payload


async def test_healthz(app_env):
    app, client = app_env
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"


async def test_models(app_env):
    app, client = app_env
    resp = await client.get("/v1/models")
    assert resp.status == 200
    body = await resp.json()
    assert body["data"][0]["id"] == app["cfg"].llm.model_name


async def test_auth_401(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "sekret"
    resp = await client.post("/v1/chat/completions", json=chat_payload())
    assert resp.status == 401
    resp = await client.post(
        "/v1/chat/completions",
        json=chat_payload(),
        headers={"Authorization": "Bearer sekret"},
    )
    assert resp.status == 200


async def test_invalid_json(app_env):
    app, client = app_env
    resp = await client.post(
        "/v1/chat/completions", data=b"not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400


async def test_chat_nonstream(app_env):
    app, client = app_env
    resp = await client.post("/v1/chat/completions", json=chat_payload())
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"]
    assert body["job_id"]
    usage = app["usage"].totals()
    assert usage["default"]["prompt"] >= 100
    assert usage["default"]["requests"] >= 1


def _replay_stream_content(text: str) -> str:
    import json as _json

    parts = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            obj = _json.loads(line[6:])
        except ValueError:
            continue
        choices = obj.get("choices") or [{}]
        delta = (choices[0] or {}).get("delta") or {}
        if isinstance(delta.get("content"), str):
            parts.append(delta["content"])
    return "".join(parts)


async def test_chat_stream(app_env):
    app, client = app_env
    resp = await client.post("/v1/chat/completions", json=chat_payload(stream=True))
    assert resp.status == 200
    assert "text/event-stream" in resp.headers.get("Content-Type", "")
    text = await resp.text()
    assert "data: [DONE]" in text
    # content arrives in chunks; replaying the deltas must give the full reply
    assert _replay_stream_content(text) == "mock reply from the test llm "
    # job must be done with recorded usage
    done = [j for j in app["queue"].all_jobs() if j.state.value == "done"]
    assert done, "expected at least one done job"
    assert done[0].result["usage"]["completion_tokens"] == 6


async def test_202_when_server_off(app_env):
    app, client = app_env
    from llama_proxy.power import ProxyState

    app["mock_llm"].ready = False
    app["power"].state = ProxyState.OFF
    resp = await client.post("/v1/chat/completions", json=chat_payload())
    assert resp.status == 202
    body = await resp.json()
    assert "job_id" in body and body["poll"].startswith("/v1/jobs/")
    assert body["state"] == "off"


async def test_job_404(app_env):
    app, client = app_env
    resp = await client.get("/v1/jobs/doesnotexist")
    assert resp.status == 404


async def test_queue_view(app_env):
    app, client = app_env
    resp = await client.get("/v1/queue")
    assert resp.status == 200
    body = await resp.json()
    assert body["server_state"] in ("off", "waking", "ready", "shutting_down")
    assert "active" in body and "queued" in body


async def test_metrics_endpoint(app_env):
    app, client = app_env
    resp = await client.get("/metrics")
    assert resp.status == 200
    text = await resp.text()
    assert "llama_proxy_state" in text
    assert "llama_proxy_requests_total" in text


async def test_ui(app_env):
    app, client = app_env
    resp = await client.get("/")
    assert resp.status == 200
    assert "llama-proxy" in await resp.text()


async def test_completions_endpoint(app_env):
    """The /v1/completions route exists (mock only implements chat; expect 502 upstream)."""
    app, client = app_env
    resp = await client.post("/v1/completions", json={"prompt": "hi"})
    assert resp.status in (200, 502)


async def test_user_key_auth(app_env):
    """Once keys are configured, every /v1/* request needs a valid key."""
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, key = app["keys"].create("alice")

    resp = await client.post("/v1/chat/completions", json=chat_payload())
    assert resp.status == 401
    resp = await client.get("/v1/models")
    assert resp.status == 401

    resp = await client.post(
        "/v1/chat/completions",
        json=chat_payload(),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status == 200
    assert "alice" in app["usage"].totals()  # usage recorded under the user


async def test_rate_limit_429(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, key = app["keys"].create("alice", requests_per_hour=2)
    h = {"Authorization": f"Bearer {key}"}

    assert (await client.post("/v1/chat/completions", json=chat_payload(), headers=h)).status == 200
    assert (await client.post("/v1/chat/completions", json=chat_payload(), headers=h)).status == 200
    resp = await client.post("/v1/chat/completions", json=chat_payload(), headers=h)
    assert resp.status == 429
    assert int(resp.headers["Retry-After"]) >= 1
    body = await resp.json()
    assert body["error"]["type"] == "rate_limit_exceeded"

    # other users and the master key are unaffected
    _, other = app["keys"].create("bob", requests_per_hour=2)
    resp = await client.post(
        "/v1/chat/completions",
        json=chat_payload(),
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status == 200
    resp = await client.post(
        "/v1/chat/completions",
        json=chat_payload(),
        headers={"Authorization": "Bearer mkey"},
    )
    assert resp.status == 200


async def test_user_priority_and_cap(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, key = app["keys"].create("alice", priority=7, max_priority=8)
    h = {"Authorization": f"Bearer {key}"}

    resp = await client.post("/v1/chat/completions", json=chat_payload(), headers=h)
    assert resp.status == 200
    jobs = [j for j in app["queue"].all_jobs() if j.user == "alice"]
    assert jobs[-1].priority == 7  # key default applies

    resp = await client.post(
        "/v1/chat/completions", json=chat_payload(), headers={**h, "X-Priority": "10"}
    )
    assert resp.status == 200
    jobs = [j for j in app["queue"].all_jobs() if j.user == "alice"]
    assert jobs[-1].priority == 8  # header clamped to the key's max


async def test_tokens_per_day_limit(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    _, key = app["keys"].create("alice", tokens_per_day=10)
    h = {"Authorization": f"Bearer {key}"}

    # pre-check passes (estimate ~4 tokens < 10); the mock then reports 100+
    resp = await client.post("/v1/chat/completions", json=chat_payload(), headers=h)
    assert resp.status == 200
    # usage (100 prompt + 6 completion) now exceeds the 10/day budget
    resp = await client.post("/v1/chat/completions", json=chat_payload(), headers=h)
    assert resp.status == 429
    assert (await resp.json())["error"]["type"] == "rate_limit_exceeded"
