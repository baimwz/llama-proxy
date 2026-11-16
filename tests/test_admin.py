from conftest import chat_payload


async def test_admin_requires_master(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    resp = await client.get("/admin/keys")
    assert resp.status == 403
    # a regular user key is not enough either
    _, key = app["keys"].create("alice")
    resp = await client.get("/admin/keys", headers={"Authorization": f"Bearer {key}"})
    assert resp.status == 403


async def test_admin_create_list_update_delete(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    h = {"Authorization": "Bearer mkey"}

    resp = await client.post(
        "/admin/keys", json={"name": "alice", "priority": 5}, headers=h
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["name"] == "alice"
    assert body["priority"] == 5
    assert body["key"].startswith("lp-")

    resp = await client.get("/admin/keys", headers=h)
    assert resp.status == 200
    users = (await resp.json())["users"]
    assert [u["name"] for u in users] == ["alice"]

    resp = await client.patch("/admin/keys/alice", json={"disabled": True}, headers=h)
    assert resp.status == 200
    assert (await resp.json())["disabled"] is True

    resp = await client.delete("/admin/keys/alice", headers=h)
    assert resp.status == 204

    resp = await client.get("/admin/keys", headers=h)
    assert (await resp.json())["users"] == []

    resp = await client.delete("/admin/keys/ghost", headers=h)
    assert resp.status == 404


async def test_admin_create_validation(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    h = {"Authorization": "Bearer mkey"}
    resp = await client.post("/admin/keys", json={}, headers=h)
    assert resp.status == 400
    resp = await client.post("/admin/keys", json={"name": "BAD NAME"}, headers=h)
    assert resp.status == 400
    resp = await client.post("/admin/keys", json={"name": "alice"}, headers=h)
    assert resp.status == 201
    resp = await client.post("/admin/keys", json={"name": "alice"}, headers=h)
    assert resp.status == 400


async def test_admin_usage_endpoint(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    h = {"Authorization": "Bearer mkey"}
    resp = await client.get("/admin/usage", headers=h)
    assert resp.status == 200
    body = await resp.json()
    assert body["days"] == 7
    assert isinstance(body["users"], dict)


async def test_user_key_lifecycle(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"
    h = {"Authorization": "Bearer mkey"}
    resp = await client.post("/admin/keys", json={"name": "alice"}, headers=h)
    key = (await resp.json())["key"]
    uh = {"Authorization": f"Bearer {key}"}

    # works
    resp = await client.post("/v1/chat/completions", json=chat_payload(), headers=uh)
    assert resp.status == 200
    assert "alice" in app["usage"].totals()

    # disabled -> 401
    await client.patch("/admin/keys/alice", json={"disabled": True}, headers=h)
    resp = await client.post("/v1/chat/completions", json=chat_payload(), headers=uh)
    assert resp.status == 401

    # enabled again -> works
    await client.patch("/admin/keys/alice", json={"disabled": False}, headers=h)
    resp = await client.post("/v1/chat/completions", json=chat_payload(), headers=uh)
    assert resp.status == 200


async def test_admin_wake_endpoint(app_env):
    app, client = app_env
    app["cfg"].proxy.master_api_key = "mkey"

    # not master -> 403
    resp = await client.post("/admin/wake")
    assert resp.status == 403

    # server already ready -> instant, no boot seconds
    h = {"Authorization": "Bearer mkey"}
    resp = await client.post("/admin/wake", headers=h)
    assert resp.status == 200
    body = await resp.json()
    assert body["state"] == "ready"

    # server off -> wake path (mock is healthy, so this just polls health)
    from llama_proxy.power import ProxyState

    app["power"].state = ProxyState.OFF
    resp = await client.post("/admin/wake", headers=h)
    assert resp.status == 200
    body = await resp.json()
    assert body["state"] == "ready"
    assert body["boot_seconds"] is not None
