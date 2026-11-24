"""Ops routes: health, server state, Prometheus metrics, minimal HTML UI."""
from __future__ import annotations

from aiohttp import web


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "llama-proxy"})


async def server_state(request: web.Request) -> web.Response:
    app = request.app
    return web.json_response(
        {
            "state": app["power"].state.value,
            "idle_seconds": round(app["power"].idle_seconds, 1),
            "last_boot_s": app["power"].last_boot_s,
            "queue": {"active": len(app["queue"].active()), "queued": app["queue"].pending_count()},
        }
    )


async def metrics(request: web.Request) -> web.Response:
    text = request.app["metrics"].render()
    resp = web.Response(text=text, content_type="text/plain", charset="utf-8")
    resp.headers["Content-Type"] = "text/plain; version=0.0.4; charset=utf-8"
    return resp


_UI = """<!doctype html>
<html><head><meta charset="utf-8"><title>llama-proxy</title>
<meta http-equiv="refresh" content="15">
<style>
body{{font-family:system-ui,sans-serif;margin:2rem auto;max-width:760px;color:#222}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}
td,th{{padding:.4rem .6rem;text-align:left;border-bottom:1px solid #e0e0e0}}
th{{width:180px;color:#555}}
.state-ready{{color:#0a0;font-weight:600}} .state-off{{color:#a33;font-weight:600}}
code{{background:#f2f2f2;padding:.1rem .3rem}}
</style></head>
<body>
<h1>llama-proxy</h1>
<table>
<tr><th>LLM server</th><td class="state-{state}">{state}</td></tr>
<tr><th>Idle for</th><td>{idle:.0f} s (timeout {idle_timeout:.0f} s)</td></tr>
<tr><th>Active jobs</th><td>{active} (large: {active_large})</td></tr>
<tr><th>Queued jobs</th><td>{queued}</td></tr>
<tr><th>Tokenizer</th><td>{tokenizer}{tok_err}</td></tr>
<tr><th>Usage (7 days)</th><td>{usage}</td></tr>
<tr><th>Classify</th><td>new tokens &ge; {threshold} &rarr; large (exclusive: {exclusive})</td></tr>
</table>
<p>
 <a href="/metrics"><code>/metrics</code></a> &middot;
 <a href="/healthz"><code>/healthz</code></a> &middot;
 <a href="/v1/queue"><code>/v1/queue</code></a>
</p>
</body></html>
"""


async def ui(request: web.Request) -> web.Response:
    app = request.app
    active = app["queue"].active()
    active_large = sum(1 for j in active if j.is_large)
    usage = app["usage"].totals(days=7)
    usage_bits = []
    for user, row in usage.items():
        usage_bits.append(
            f"{user}: {row['prompt']} prompt + {row['completion']} completion "
            f"tokens over {row['requests']} requests"
        )
    usage_html = ", ".join(usage_bits) or "no data yet"
    counter = app["counter"]
    if counter.backend != "huggingface":
        tok_err = f" <em>({counter.load_error or 'see logs'})</em>"
    else:
        tok_err = ""
    html = _UI.format(
        state=app["power"].state.value,
        idle=app["power"].idle_seconds,
        idle_timeout=app["cfg"].proxy.idle_timeout_s,
        active=len(active),
        active_large=active_large,
        queued=app["queue"].pending_count(),
        tokenizer=counter.backend,
        tok_err=tok_err,
        usage=usage_html,
        threshold=app["cfg"].scheduler.large_threshold_tokens,
        exclusive="yes" if app["cfg"].scheduler.large_exclusive else "no",
    )
    return web.Response(text=html, content_type="text/html")


def setup(app: web.Application) -> None:
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/server", server_state)
    app.router.add_get("/metrics", metrics)
    # NOTE: "/" is the new personal usage page (routes_web); this ops UI is
    # kept at /ui only.
    app.router.add_get("/ui", ui)
