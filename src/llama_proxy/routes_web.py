"""Web page (phase 3): personal usage page, admin overview, live status.

Identity model (final, per source IP + key):

  * request carries a valid key (master or user)  -> that identity
  * no key, source IP within ``proxy.lan_prefix`` -> ``proxy.lan_fallback_user``
  * otherwise (e.g. tailnet via ``tailscale serve``) -> no identity; the
    browser stores a key in localStorage and presents it on the next request

The HTML pages are static, self-contained (no framework, no CDN). All data
is fetched client-side from the JSON endpoints in this module.
"""
from __future__ import annotations

from aiohttp import web

from .auth import Auth, Identity, token_from_header
from .config import Config
from .power import ProxyState
from .routes_openai import err
from .timeutil import now_local

_KEY_HEADER = "Authorization"


def resolve_web_identity(
    auth: Auth, cfg: Config, token: str, remote: str | None
) -> Identity | None:
    """Resolve the caller of a web request.

    A key always wins. Without a key, requests coming from the configured
    LAN prefix fall back to the fallback user (usually "anton"); anything
    else (tailnet via serve, other networks) gets no identity and must log
    in with a browser-stored key. An *invalid* key never falls back to the
    LAN identity (fail closed).
    """
    if token:
        ident = auth.resolve_token(token, cfg.proxy.master_api_key)
        if ident is None:
            return None
        return ident
    prefix = cfg.proxy.lan_prefix
    if prefix and remote and remote.startswith(prefix):
        return Identity(name=cfg.proxy.lan_fallback_user)
    return None


def _clamp_days(request: web.Request, default: int = 30) -> int:
    try:
        return max(1, min(365, int(request.query.get("days", str(default)))))
    except ValueError:
        return default


def _mmss(total_s: float) -> str:
    t = max(0, int(total_s))
    return f"{t // 60:02d}:{t % 60:02d}"


def _zero_totals() -> dict:
    return {
        "prompt": 0,
        "completion": 0,
        "cached": 0,
        "requests": 0,
        "compute_seconds": 0.0,
        "prefill_seconds": 0.0,
        "generation_seconds": 0.0,
        "idle_seconds": 0.0,
        "energy_wh": 0.0,
    }


def _web_headers(resp: web.Response) -> None:
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def usage_me(request: web.Request) -> web.Response:
    """JSON usage for the identified user only (privacy: no cross-user data)."""
    app = request.app
    cfg = app["cfg"]
    token = token_from_header(request.headers.get(_KEY_HEADER, ""))
    ident = resolve_web_identity(app["auth"], cfg, token, request.remote)
    if ident is None:
        return err(
            "kein Key – bitte anmelden (Key wird lokal im Browser gespeichert) "
            "oder aus dem LAN abrufen",
            401,
            login_required=True,
        )
    days = _clamp_days(request)
    totals = app["usage"].totals(days=days)
    daily = app["usage"].daily(ident.name, days=days).get(ident.name, [])
    return web.json_response(
        {
            "user": ident.name,
            "master": ident.master,
            "days": days,
            "totals": totals.get(ident.name) or _zero_totals(),
            "daily": daily,
            # today's server-on time split (idle/prefill/generation) for
            # the distribution bar; server-off time is not included
            "today": app["usage"].today_phase_seconds(ident.name),
        }
    )


async def status_view(request: web.Request) -> web.Response:
    """Live status (open, same info as the existing /server endpoint)."""
    app = request.app
    power = app["power"]
    active = app["queue"].active()
    timeout_s = app["cfg"].proxy.idle_timeout_s
    busy = app["queue"].busy()
    # While jobs are running the server is NOT idle: the idle time stays at
    # 0:00 and the countdown shows the full timeout. Both start counting
    # again the moment the last job finished (note_user_activity resets the
    # idle timer), and the watchdog only fires when idle >= timeout AND no
    # jobs are active (see power.idle_watchdog).
    if busy:
        idle_s = 0.0
        shutdown_in_s = timeout_s
    else:
        idle_s = power.idle_seconds
        shutdown_in_s = max(0.0, timeout_s - power.idle_seconds)
    return web.json_response(
        {
            "server_state": power.state.value,
            "up": power.state.value == "ready",
            "model": app["cfg"].llm.model_name,
            "busy": busy,
            "idle_seconds": round(idle_s, 1),
            "idle_timeout_s": timeout_s,
            "shutdown_in_s": round(shutdown_in_s, 1),
            # "Server abschalten": stop intents + who still has to consent
            # (users active within the idle window, sorted)
            "stop_pending": power.stop_pending,
            "stop_waiting_for": power.stop_blocked_by(timeout_s),
            "last_boot_s": power.last_boot_s,
            "queue": {
                "active": len(active),
                "active_large": sum(1 for j in active if j.is_large),
                "queued": app["queue"].pending_count(),
            },
            # local (Europe/Berlin) time, DST-aware
            "server_time": now_local().isoformat(timespec="seconds"),
        }
    )


async def usage_recent(request: web.Request) -> web.Response:
    """Load of the last 12 hours (rolling), open like /status.

    Total GPU utilization as 5-minute buckets - identical for everyone and
    deliberately without a per-user split (Anton: "nur die
    Gesamtauslastung abbilden").
    """
    app = request.app
    try:
        window = int(request.query.get("window_s", 43200))
    except (TypeError, ValueError):
        window = 43200
    window = max(1800, min(43200, window))  # 12 h = retention of the ring buffer
    try:
        bucket = int(request.query.get("bucket_s", 300))
    except (TypeError, ValueError):
        bucket = 300
    bucket = max(60, min(3600, bucket))
    return web.json_response(app["load"].graph(window, bucket))


async def wake_server(request: web.Request) -> web.Response:
    """POST: wake the LLM host (WoL) without waiting for the boot to finish.

    Non-blocking by design: booting can take up to boot_timeout_s, which a
    browser request must not sit through. The status poll (15 s) picks up
    the state change as soon as the server is READY.
    """
    app = request.app
    power = app["power"]
    if power.state is ProxyState.READY:
        return web.json_response(
            {"state": power.state.value, "waking": False,
             "message": "Server läuft bereits."}
        )
    started = await power.request_wake()
    return web.json_response(
        {
            "state": power.state.value,
            "waking": True,
            "message": "Wake-on-LAN gestartet – Server weckt sich auf …"
            if started
            else "Wakeup läuft bereits.",
        }
    )


async def reset_idle(request: web.Request) -> web.Response:
    """POST: reset the idle timer -> the idle shutdown is postponed by the
    full timeout. Open like /wake: it only delays the shutdown, nothing
    else (same trust model as the wake button)."""
    app = request.app
    power = app["power"]
    power.note_activity()
    timeout_s = app["cfg"].proxy.idle_timeout_s
    return web.json_response(
        {
            "state": power.state.value,
            "idle_seconds": 0.0,
            "shutdown_in_s": timeout_s,
            "message": f"Idle-Timer zurückgesetzt – Abschaltung in {_mmss(timeout_s)}",
        }
    )


async def stop_server(request: web.Request) -> web.Response:
    """POST: request a shutdown (the "Server abschalten" button).

    Identity required (key or LAN fallback, fail closed - unlike /wake this
    turns the server OFF): the consent is attributed to a concrete user, and
    anonymous tailnet clients must not be able to switch off the shared
    server.

    The watchdog powers the server off on its next tick (a few seconds) as
    soon as nothing is queued/running AND every user active within the idle
    window has consented as well. New activity by a consenting user cancels
    their intent (see PowerManager.note_user_activity).
    """
    app = request.app
    power = app["power"]
    token = token_from_header(request.headers.get(_KEY_HEADER, ""))
    ident = resolve_web_identity(app["auth"], app["cfg"], token, request.remote)
    if ident is None:
        return err(
            "kein Key – bitte anmelden (Key wird lokal im Browser gespeichert) "
            "oder aus dem LAN abrufen",
            401,
            login_required=True,
        )
    if power.state is not ProxyState.READY:
        return web.json_response(
            {
                "ok": True,
                "immediate": False,
                "waiting_for": [],
                "message": "Server ist ohnehin ausgeschaltet – nichts zu tun.",
            }
        )
    power.request_stop(ident.name)
    waiting = power.stop_blocked_by(app["cfg"].proxy.idle_timeout_s)
    if waiting:
        message = "Stopp angemeldet – warte auf: " + ", ".join(waiting)
    else:
        message = (
            "Stopp angemeldet – Server geht in wenigen Sekunden aus "
            "(nach den laufenden Jobs)."
        )
    return web.json_response(
        {
            "ok": True,
            "immediate": not waiting,
            "waiting_for": waiting,
            "message": message,
        }
    )


async def personal_page(request: web.Request) -> web.Response:
    resp = web.Response(
        text=PERSONAL_HTML, content_type="text/html", charset="utf-8"
    )
    _web_headers(resp)
    return resp


async def admin_page(request: web.Request) -> web.Response:
    resp = web.Response(text=ADMIN_HTML, content_type="text/html", charset="utf-8")
    _web_headers(resp)
    return resp


def setup(app: web.Application) -> None:
    app.router.add_get("/", personal_page)
    app.router.add_get("/admin", admin_page)
    app.router.add_get("/usage/me", usage_me)
    app.router.add_get("/usage/recent", usage_recent)
    app.router.add_get("/status", status_view)
    app.router.add_post("/wake", wake_server)
    app.router.add_post("/reset-idle", reset_idle)
    app.router.add_post("/stop", stop_server)


# ---------------------------------------------------------------------------
# Frontend (plain HTML/CSS/JS, no framework)
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --ink: #1c2430; --muted: #5b6675;
  --line: #e3e7ec; --ok: #0a7d33; --bad: #b3261e; --warn: #9a6700;
  --accent: #2456d6;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
       background: var(--bg); color: var(--ink); }
header { display: flex; align-items: baseline; gap: .75rem; padding: 1rem 1.25rem;
         background: var(--card); border-bottom: 1px solid var(--line); }
h1 { font-size: 1.15rem; margin: 0; }
h2 { font-size: 1rem; margin: 0 0 .75rem; }
main { max-width: 880px; margin: 0 auto; padding: 1rem 1rem 2rem; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
        padding: 1rem 1.1rem; margin-bottom: 1rem; }
.badge { display: inline-block; padding: .15rem .6rem; border-radius: 999px;
         font-size: .8rem; font-weight: 600; }
.badge.ok { background: #e5f6ec; color: var(--ok); }
.badge.warn { background: #fdf3d8; color: var(--warn); }
.badge.bad { background: #fdeceb; color: var(--bad); }
.badge.user { background: #e8eefc; color: var(--accent); }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: right; padding: .4rem .5rem; border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { text-align: left; }
thead th { color: var(--muted); font-weight: 600; border-bottom: 2px solid var(--line); }
tfoot td { font-weight: 700; border-top: 2px solid var(--line); border-bottom: none; }
.k { color: var(--muted); margin-right: .5rem; }
.row { display: flex; flex-wrap: wrap; gap: 1rem; align-items: center; }
form { display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .5rem; }
input[type=password] { flex: 1 1 260px; padding: .55rem .7rem; font-size: .95rem;
                       border: 1px solid var(--line); border-radius: 8px; }
button { padding: .55rem .9rem; font-size: .95rem; border-radius: 8px;
         border: 1px solid var(--accent); background: var(--accent); color: #fff;
         cursor: pointer; }
button.secondary { background: #fff; color: var(--ink); border-color: var(--line); }
button.danger { background: var(--bad); border-color: var(--bad); }
.error { color: var(--bad); min-height: 1.2em; }
.muted { color: var(--muted); }
select { padding: .4rem .5rem; border: 1px solid var(--line); border-radius: 8px;
         background: #fff; }
code { background: #f2f4f7; padding: .05rem .35rem; border-radius: 5px; }
footer { max-width: 880px; margin: 0 auto; padding: 0 1rem 2rem;
         color: var(--muted); font-size: .8rem; }
details { margin: .3rem 0; }
summary { cursor: pointer; color: var(--accent); font-weight: 600; }
.dwrap { margin: .5rem 0 .5rem 1rem; }
.lg-chart { display: flex; align-items: stretch; gap: .3rem; }
.lg-ytitle { writing-mode: vertical-rl; transform: rotate(180deg);
             font-size: .7rem; color: var(--muted); align-self: center; }
.lg-yaxis { display: flex; flex-direction: column; justify-content: space-between;
            align-items: flex-end; font-size: .7rem; color: var(--muted);
            height: 120px; padding: 3px 0; }
.lg-plot { flex: 1 1 auto; min-width: 0; }
.lg-track { display: flex; align-items: flex-end; gap: 1px; height: 120px;
            border: 1px solid var(--line); border-radius: 6px; padding: 2px; }
.lg-cell { flex: 1 1 0; min-width: 0; height: 100%; display: flex; align-items: flex-end; }
.lg-bar { width: 100%; background: var(--accent); border-radius: 1px 1px 0 0; }
.lg-axis { position: relative; height: 1.1em; margin-top: .25rem; }
.lg-label { position: absolute; transform: translateX(-50%); font-size: .7rem;
            color: var(--muted); white-space: nowrap; }
.dist-track { display: flex; height: 14px; border-radius: 7px; overflow: hidden;
              background: #eef1f5; }
.dist-seg { height: 100%; min-width: 0; }
.dist-seg.prefill { background: var(--accent); }
.dist-seg.generation { background: #2e9e5b; }
.dist-seg.idle { background: #b9c2cf; }
.dist-legend { display: flex; gap: 1.1rem; flex-wrap: wrap; margin-top: .45rem;
               font-size: .82rem; }
.dist-dot { display: inline-block; width: .6em; height: .6em; border-radius: 2px;
            margin-right: .35em; }
"""

_STATUS_JS = """
function fmtInt(n) { return new Intl.NumberFormat("de-DE").format(Number(n || 0)); }
function fmtSecs(s) {
  const v = Number(s || 0);
  if (v >= 3600) return (v / 3600).toLocaleString("de-DE", {maximumFractionDigits: 2}) + " h";
  if (v >= 90) return (v / 60).toLocaleString("de-DE", {maximumFractionDigits: 1}) + " min";
  return v.toLocaleString("de-DE", {maximumFractionDigits: 1}) + " s";
}
function fmtMMSS(s) {
  const t = Math.max(0, Math.floor(Number(s) || 0));
  return String(Math.floor(t / 60)).padStart(2, "0") + ":" + String(t % 60).padStart(2, "0");
}
function fmtWh(w) {
  const v = Number(w || 0);
  if (v >= 1000) return (v / 1000).toLocaleString("de-DE", {maximumFractionDigits: 2}) + " kWh";
  return v.toLocaleString("de-DE", {maximumFractionDigits: 1}) + " Wh";
}
async function api(path, key) {
  const headers = key ? { Authorization: "Bearer " + key } : {};
  return fetch(path, { headers, cache: "no-store" });
}
let lastStatus = null;
let statusAnchor = 0;
function renderStatus(d) {
  lastStatus = d;
  statusAnchor = performance.now(); // re-anchored on every poll (15 s)
  paintStatus();
}
function paintStatus() {
  const d = lastStatus;
  if (!d) return;
  const elapsed_s = (performance.now() - statusAnchor) / 1000;
  const busy = d.busy == null
    ? (d.queue.active > 0 || d.queue.queued > 0)
    : d.busy;
  const timeout_s = Number(d.idle_timeout_s || 0);
  // While jobs are running the server is NOT idle: idle stays 0:00 and the
  // countdown the full timeout (frozen). Both count again every second the
  // moment the last job finished - the server reports the fresh values on
  // the next poll and this tick continues from there.
  const idle = busy ? 0 : Math.max(0, (d.idle_seconds || 0) + elapsed_s);
  const shutdown = busy
    ? timeout_s
    : Math.max(0, (d.shutdown_in_s == null ? timeout_s : d.shutdown_in_s) - elapsed_s);
  const el = document.getElementById("st-state");
  // Badge = operating state (Anton 27.11.):
  //   on + jobs active/queued -> "läuft" (green)
  //   on + waiting idle       -> "idle"  (yellow)
  //   booting                 -> "weckt auf …" (yellow, + queued count)
  //   shutting down           -> "schaltet ab …" (red)
  //   off                     -> "aus" (red)
  let label, cls;
  if (d.up) {
    label = busy ? "läuft" : "idle";
    cls = busy ? "ok" : "warn";
  } else if (d.server_state === "waking") {
    label = d.queue.queued > 0
      ? "weckt auf · " + d.queue.queued + " in der Schlange"
      : "weckt auf …";
    cls = "warn";
  } else if (d.server_state === "shutting_down") {
    label = "schaltet ab …";
    cls = "bad";
  } else {
    label = "aus";
    cls = "bad";
  }
  el.textContent = label;
  el.className = "badge " + cls;
  document.getElementById("st-model").textContent = d.model || "–";
  document.getElementById("st-queue").textContent =
    d.queue.active + " aktiv · " + d.queue.queued + " in Warteschlange";
  document.getElementById("st-idle").textContent = fmtMMSS(idle);
  document.getElementById("st-boot").textContent =
    d.last_boot_s != null ? "letzte Boot-Zeit: " + fmtSecs(d.last_boot_s) : "–";
  document.getElementById("st-shutdown").textContent =
    d.up ? fmtMMSS(shutdown) : "–";
  document.getElementById("st-pause").textContent =
    d.up && busy ? "· Jobs aktiv – Idle-Zeit und Abschaltung pausiert" : "";
  const stopEl = document.getElementById("st-stop");
  if (stopEl) {
    if (d.stop_pending) {
      stopEl.textContent =
        d.stop_waiting_for && d.stop_waiting_for.length
          ? " · Stopp: warte auf " + d.stop_waiting_for.join(", ")
          : " · Stopp angemeldet – Server geht gleich aus";
    } else {
      stopEl.textContent = "";
    }
  }
}
setInterval(paintStatus, 1000); // 1-s tick; each poll re-anchors the values
function renderGraph(d) {
  const busyEl = document.getElementById("ld-busy");
  if (!busyEl) return;
  busyEl.textContent = fmtSecs(d.busy_seconds) + " (" +
    Number(d.utilization_pct).toLocaleString("de-DE") + " %)";
  document.getElementById("ld-jobs").textContent = fmtInt(d.jobs);
  const el = document.getElementById("ld-graph");
  el.innerHTML = "";
  const buckets = d.buckets || [];
  if (!buckets.length) {
    el.innerHTML = '<p class="muted">Keine Aktivität im Zeitraum.</p>';
    return;
  }
  const n = buckets.length;
  // y-axis: rotated "Auslastung" title + 0/50/100 % ticks, then the plot
  const chart = document.createElement("div");
  chart.className = "lg-chart";
  const ytitle = document.createElement("span");
  ytitle.className = "lg-ytitle";
  ytitle.textContent = "Auslastung";
  const yaxis = document.createElement("div");
  yaxis.className = "lg-yaxis";
  for (const v of ["100 %", "50 %", "0 %"]) {
    const tick = document.createElement("span");
    tick.textContent = v;
    yaxis.appendChild(tick);
  }
  const plot = document.createElement("div");
  plot.className = "lg-plot";
  const track = document.createElement("div");
  track.className = "lg-track";
  const axis = document.createElement("div");
  axis.className = "lg-axis";
  for (let i = 0; i < n; i++) {
    const b = buckets[i];
    const cell = document.createElement("div");
    cell.className = "lg-cell";
    const bar = document.createElement("div");
    bar.className = "lg-bar";
    bar.style.height = Math.max(0, Math.min(100, Number(b.utilization_pct) || 0)) + "%";
    cell.appendChild(bar);
    track.appendChild(cell);
    // hour marks every 2 h: the label sits at the boundary position inside
    // the bucket that contains it (works for any bucket size)
    const boundary = (Math.floor(b.start / 7200) + 1) * 7200;
    if (boundary > b.start && boundary <= b.start + d.bucket_s) {
      const lab = document.createElement("span");
      lab.className = "lg-label";
      const t = new Date(boundary * 1000);
      lab.textContent = String(t.getHours()).padStart(2, "0") + ":" +
        String(t.getMinutes()).padStart(2, "0");
      lab.style.left = ((i + (boundary - b.start) / d.bucket_s) / n * 100) + "%";
      axis.appendChild(lab);
    }
  }
  plot.appendChild(track);
  plot.appendChild(axis);
  chart.appendChild(ytitle);
  chart.appendChild(yaxis);
  chart.appendChild(plot);
  el.appendChild(chart);
}
// Distribution bar: today's server-on time split into Prefill / Generation
// / Idle. Widths are shares of (prefill + generation + idle), so time with
// the server powered OFF never shows up (Anton: "Aus-Zeiten nicht zeigen").
function renderDist(t) {
  const track = document.getElementById("dist-track");
  const legend = document.getElementById("dist-legend");
  if (!track || !legend) return;
  const p = Number((t || {}).prefill_seconds || 0);
  const g = Number((t || {}).generation_seconds || 0);
  const i = Number((t || {}).idle_seconds || 0);
  const total = p + g + i;
  if (total <= 0) {
    track.style.visibility = "hidden";
    legend.innerHTML = '<span>Heute noch keine Serverzeit.</span>';
    return;
  }
  track.style.visibility = "visible";
  document.getElementById("dist-prefill").style.width = (p / total * 100) + "%";
  document.getElementById("dist-generation").style.width = (g / total * 100) + "%";
  document.getElementById("dist-idle").style.width = (i / total * 100) + "%";
  const pct = (v) => (v / total * 100).toLocaleString("de-DE", {maximumFractionDigits: 1}) + " %";
  const item = (color, name, v) =>
    '<span><span class="dist-dot" style="background:' + color + '"></span>' +
    name + " " + fmtSecs(v) + " (" + pct(v) + ")</span>";
  legend.innerHTML =
    item("var(--accent)", "Prefill", p) +
    item("#2e9e5b", "Generation", g) +
    item("#b9c2cf", "Idle", i);
}
async function refreshShared() {
  const [rs, rl] = await Promise.all([
    api("/status"),
    api("/usage/recent?window_s=43200&bucket_s=300"),
  ]);
  if (rs.ok) renderStatus(await rs.json());
  if (rl.ok) renderGraph(await rl.json());
}
function initActionBtn(btnId, url, label) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const msg = document.getElementById("action-msg");
    msg.textContent = label + " …";
    try {
      const r = await fetch(url, { method: "POST" });
      let b = {};
      try { b = await r.json(); } catch (e) { /* ignore */ }
      msg.textContent = (b && b.message) || (r.ok ? "OK" : "Fehler (" + r.status + ")");
      refreshShared();
    } catch (e) {
      msg.textContent = "Fehler: " + e;
    }
    btn.disabled = false;
  });
}
function initWake() {
  initActionBtn("wake-btn", "/wake", "Starte Wake-on-LAN");
  initActionBtn("idle-reset-btn", "/reset-idle", "Setze Abschaltung zurück");
}
// "Server abschalten": unlike /wake and /reset-idle this turns the server
// OFF, so an identity is required: the stored key (if any) is sent, and in
// the LAN the server-side fallback identity (anton) applies.
function initStop() {
  const btn = document.getElementById("stop-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (!confirm("Soll der LLM-Server abgeschaltet werden?\\n\\n" +
      "Läuft gerade ein Job, geht der Server erst danach aus.\\n" +
      "Ist noch ein anderer Nutzer im 20-Minuten-Fenster aktiv, muss " +
      "er den Stopp ebenfalls bestätigen.")) {
      return;
    }
    btn.disabled = true;
    const msg = document.getElementById("action-msg");
    msg.textContent = "Frage Abschaltung ab …";
    try {
      const key = localStorage.getItem(KEY_SLOT);
      const headers = key ? { Authorization: "Bearer " + key } : {};
      const r = await fetch("/stop", { method: "POST", headers });
      let b = {};
      try { b = await r.json(); } catch (e) { /* ignore */ }
      msg.textContent = (b && b.message) || (r.ok ? "OK" : "Fehler (" + r.status + ")");
      refreshShared();
    } catch (e) {
      msg.textContent = "Fehler: " + e;
    }
    btn.disabled = false;
  });
}
"""

_PERSONAL_JS = """
const KEY_SLOT = "llama_proxy_key";
let days = 30;
const $ = (id) => document.getElementById(id);

// "Prompt (neu)": fresh prompt tokens only — cached tokens are shown in
// their own column and not counted twice here (Anton: "nur frische sehen")
function freshPrompt(r) { return Math.max(0, (r.prompt || 0) - (r.cached || 0)); }
function usageCells(r) {
  return "<td>" + r.day + "</td><td>" + fmtInt(freshPrompt(r)) + "</td><td>" +
    fmtInt(r.completion) + "</td><td>" + fmtInt(r.cached) + "</td><td>" +
    fmtInt(r.requests) + "</td><td>" + fmtSecs(r.compute_seconds) + "</td>" +
    "<td>" + fmtSecs(r.idle_seconds) + "</td><td>" + fmtWh(r.energy_wh) + "</td>";
}
function renderUsage(d) {
  $("user-badge").textContent = d.user;
  const t = d.totals || {};
  const rows = d.daily || [];
  renderDist(d.today);
  const tbody = $("usage-tbody");
  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML =
      '<tr><td colspan="8" class="muted">Noch keine Nutzung in dem Zeitraum.</td></tr>';
  }
  for (const r of rows.slice().reverse()) {
    const tr = document.createElement("tr");
    tr.innerHTML = usageCells(r);
    tbody.appendChild(tr);
  }
  const tsum = {day: "Summe (" + days + " Tage)"};
  for (const k of ["prompt","completion","cached","requests","compute_seconds",
                   "idle_seconds","energy_wh"]) {
    tsum[k] = t[k] || 0;
  }
  $("usage-tfoot").innerHTML = "<tr>" + usageCells(tsum) + "</tr>";
}

function setLoginVisible(v) {
  $("login-card").hidden = !v;
  $("usage-card").hidden = v;
  $("dist-card").hidden = v;
}

async function refresh() {
  const key = localStorage.getItem(KEY_SLOT);
  const r = await api("/usage/me?days=" + days, key);
  if (r.ok) {
    renderUsage(await r.json());
    setLoginVisible(false);
    $("login-error").textContent = "";
  } else {
    setLoginVisible(true);
    let msg = "Anmeldung erforderlich.";
    try {
      const b = await r.json();
      if (b && b.error && b.error.message) msg = b.error.message;
    } catch (e) { /* keep default */ }
    $("login-error").textContent = msg;
  }
  await refreshShared();
}

$("login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const key = $("login-key").value.trim();
  if (!key) return;
  localStorage.setItem(KEY_SLOT, key);
  refresh();
});
$("logout-btn").addEventListener("click", () => {
  localStorage.removeItem(KEY_SLOT);
  refresh();
});
$("days-select").addEventListener("change", (e) => {
  days = Number(e.target.value);
  refresh();
});
initWake();
initStop();
refresh();
setInterval(refresh, 15000);
"""

_PERSONAL_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llama-proxy – meine Nutzung</title>
<style>__CSS__</style>
</head>
<body>
<header>
  <h1>llama-proxy</h1>
  <span id="user-badge" class="badge user"></span>
</header>
<main>
  <section class="card" id="dist-card" hidden>
    <h2>Heute
      <span class="muted" style="font-size:.8rem;font-weight:400">· meine Server-an-Zeit</span>
    </h2>
    <div class="dist-track" id="dist-track">
      <div class="dist-seg prefill" id="dist-prefill"></div>
      <div class="dist-seg generation" id="dist-generation"></div>
      <div class="dist-seg idle" id="dist-idle"></div>
    </div>
    <div class="dist-legend muted" id="dist-legend"></div>
    <p class="muted" style="margin:.6rem 0 0">
      Verteilung meiner Server-an-Zeit heute: Prefill (Prompt-Verarbeitung),
      Generation (Antwort), Idle (Server an, nicht rechnend). Zeiträume,
      in denen der Server ausgeschaltet war, sind nicht enthalten.
    </p>
  </section>

  <section class="card">
    <h2>Live-Status</h2>
    <div class="row">
      <div><span class="k">Server</span><span id="st-state" class="badge">…</span></div>
      <div><span class="k">Modell</span><span id="st-model">…</span></div>
      <div><span class="k">Queue</span><span id="st-queue">…</span></div>
      <div><span class="k">Idle</span><span id="st-idle">…</span></div>
    </div>
    <p class="muted" style="margin:.6rem 0 0">
      <span id="st-boot">–</span> · Abschaltung in: <span id="st-shutdown">…</span><span id="st-pause"></span><span id="st-stop"></span>
    </p>
    <p class="row" style="margin:.7rem 0 0">
      <button id="wake-btn" type="button">Server wecken</button>
      <button id="stop-btn" type="button" class="danger">Server abschalten</button>
      <button id="idle-reset-btn" type="button" class="secondary">Abschaltung zurücksetzen</button>
      <span id="action-msg" class="muted"></span>
    </p>
  </section>

  <section class="card">
    <h2>Auslastung (letzte 12 Std.)</h2>
    <div class="row">
      <div><span class="k">GPU-Busy</span><span id="ld-busy">…</span></div>
      <div><span class="k">Jobs</span><span id="ld-jobs">…</span></div>
    </div>
    <div id="ld-graph" style="margin-top:.5rem"></div>
    <p class="muted" style="margin:.6rem 0 0">
      GPU-Auslastung je 5-Minuten-Bucket (Busy-Sekunden ÷ 5 min, max. 100 %)
      über die letzten 12 Stunden – alle Nutzer zusammen, rollierendes Fenster.
    </p>
  </section>

  <section class="card" id="usage-card" hidden>
    <h2>
      <span style="float:right"><label class="k">Zeitraum
        <select id="days-select">
          <option value="7">7 Tage</option>
          <option value="30" selected>30 Tage</option>
          <option value="90">90 Tage</option>
        </select>
      </label></span>
      Meine Nutzung
    </h2>
    <table>
      <thead>
        <tr><th>Datum</th><th>Prompt (neu)</th><th>Completion</th><th>Cached</th><th>Anfragen</th>
        <th>Rechenzeit</th><th>Idle</th><th>Strom</th></tr>
      </thead>
      <tbody id="usage-tbody"></tbody>
      <tfoot id="usage-tfoot"></tfoot>
    </table>
    <p class="muted" style="margin:.6rem 0 0">
      Rechenzeit = GPU-Berechnungszeit des LLM (llama.cpp timings,
      Prefill + Generation), pro Tag.
      Prompt (neu) = frische Prompt-Tokens (Cache-Treffer abgezogen);
      Cached = Prompt-Tokens, die der Server aus dem KV-Cache bedient hat.
      Idle = Zeit, in der der Server für dich an war, aber nicht rechnete
      (bei mehreren aktiven Nutzern anteilig aufgeteilt).
      Strom = Näherung aus den konfigurierten Durchschnittsverbräuchen
      (idle / prefill / generation in W) – später durch Messwerte ersetzt.
    </p>
  </section>

  <section class="card" id="login-card">
    <h2>Anmelden</h2>
    <p class="muted">
      Du kommst über das Tailnet. Gib deinen API-Key ein – er wird nur lokal
      in diesem Browser gespeichert und nirgends hochgeladen.
    </p>
    <form id="login-form">
      <input type="password" id="login-key" placeholder="lp-…" autocomplete="off" required>
      <button type="submit">Speichern &amp; laden</button>
      <button type="button" id="logout-btn" class="secondary">Abmelden</button>
    </form>
    <p class="error" id="login-error"></p>
  </section>
</main>
<footer>
  llama-proxy · <a href="/admin">Admin</a> · <a href="/ui">Status (alt)</a> · <a href="/healthz">healthz</a>
</footer>
<script>__STATUS_JS__
__PERSONAL_JS__</script>
</body>
</html>
"""

_ADMIN_JS = """
const KEY_SLOT = "llama_proxy_master_key";
let days = 30;
const $ = (id) => document.getElementById(id);

const USAGE_COLS = '<th>Datum</th><th>Prompt (neu)</th><th>Completion</th><th>Cached</th>' +
  '<th>Anfragen</th><th>Rechenzeit</th><th>Idle</th><th>Strom</th>';
function dailyTable(rows) {
  if (!rows.length) return '<p class="muted">keine Daten in dem Zeitraum</p>';
  let html = '<table><thead><tr>' + USAGE_COLS + '</tr></thead><tbody>';
  for (const r of rows.slice().reverse()) {
    html += "<tr>" + usageCells(r) + "</tr>";
  }
  return html + "</tbody></table>";
}
// "Prompt (neu)": fresh prompt tokens only — cached tokens are shown in
// their own column and not counted twice here (Anton: "nur frische sehen")
function freshPrompt(r) { return Math.max(0, (r.prompt || 0) - (r.cached || 0)); }
function usageCells(r) {
  return "<td>" + r.day + "</td><td>" + fmtInt(freshPrompt(r)) + "</td><td>" +
    fmtInt(r.completion) + "</td><td>" + fmtInt(r.cached) + "</td><td>" +
    fmtInt(r.requests) + "</td><td>" + fmtSecs(r.compute_seconds) + "</td>" +
    "<td>" + fmtSecs(r.idle_seconds) + "</td><td>" + fmtWh(r.energy_wh) + "</td>";
}

function renderAdmin(data, keys) {
  const users = Object.keys(data.users || {}).sort();
  renderDist(data.today);
  let html = "";
  for (const name of users) {
    const t = data.users[name];
    html += "<tr><td>" + name + "</td><td>" + fmtInt(freshPrompt(t)) + "</td><td>" +
      fmtInt(t.completion) + "</td><td>" + fmtInt(t.cached) + "</td><td>" +
      fmtInt(t.requests) + "</td><td>" + fmtSecs(t.compute_seconds) + "</td>" +
      "<td>" + fmtSecs(t.idle_seconds) + "</td><td>" + fmtWh(t.energy_wh) + "</td></tr>";
  }
  $("users-tbody").innerHTML =
    html || '<tr><td colspan="8" class="muted">Noch keine Nutzungsdaten.</td></tr>';

  // Keep each user's <details> open/closed state across re-renders:
  // refresh() rebuilds the whole block every 15 s and used to drop the
  // "open" attribute (Anton 27.11: expanded rows collapsed on their own).
  const openNow = new Set(
    Array.from($("daily-block").querySelectorAll("details[open]"))
      .map((el) => el.getAttribute("data-user")));
  let dh = "";
  for (const name of users) {
    dh += '<details data-user="' + name + '"' + (openNow.has(name) ? " open" : "") +
      "><summary>" + name + "</summary><div class=\\\"dwrap\\\">" +
      dailyTable((data.daily || {})[name] || []) + "</div></details>";
  }
  $("daily-block").innerHTML = dh || '<p class="muted">Noch keine Nutzungsdaten.</p>';

  let kh = "";
  for (const k of (keys && keys.users) || []) {
    const rl = k.requests_per_hour ? k.requests_per_hour + " req/h" : "unbegrenzt";
    const tl = k.tokens_per_day ? fmtInt(k.tokens_per_day) + " tok/Tag" : "unbegrenzt";
    kh += "<tr><td>" + k.name + "</td><td><code>" + k.prefix + "…</code></td><td>" +
      k.priority + " (max " + k.max_priority + ")</td><td>" + rl + "</td><td>" +
      tl + "</td><td>" + (k.disabled ? "deaktiviert" : "aktiv") + "</td><td>" +
      (k.created_at || "") + "</td></tr>";
  }
  $("keys-tbody").innerHTML =
    kh || '<tr><td colspan="7" class="muted">Keine Keys angelegt.</td></tr>';
}

function setLoginVisible(v) {
  $("login-card").hidden = !v;
  $("data-cards").hidden = v;
  $("dist-card").hidden = v;
}

async function refresh() {
  const key = localStorage.getItem(KEY_SLOT);
  let ok = false;
  const r1 = await api("/admin/usage?days=" + days, key);
  const r2 = await api("/admin/keys", key);
  if (r1.ok && r2.ok) {
    renderAdmin(await r1.json(), await r2.json());
    ok = true;
  }
  if (!ok) {
    setLoginVisible(true);
    $("admin-error").textContent =
      "Master-Key erforderlich – wird nur lokal in diesem Browser gespeichert.";
  } else {
    setLoginVisible(false);
    $("admin-error").textContent = "";
  }
  await refreshShared();
}

$("login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const key = $("login-key").value.trim();
  if (!key) return;
  localStorage.setItem(KEY_SLOT, key);
  refresh();
});
$("logout-btn").addEventListener("click", () => {
  localStorage.removeItem(KEY_SLOT);
  refresh();
});
$("days-select").addEventListener("change", (e) => {
  days = Number(e.target.value);
  refresh();
});
initWake();
initStop();
refresh();
setInterval(refresh, 15000);
"""

_ADMIN_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llama-proxy – Admin</title>
<style>__CSS__</style>
</head>
<body>
<header>
  <h1>llama-proxy – Admin</h1>
  <a href="/" class="badge user">zurück</a>
</header>
<main>
  <section class="card" id="dist-card" hidden>
    <h2>Heute
      <span class="muted" style="font-size:.8rem;font-weight:400">· alle Nutzer</span>
    </h2>
    <div class="dist-track" id="dist-track">
      <div class="dist-seg prefill" id="dist-prefill"></div>
      <div class="dist-seg generation" id="dist-generation"></div>
      <div class="dist-seg idle" id="dist-idle"></div>
    </div>
    <div class="dist-legend muted" id="dist-legend"></div>
    <p class="muted" style="margin:.6rem 0 0">
      Verteilung der Server-an-Zeit heute (alle Nutzer): Prefill
      (Prompt-Verarbeitung), Generation (Antwort), Idle (Server an, nicht
      rechnend). Zeiträume, in denen der Server ausgeschaltet war, sind
      nicht enthalten.
    </p>
  </section>

  <section class="card">
    <h2>Live-Status</h2>
    <div class="row">
      <div><span class="k">Server</span><span id="st-state" class="badge">…</span></div>
      <div><span class="k">Modell</span><span id="st-model">…</span></div>
      <div><span class="k">Queue</span><span id="st-queue">…</span></div>
      <div><span class="k">Idle</span><span id="st-idle">…</span></div>
    </div>
    <p class="muted" style="margin:.6rem 0 0">
      <span id="st-boot">–</span> · Abschaltung in: <span id="st-shutdown">…</span><span id="st-pause"></span><span id="st-stop"></span>
    </p>
    <p class="row" style="margin:.7rem 0 0">
      <button id="wake-btn" type="button">Server wecken</button>
      <button id="stop-btn" type="button" class="danger">Server abschalten</button>
      <button id="idle-reset-btn" type="button" class="secondary">Abschaltung zurücksetzen</button>
      <span id="action-msg" class="muted"></span>
    </p>
  </section>

  <section class="card">
    <h2>Auslastung (letzte 12 Std.)</h2>
    <div class="row">
      <div><span class="k">GPU-Busy</span><span id="ld-busy">…</span></div>
      <div><span class="k">Jobs</span><span id="ld-jobs">…</span></div>
    </div>
    <div id="ld-graph" style="margin-top:.5rem"></div>
    <p class="muted" style="margin:.6rem 0 0">
      GPU-Auslastung je 5-Minuten-Bucket (Busy-Sekunden ÷ 5 min, max. 100 %)
      über die letzten 12 Stunden – alle Nutzer zusammen, rollierendes Fenster.
    </p>
  </section>

  <div id="data-cards">
    <section class="card">
      <h2>
        <span style="float:right"><label class="k">Zeitraum
          <select id="days-select">
            <option value="7">7 Tage</option>
            <option value="30" selected>30 Tage</option>
            <option value="90">90 Tage</option>
          </select>
        </label></span>
        Nutzer – Summe
      </h2>
      <table>
        <thead>
          <tr><th>User</th><th>Prompt (neu)</th><th>Completion</th><th>Cached</th><th>Anfragen</th>
          <th>Rechenzeit</th><th>Idle</th><th>Strom</th></tr>
        </thead>
        <tbody id="users-tbody"></tbody>
      </table>
      <p class="muted" style="margin:.6rem 0 0">
        Rechenzeit = GPU-Berechnungszeit des LLM (Prefill + Generation).
        Prompt (neu) = frische Prompt-Tokens (Cache-Treffer abgezogen);
        Cached = Prompt-Tokens, die der Server aus dem KV-Cache bedient hat.
        Idle = Server an, aber nicht rechnend; anteilig auf kürzlich aktive
        Nutzer aufgeteilt. Strom = Näherung aus konfigurierten
        Durchschnittsverbräuchen (idle / prefill / generation in W) –
        später durch Messwerte ersetzt.
      </p>
    </section>

    <section class="card">
      <h2>Nutzer – pro Tag</h2>
      <div id="daily-block"></div>
    </section>

    <section class="card">
      <h2>Keys</h2>
      <table>
        <thead>
          <tr><th>Name</th><th>Präfix</th><th>Prio</th><th>Req-Limit</th><th>Token-Limit</th><th>Status</th><th>Erstellt</th></tr>
        </thead>
        <tbody id="keys-tbody"></tbody>
      </table>
      <p class="muted" style="margin:.6rem 0 0">
        Verwaltung über die Admin-API: <code>POST /admin/keys</code>,
        <code>PATCH/DELETE /admin/keys/&lt;name&gt;</code> (Master-Key).
      </p>
    </section>
  </div>

  <section class="card" id="login-card" hidden>
    <h2>Admin-Anmeldung</h2>
    <p class="muted">
      Diese Übersicht ist nur mit dem Master-Key erreichbar.
      Der Key wird nur lokal in diesem Browser gespeichert.
    </p>
    <form id="login-form">
      <input type="password" id="login-key" placeholder="Master-Key" autocomplete="off" required>
      <button type="submit">Speichern &amp; laden</button>
      <button type="button" id="logout-btn" class="secondary">Abmelden</button>
    </form>
    <p class="error" id="admin-error"></p>
  </section>
</main>
<footer>
  llama-proxy · <a href="/">persönliche Seite</a> · <a href="/ui">Status (alt)</a>
</footer>
<script>__STATUS_JS__
__ADMIN_JS__</script>
</body>
</html>
"""

PERSONAL_HTML = (
    _PERSONAL_HTML.replace("__CSS__", _CSS).replace("__STATUS_JS__", _STATUS_JS).replace(
        "__PERSONAL_JS__", _PERSONAL_JS
    )
)
ADMIN_HTML = _ADMIN_HTML.replace("__CSS__", _CSS).replace("__STATUS_JS__", _STATUS_JS).replace(
    "__ADMIN_JS__", _ADMIN_JS
)
