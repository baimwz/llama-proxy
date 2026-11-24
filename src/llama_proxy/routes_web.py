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

from datetime import datetime

from aiohttp import web

from .auth import Auth, Identity, token_from_header
from .config import Config
from .power import ProxyState
from .routes_openai import err

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
        }
    )


async def status_view(request: web.Request) -> web.Response:
    """Live status (open, same info as the existing /server endpoint)."""
    app = request.app
    power = app["power"]
    active = app["queue"].active()
    timeout_s = app["cfg"].proxy.idle_timeout_s
    return web.json_response(
        {
            "server_state": power.state.value,
            "up": power.state.value == "ready",
            "model": app["cfg"].llm.model_name,
            "idle_seconds": round(power.idle_seconds, 1),
            "idle_timeout_s": timeout_s,
            # countdown to the idle shutdown; the watchdog only fires when
            # idle >= timeout AND no jobs are active (see power.idle_watchdog)
            "shutdown_in_s": round(max(0.0, timeout_s - power.idle_seconds), 1),
            "last_boot_s": power.last_boot_s,
            "queue": {
                "active": len(active),
                "active_large": sum(1 for j in active if j.is_large),
                "queued": app["queue"].pending_count(),
            },
            "server_time": datetime.now().isoformat(timespec="seconds"),
        }
    )


async def usage_recent(request: web.Request) -> web.Response:
    """Load of the last N seconds (default 1200 = 20 min), open like /status.

    Shows the GPU busy share per user (who causes how much of the load) -
    relative load, not personal usage data, so it is safe on both pages.
    """
    app = request.app
    try:
        window = int(request.query.get("window_s", 1200))
    except (TypeError, ValueError):
        window = 1200
    window = max(60, min(3600, window))
    return web.json_response(app["load"].snapshot(window))


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
.ld-user { margin-bottom: .65rem; }
.ld-track { height: 10px; background: #eef1f5; border-radius: 6px; overflow: hidden; }
.ld-bar { height: 100%; background: var(--accent); }
.ld-label { font-size: .82rem; color: var(--muted); margin-top: .15rem; }
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
async function api(path, key) {
  const headers = key ? { Authorization: "Bearer " + key } : {};
  return fetch(path, { headers, cache: "no-store" });
}
function renderStatus(d) {
  const el = document.getElementById("st-state");
  el.textContent = d.up ? "läuft" : d.server_state;
  el.className = "badge " + (d.up ? "ok" : "bad");
  document.getElementById("st-model").textContent = d.model || "–";
  document.getElementById("st-queue").textContent =
    d.queue.active + " aktiv · " + d.queue.queued + " in Warteschlange";
  document.getElementById("st-idle").textContent = fmtMMSS(d.idle_seconds);
  document.getElementById("st-boot").textContent =
    d.last_boot_s != null ? "letzte Boot-Zeit: " + fmtSecs(d.last_boot_s) : "–";
  document.getElementById("st-shutdown").textContent =
    d.up ? fmtMMSS(d.shutdown_in_s) : "–";
  const busy = d.queue.active > 0 || d.queue.queued > 0;
  document.getElementById("st-pause").textContent =
    d.up && busy ? "· Jobs aktiv – Abschaltung pausiert" : "";
}
function renderLoad(d) {
  const busyEl = document.getElementById("ld-busy");
  if (!busyEl) return;
  busyEl.textContent = fmtSecs(d.busy_seconds) + " (" +
    Number(d.utilization_pct).toLocaleString("de-DE") + " %)";
  document.getElementById("ld-jobs").textContent = fmtInt(d.jobs);
  const el = document.getElementById("ld-users");
  el.innerHTML = "";
  if (!d.users || !d.users.length) {
    el.innerHTML = '<p class="muted">Keine Aktivität im Zeitraum.</p>';
    return;
  }
  for (const u of d.users) {
    const track = document.createElement("div");
    track.className = "ld-track";
    const bar = document.createElement("div");
    bar.className = "ld-bar";
    bar.style.width = u.pct + "%";
    track.appendChild(bar);
    const label = document.createElement("div");
    label.className = "ld-label";
    label.textContent = u.name + "  ·  " +
      Number(u.pct).toLocaleString("de-DE") + " %  ·  " + fmtSecs(u.busy_seconds) +
      "  ·  " + u.jobs + (u.jobs === 1 ? " Job" : " Jobs");
    const user = document.createElement("div");
    user.className = "ld-user";
    user.appendChild(track);
    user.appendChild(label);
    el.appendChild(user);
  }
}
async function refreshShared() {
  const [rs, rl] = await Promise.all([
    api("/status"),
    api("/usage/recent?window_s=1200"),
  ]);
  if (rs.ok) renderStatus(await rs.json());
  if (rl.ok) renderLoad(await rl.json());
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
"""

_PERSONAL_JS = """
const KEY_SLOT = "llama_proxy_key";
let days = 30;
const $ = (id) => document.getElementById(id);

function renderUsage(d) {
  $("user-badge").textContent = d.user;
  const t = d.totals || {};
  const rows = d.daily || [];
  const tbody = $("usage-tbody");
  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">Noch keine Nutzung in dem Zeitraum.</td></tr>';
  }
  for (const r of rows.slice().reverse()) {
    const tr = document.createElement("tr");
    tr.innerHTML = "<td>" + r.day + "</td><td>" + fmtInt(r.prompt) + "</td><td>" +
      fmtInt(r.completion) + "</td><td>" + fmtInt(r.cached) + "</td><td>" +
      fmtInt(r.requests) + "</td><td>" + fmtSecs(r.compute_seconds) + "</td>";
    tbody.appendChild(tr);
  }
  $("usage-tfoot").innerHTML =
    '<tr><td>Summe (' + days + " Tage)</td><td>" + fmtInt(t.prompt) + "</td><td>" +
    fmtInt(t.completion) + "</td><td>" + fmtInt(t.cached) + "</td><td>" +
    fmtInt(t.requests) + "</td><td>" + fmtSecs(t.compute_seconds) + "</td></tr>";
}

function setLoginVisible(v) {
  $("login-card").hidden = !v;
  $("usage-card").hidden = v;
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
  <section class="card">
    <h2>Live-Status</h2>
    <div class="row">
      <div><span class="k">Server</span><span id="st-state" class="badge">…</span></div>
      <div><span class="k">Modell</span><span id="st-model">…</span></div>
      <div><span class="k">Queue</span><span id="st-queue">…</span></div>
      <div><span class="k">Idle</span><span id="st-idle">…</span></div>
    </div>
    <p class="muted" style="margin:.6rem 0 0">
      <span id="st-boot">–</span> · Abschaltung in: <span id="st-shutdown">…</span><span id="st-pause"></span>
    </p>
    <p class="row" style="margin:.7rem 0 0">
      <button id="wake-btn" type="button">Server wecken</button>
      <button id="idle-reset-btn" type="button" class="secondary">Abschaltung zurücksetzen</button>
      <span id="action-msg" class="muted"></span>
    </p>
  </section>

  <section class="card">
    <h2>Auslastung (letzte 20 Min.)</h2>
    <div class="row">
      <div><span class="k">GPU-Busy</span><span id="ld-busy">…</span></div>
      <div><span class="k">Jobs</span><span id="ld-jobs">…</span></div>
    </div>
    <div id="ld-users" style="margin-top:.5rem"></div>
    <p class="muted" style="margin:.6rem 0 0">
      Anteil an der GPU-Berechnungszeit im Zeitraum (Live-Wert, kein Verlauf).
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
        <tr><th>Datum</th><th>Prompt</th><th>Completion</th><th>Cached</th><th>Anfragen</th><th>Rechenzeit</th></tr>
      </thead>
      <tbody id="usage-tbody"></tbody>
      <tfoot id="usage-tfoot"></tfoot>
    </table>
    <p class="muted" style="margin:.6rem 0 0">
      Rechenzeit = GPU-Berechnungszeit des LLM (llama.cpp timings), pro Tag.
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

function dailyTable(rows) {
  if (!rows.length) return '<p class="muted">keine Daten in dem Zeitraum</p>';
  let html = '<table><thead><tr><th>Datum</th><th>Prompt</th><th>Completion</th>' +
    '<th>Cached</th><th>Anfragen</th><th>Rechenzeit</th></tr></thead><tbody>';
  for (const r of rows.slice().reverse()) {
    html += "<tr><td>" + r.day + "</td><td>" + fmtInt(r.prompt) + "</td><td>" +
      fmtInt(r.completion) + "</td><td>" + fmtInt(r.cached) + "</td><td>" +
      fmtInt(r.requests) + "</td><td>" + fmtSecs(r.compute_seconds) + "</td></tr>";
  }
  return html + "</tbody></table>";
}

function renderAdmin(data, keys) {
  const users = Object.keys(data.users || {}).sort();
  let html = "";
  for (const name of users) {
    const t = data.users[name];
    html += "<tr><td>" + name + "</td><td>" + fmtInt(t.prompt) + "</td><td>" +
      fmtInt(t.completion) + "</td><td>" + fmtInt(t.cached) + "</td><td>" +
      fmtInt(t.requests) + "</td><td>" + fmtSecs(t.compute_seconds) + "</td></tr>";
  }
  $("users-tbody").innerHTML =
    html || '<tr><td colspan="6" class="muted">Noch keine Nutzungsdaten.</td></tr>';

  let dh = "";
  for (const name of users) {
    dh += '<details><summary>' + name + "</summary><div class=\\\"dwrap\\\">" +
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
  <section class="card">
    <h2>Live-Status</h2>
    <div class="row">
      <div><span class="k">Server</span><span id="st-state" class="badge">…</span></div>
      <div><span class="k">Modell</span><span id="st-model">…</span></div>
      <div><span class="k">Queue</span><span id="st-queue">…</span></div>
      <div><span class="k">Idle</span><span id="st-idle">…</span></div>
    </div>
    <p class="muted" style="margin:.6rem 0 0">
      <span id="st-boot">–</span> · Abschaltung in: <span id="st-shutdown">…</span><span id="st-pause"></span>
    </p>
    <p class="row" style="margin:.7rem 0 0">
      <button id="wake-btn" type="button">Server wecken</button>
      <button id="idle-reset-btn" type="button" class="secondary">Abschaltung zurücksetzen</button>
      <span id="action-msg" class="muted"></span>
    </p>
  </section>

  <section class="card">
    <h2>Auslastung (letzte 20 Min.)</h2>
    <div class="row">
      <div><span class="k">GPU-Busy</span><span id="ld-busy">…</span></div>
      <div><span class="k">Jobs</span><span id="ld-jobs">…</span></div>
    </div>
    <div id="ld-users" style="margin-top:.5rem"></div>
    <p class="muted" style="margin:.6rem 0 0">
      Anteil an der GPU-Berechnungszeit im Zeitraum (Live-Wert, kein Verlauf).
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
          <tr><th>User</th><th>Prompt</th><th>Completion</th><th>Cached</th><th>Anfragen</th><th>Rechenzeit</th></tr>
        </thead>
        <tbody id="users-tbody"></tbody>
      </table>
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
