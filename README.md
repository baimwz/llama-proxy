# llama-proxy

A small, low-maintenance proxy (runs on a Raspberry Pi, any small Debian
box, or an LXC container) that sits between your clients and a `llama.cpp`
server, so the LLM server can **sleep when idle** and be **woken on demand**.

```
 client (LAN / Tailscale)
        │  OpenAI-compatible API
        ▼
┌─────────────────── llama-proxy (Raspberry Pi / Debian / LXC) ────────────────────┐
│  • durable-in-RAM priority queue           (no DB, RAM-resident queue)           │
│  • wake-on-request  (Wake-on-LAN)                                                │
│  • idle shutdown      (SSH `sudo poweroff` after 20 min, configurable)           │
│  • consented shutdown (per-user „Server abschalten", all active agree)           │
│  • prefill scheduler  (large prompts run exclusively)                            │
│  • local token counting (HuggingFace tokenizers, heuristic fallback)             │
│  • web UI (personal + admin), 12 h load graph, Prometheus /metrics               │
└──────────────────────────────────────────────────────────────────────────────────┘
        │ WoL (on) / SSH (off)                     │ HTTP (requests)
        ▼                                           ▼
  Proxmox host (whole machine sleeps)      llama.cpp server (qwen3-27B)
```

**Why:** a 27B local LLM server idles around 90 W. With this proxy it is off
~23 h/day and back serving within one boot cycle (typically 1–3 min).

---

## Features

- **OpenAI-compatible API** – `POST /v1/chat/completions` (streaming and
  non-streaming), `POST /v1/completions`, `GET /v1/models`
- **Power management**
  - after `idle_timeout_s` (default 20 min) without requests the whole Proxmox
    host is powered off via SSH (`sudo -n poweroff`, dedicated user, key-based)
  - **consented early shutdown**: `POST /stop` (web: „Server abschalten")
    records a per-user shutdown intent; the host powers off as soon as
    *every* user active within the idle window has consented and nothing is
    queued or running – new activity by a consenting user cancels their intent
  - the next request sends a **Wake-on-LAN** magic packet and polls the
    llama.cpp `/health` endpoint until the model is loaded
  - while the server boots, streaming clients wait on a live SSE stream with
    heartbeats; non-streaming clients get a `202` + `job_id` to poll
- **Request queue (in RAM)** – every request is accepted and queued before the
  server is reachable; jobs are never rejected while booting. The queue is
  intentionally *not* written to the SD card (see *SD card & data*).
- **Prefill scheduler**
  - prompts are tokenized **locally on the proxy host** (no server round-trip needed)
  - a job is **Large** when its *new* tokens (delta vs. the conversation's last
    state, i.e. what still has to be prefilled) are ≥ `large_threshold_tokens`
      (default 4096) – a coding agent re-sending 30 k context with only 2 k new
      tokens is a **Small** job
  - Large jobs run **exclusively** (`large_exclusive = true`): while one runs,
    no other job is dispatched; Small jobs run up to `small_max_concurrent`
      (default 2, keep ≤ llama.cpp `--parallel`) in parallel
  - queue order: `X-Priority` header (0–10) desc, then FIFO
- **Monitoring & web UI** – Prometheus metrics at `/metrics`; two
  self-contained HTML pages: `/` (personal: live status, wake/stop buttons,
  12 h load graph, my usage) and `/admin` (all users, key list – master key);
  usage accounting per day with Prefill/Generation split, per-user idle time
  and energy estimate (Wh, flushed to one JSON file every 60 s)
- **Auth & multi-user (phase 2)**
  - per-user API keys (Bearer), stored only as salted SHA-256 hashes – the
    plaintext key is shown exactly once, at creation
  - per-user **default priority** + a ceiling for the `X-Priority` header
  - per-user **rate limits**: `requests_per_hour` (sliding window) and
    `tokens_per_day` (pre-checked against usage); violations return `429` with
    a `Retry-After` header
  - per-user **usage stats** (prompt / completion tokens, request count)
  - admin CLI + admin API to manage keys while the proxy keeps running
    (the key file is hot-reloaded on change)
  - a master key (admin) plus "open dev mode" when no keys are configured at all

## Endpoints

| Method | Path                | Purpose                                          |
|--------|---------------------|--------------------------------------------------|
| POST   | `/v1/chat/completions` | OpenAI-compatible chat (stream + non-stream)  |
| POST   | `/v1/completions`   | OpenAI-compatible completions                    |
| GET    | `/v1/models`        | model name (from config)                         |
| GET    | `/v1/jobs/{id}`     | job state + result (poll after 202 / recovery)   |
| GET    | `/v1/queue`         | active + queued jobs, server state               |
| GET    | `/healthz`          | proxy liveness                                   |
| GET    | `/server`           | LLM server state, idle time, queue sizes         |
| GET    | `/metrics`          | Prometheus text format                           |
| GET    | `/`                 | **web** – personal page (status, buttons, 12 h load graph, my usage; key or LAN fallback) |
| GET    | `/admin`            | **web** – admin page (all users, key list; **master key**) |
| GET    | `/ui`               | minimal auto-refreshing ops dashboard            |
| GET    | `/status`           | live status: state, busy, idle, stop intents, queue (public) |
| POST   | `/wake`             | start Wake-on-LAN, non-blocking (public)         |
| POST   | `/reset-idle`       | reset the idle timer (public)                    |
| POST   | `/stop`             | request a shutdown – identity required, per-user consent (see sleep/wake) |
| GET    | `/usage/me`         | my usage totals + daily rows (identity required) |
| GET    | `/usage/recent`     | 12 h load buckets, `?window_s=…&bucket_s=…` (public) |
| POST   | `/admin/keys`       | **master key** – create a user key (returns the plaintext once) |
| GET    | `/admin/keys`       | **master key** – list all users                  |
| PATCH  | `/admin/keys/{name}`| **master key** – update limits / priority / disable |
| DELETE | `/admin/keys/{name}`| **master key** – remove a user                   |
| GET    | `/admin/usage`      | **master key** – per-user totals (`?days=N`)     |

## Web UI

Two self-contained HTML pages (no framework, no CDN) driven by the JSON
endpoints above, refreshed every 15 s with a 1 s local tick for counters:

| Page | Who | Shows |
|---|---|---|
| `/` (personal) | any valid key, or the configured LAN fallback | live status, wake/stop buttons, 12 h load graph, my usage (7/30/90 days), today's server-on time split |
| `/admin` | master key only | the same status + graph, all users (sums and per day), key list with limits |

**Identity (fail closed):** a valid key always wins. Without a key, requests
whose source IP matches `proxy.lan_prefix` get the `lan_fallback_user`
identity (LAN convenience). Anything else – e.g. tailnet traffic via
`tailscale serve` – sees a login mask and stores the key in the browser's
`localStorage`. An *invalid* key never falls back to the LAN identity
(401), and `/stop` + `/usage/me` require an identity. The status and load
data are identical for everyone (deliberately no per-user split in the graph).

**Status badge** (both pages) – the badge shows the *operating* state:

| Server | Badge |
|---|---|
| on + a job is active or queued | `läuft` (green) |
| on, waiting idle | `idle` (yellow) |
| booting | `weckt auf …` (yellow; `weckt auf · N in der Schlange` while jobs are queued) |
| shutting down | `schaltet ab …` (red) |
| off | `aus` (red) |

While jobs are active, the idle counter and the shutdown countdown are
frozen („Jobs aktiv – Idle-Zeit und Abschaltung pausiert") and resume the
moment the last job finishes.

**Buttons:** `Server wecken` (`POST /wake`, non-blocking – the 15 s poll
picks up the state change), `Server abschalten` (`POST /stop`, identity
required, per-user consent – see sleep/wake below),
`Abschaltung zurücksetzen` (`POST /reset-idle`).

**12 h load graph:** GPU busy seconds in 5-minute buckets over the last
12 hours (rolling window). Buckets are aligned to 5-minute epoch boundaries,
so a job stays in the same bar across refreshes; the current (open) bucket
is shown as-is. The y-axis carries 0/50/100 % ticks plus the „Auslastung"
label; the x-axis labels sit at the 2-hour boundaries. JSON source:
`GET /usage/recent?window_s=…&bucket_s=…` (window clamped to 1800–43200 s,
bucket to 60–3600 s – defaults 43200 s / 300 s = 144 buckets).

## Quick start (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config.example.toml config.toml   # fill in values
.venv/bin/llama-proxy serve --config config.toml -v
.venv/bin/llama-proxy selftest --config config.toml
```

### Try it without a real LLM server

```bash
.venv/bin/python scripts/mock_llm.py --port 9090 &
# point config [llm] base_url to http://127.0.0.1:9090
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer YOUR_KEY' \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

## Deployment (Raspberry Pi, Debian box, or LXC)

1. **OS & runtime** – Raspberry Pi OS (64-bit) or any Debian-based system
   (e.g. a Debian 13 LXC container), Python ≥ 3.11 (both ship it). For a
   Raspberry Pi use a good SD card (industrial/endurance grade) or an eMMC
   module.
2. **Install**
   ```bash
   git clone https://github.com/<you>/llama-proxy ~/llama-proxy
   cd ~/llama-proxy
   python3 -m venv .venv
   .venv/bin/pip install .
   cp config.example.toml config.toml   # edit: secrets stay HERE, never in git
   ```
3. **Config** – see [config reference](#configuration-reference). The real
   `config.toml` (with API keys) is git-ignored and stays on the machine.
4. **systemd** (adjust user and paths, e.g. `User=pi` → your user)
   ```ini
   # /etc/systemd/system/llama-proxy.service
   [Unit]
   Description=llama-proxy (LLM sleep/wake proxy)
   After=network-online.target
   Wants=network-online.target

   [Service]
   User=pi
   WorkingDirectory=/home/pi/llama-proxy
   ExecStart=/home/pi/llama-proxy/.venv/bin/llama-proxy serve --config /home/pi/llama-proxy/config.toml
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now llama-proxy
   journalctl -fu llama-proxy
   ```

### LLM server side (Proxmox host)

- llama.cpp as a **systemd service on the host** (auto-start after boot), e.g.
  `llama-server -m qwen3-27b-q4_K_M.gguf --ctx-size 16384 --parallel 2
  --api-key <service-key>` – or a VM/LXC with *Start at boot* (recommended:
  an LXC, e.g. id 100; the proxy's `llm.base_url` then points at the
  container's IP/port).
- a **dedicated SSH user** `llama` with key login and a locked-down sudoers
  rule (no full root):
  ```
  # /etc/sudoers.d/llama-proxy
  llama ALL=(root) NOPASSWD: /usr/sbin/poweroff
  ```
  (`visudo -cf /etc/sudoers.d/llama-proxy` to check syntax)
- **Wake-on-LAN**: enable WoL for the server NIC in BIOS/UEFI. The proxy must
  be in the same L2 segment (home LAN) for the magic packet to arrive – WoL
  does *not* work over Tailscale/WireGuard. If the server sits behind a
  router on another VLAN, configure the router to forward the magic packet.
- **Tailscale** on both machines (optional but recommended) for stable MagicDNS
  names and remote access.

### Optional: LLM host internet through the Pi (gateway mode)

If the Proxmox host has only one NIC (the cable to the Pi) but you still want
internet on it (apt updates, HuggingFace model downloads), the Pi can act as a
small NAT gateway. The host stays invisible to the rest of the LAN.

```bash
sudo scripts/setup-llm-gateway.sh show            # detect + preview
sudo scripts/setup-llm-gateway.sh apply           # configure (idempotent, backs up)
sudo scripts/setup-llm-gateway.sh test            # verify from the Pi
sudo scripts/setup-llm-gateway.sh proxmox-config  # copy this onto the host
sudo scripts/setup-llm-gateway.sh uninstall       # revert
```

What it does (per-network-manager: systemd-networkd / NetworkManager /
ifupdown):

- LLM interface (auto-detected as USB) gets `10.42.0.1/24`
- enables IPv4 forwarding
- nftables: forward `10.42.0.0/24 -> LAN` + MASQUERADE, forward policy `drop`
- backs up every modified file to `/var/backups/llama-gateway/`

On the Proxmox host: set the printed stanza in `/etc/network/interfaces`
(static `10.42.0.2/24`, gateway `10.42.0.1`, DNS), then
`systemctl restart networking`.

**Bandwidth:** a Pi 4 NATs roughly **500–800 Mbit/s** – your home connection
will be the ceiling before the Pi is. Real-world: apt updates take seconds,
a ~17 GB model download ~9 min at 300 Mbit / ~3 min at 1 Gbit.

**Caveats:** the Pi is now the host's internet (SPOF for apt/downloads – the
LLM itself keeps serving). Keep `Restart=always`, a good 5 A PSU, and the USB
adapter on a USB 3.0 port.

### Safety net: server-side idle poweroff (optional)

llama-proxy shuts the host down after the idle timeout – but if the proxy
host itself is down, the server would keep burning standby power.
`scripts/proxmox-idle-poweroff.sh` installs a small Proxmox daemon that
powers the host off when the LLM has been idle for N hours (default
**2 h** – a fallback, not the primary path). It runs on the host (an LXC
container cannot power off the host) and decides "idle" from two signals –
either one resets the timer:

- **GPU power draw** (primary): `nvidia-smi` reading, taken on the host or
  via `pct exec` inside the LXC. Draw ≥ threshold (default **100 W**) means
  a job is running – this also covers long non-streaming generations that
  produce no network traffic.
- **LXC network counters** (fallback): rx/tx bytes moving ⇒ request traffic.

```bash
sudo scripts/proxmox-idle-poweroff.sh show             # config + current signals
sudo scripts/proxmox-idle-poweroff.sh install          # stable path + systemd unit (idempotent, backs up)
sudo scripts/proxmox-idle-poweroff.sh uninstall        # revert
```

Options: `--lxc-id 100`, `--lxc-iface eth0`, `--timeout 7200`,
`--interval 30`, `--gpu-threshold 100`, `--no-gpu` (network signal only),
`--probe CMD` / `--gpu-probe CMD` (testing overrides).

### Client examples

```bash
# non-streaming (server ready)
curl -s http://proxy:8080/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_KEY' -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}]}'

# streaming
curl -N http://proxy:8080/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_KEY' -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}],"stream":true}'

# server is asleep, non-streaming request -> 202 + job id:
#   {"job_id": "...", "status": "queued", "poll": "/v1/jobs/<id>"}
curl -s http://proxy:8080/v1/jobs/<id>
```

## Multi-user, priorities & rate limits (phase 2)

Every client gets its own key. The key is shown **once** when created; only a
salted hash is stored (in `data/keys.json`, git-ignored). No database.

### Create and manage keys (CLI)

```bash
# create a key (prints the plaintext exactly once)
.venv/bin/llama-proxy keys add alice --config config.toml \
    --priority 5 --max-priority 8 \
    --requests-per-hour 60 --tokens-per-day 1000000

.venv/bin/llama-proxy keys list    --config config.toml
.venv/bin/llama-proxy keys disable alice --config config.toml
.venv/bin/llama-proxy keys enable  alice --config config.toml
.venv/bin/llama-proxy keys remove  alice --config config.toml

# per-user usage (prompt / completion tokens, requests)
.venv/bin/llama-proxy usage --config config.toml --days 7 [--user alice]
```

The running proxy **hot-reloads** `data/keys.json` whenever the file changes
(mtime + size), so CLI edits take effect immediately – no restart.

### Per-user behaviour

- **Priority** – a request's priority is its `X-Priority` header (0–10) when
  present, otherwise the key's default `priority`. For user keys the header is
  clamped to the key's `max_priority`, so a key cannot escalate above its ceiling.
- **Rate limits** – `requests_per_hour` uses a sliding one-hour window
  (RAM-only, reset on proxy restart); `tokens_per_day` is pre-checked against
  that user's usage before a job is queued. A violation returns `429` with an
  OpenAI-style error body and a `Retry-After` header.
- **Master key** – `proxy.master_api_key` is the admin key: unlimited limits,
  and the only key accepted by the `/admin/*` endpoints.
- **Open dev mode** – only when *no* master key **and** no user keys are
  configured at all; every request then runs as the `default` user.

### Admin API (master key)

```bash
# create a key
curl -s http://proxy:8080/admin/keys \
  -H 'Authorization: Bearer MASTER_KEY' -H 'Content-Type: application/json' \
  -d '{"name":"bob","priority":4,"requests_per_hour":30}'
# -> 201, body contains the plaintext key (shown once)

curl -s http://proxy:8080/admin/keys -H 'Authorization: Bearer MASTER_KEY'
curl -s -X PATCH http://proxy:8080/admin/keys/bob \
  -H 'Authorization: Bearer MASTER_KEY' -d '{"disabled":true}'
curl -s -X DELETE http://proxy:8080/admin/keys/bob -H 'Authorization: Bearer MASTER_KEY'
curl -s 'http://proxy:8080/admin/usage?days=7' -H 'Authorization: Bearer MASTER_KEY'
```

## How the sleep/wake cycle works

```
READY ── idle 20 min, queue empty ──▶ SHUTTING_DOWN ── host gone ──▶ OFF
OFF ── first request ──▶ WAKING (WoL + health poll) ──▶ READY
```

- The idle clock only starts when the queue is empty and no job is running,
  and it resets on every request – long generations never trigger a shutdown.
- A request arriving while the host is shutting down simply wakes it again.
- **Consented early shutdown** (the „Server abschalten" button): `POST /stop`
  records a shutdown intent for the calling identity. The watchdog then
  powers the host off on its next tick as soon as (a) nothing is queued or
  running and (b) *every* user that was active within the idle window has
  consented as well. `/status` reports `stop_pending` and
  `stop_waiting_for` (the users whose consent is still missing). New
  activity by a consenting user cancels their intent – using the server
  again means it should stay on. Consents are cleared after a successful
  shutdown and on the next boot.
- The proxy keeps a live picture of the server's state: if the host stops
  answering unexpectedly (crash, manual power-off), the state goes back to
  `off` within a few seconds, and a request that hits a dead connection right
  after a shutdown race automatically re-wakes the host and retries once.
- Boot time is measured and exported as `llama_proxy_boot_seconds`.

## RAM-only queue & data (why there is no database)

The queue, job states and conversation states live **in RAM only**.
Consequence: if the proxy host itself restarts, in-flight jobs are lost –
but clients retry, and the retry re-triggers the wake automatically, so
nothing is broken. The only disk writes are:

- `data/usage.json` – one small atomic write per flush interval (default 60 s)
- `data/tokenizer.json` – one download on first start (~3 MB)
- the config file, only when you edit it

On an SD card (Raspberry Pi) that keeps wear negligible. If you want zero
writes at all, set `usage.flush_interval_s` high; usage stats are the only
thing that would be lost.

## Scheduler details

- Token counting: `tokenizers` + the model's `tokenizer.json` (downloaded once,
  cached under `data/`). Without it (or `tokenizer.url = ""`) a conservative
  chars/3 heuristic is used.
- **Delta classification**: for each conversation (keyed by a hash of the
  prompt head) the proxy remembers the last rendered prompt. New requests
  share a long common prefix → only the suffix is counted as "new tokens".
  After a server restart the tracker is invalidated, so first turns after a
  wake are always classified on the full prompt (conservative).
- Edge cases: first turn, expired state (TTL 30 min), unknown conversation →
  full prompt length. `classification = "total"` disables delta logic entirely.
- The server-side `usage` numbers are authoritative for statistics; the local
  count only drives the Large/Small decision.

## Usage accounting & energy

Per user per day (web tables, `/usage/me`, `/admin/usage`):

- **tokens**: fresh prompt / completion / cached, request count
- **Rechenzeit** (compute seconds): llama.cpp timings, split into
  *Prefill* (prompt processing) and *Generation* (answer)
- **Idle**: time the server was on but not computing, split *equally*
  among the users active within the attribution window
- **Strom** (Wh): phase seconds × configured average draws
  (`power.power_idle_w` / `power_prefill_w` / `power_generation_w`) –
  estimates until real measurements replace them (the logic stays the same)

The 12 h load graph additionally keeps a rolling ring buffer of GPU busy
seconds (12 h retention) – the only long-term load series.

## Configuration reference

See [`config.example.toml`](config.example.toml) for the annotated template.
Key settings:

| Key | Default | Meaning |
|-----|---------|---------|
| `proxy.master_api_key` | – | Bearer key for clients (SECRET) |
| `proxy.idle_timeout_s` | 1200 | sleep after 20 min idle |
| `proxy.lan_prefix` / `proxy.lan_fallback_user` | – / – | web identity fallback for keyless LAN requests (empty = disabled, everyone logs in) |
| `llm.base_url` | – | llama.cpp endpoint |
| `llm.api_key` | – | llama.cpp service key (SECRET) |
| `llm.boot_timeout_s` | 300 | max wake→ready wait |
| `power.ssh_host` / `ssh_user` / `ssh_key_path` | – | host power-off path |
| `power.wol_mac` | – | wake-on-LAN target MAC |
| `power.wol_resend_interval_s` | 15 | WoL re-send period while waiting for the boot (0 = send once) |
| `power.power_idle_w` / `power_prefill_w` / `power_generation_w` | 80 / 270 / 320 | average host draw per phase (W), used for the per-user Wh estimates |
| `scheduler.large_threshold_tokens` | 4096 | "new" tokens → Large job |
| `scheduler.small_max_concurrent` | 2 | parallel Small jobs |
| `scheduler.large_exclusive` | true | Large blocks everything |
| `scheduler.classification` | delta | `delta` or `total` |
| `usage.flush_interval_s` | 60 | usage file flush period |
| `users.default_priority` | 3 | default priority for new keys (0–10) |
| `users.default_max_priority` | 10 | `X-Priority` ceiling for new keys |
| `users.default_requests_per_hour` | 0 | per-hour limit for new keys (0 = unlimited) |
| `users.default_tokens_per_day` | 0 | per-day token limit for new keys (0 = unlimited) |

## Monitoring

Prometheus scrape config:

```yaml
- job_name: llama-proxy
  static_configs:
    - targets: ["pi.example:8080"]
```

Selected metrics: `llama_proxy_state`, `llama_proxy_queue_size`,
`llama_proxy_jobs_active_{small,large}`, `llama_proxy_requests_total{result,kind}`,
`llama_proxy_tokens_total{kind}`, `llama_proxy_ttfb_seconds`,
`llama_proxy_duration_seconds`, `llama_proxy_boot_seconds`,
`llama_proxy_shutdowns_total`, `llama_proxy_wake_failures_total`,
`llama_proxy_idle_seconds`.

## Security notes

- Set `proxy.master_api_key` and create per-user keys. The API is only open
  (dev mode) when *no* master key and *no* user keys exist.
- User keys are stored **only** as salted SHA-256 hashes in `data/keys.json`;
  the plaintext key is shown exactly once and cannot be recovered. Revoke by
  disabling or removing the key, or by deleting/rotating the file + salt.
- Secrets live in `config.toml`, `data/keys.json` and the SSH key file – all
  git-ignored, never in the repository.
- `data/keys.json` and `data/usage.json` should be read-restricted
  (`chmod 600`); they are the only long-lived secrets/statistics on disk.
- Tailscale/LAN is the intended perimeter; no public TLS is required, but add
  one if you expose the port publicly (e.g. `tailscale serve` puts HTTPS in
  front for free).
- Web pages fail closed: without a valid key (or the configured LAN
  fallback) `/usage/me` and `/stop` return 401; anonymous visitors only see
  the public status/load data and the login mask.
- The `llama` SSH user on the Proxmox host can only run `poweroff`.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Wake never succeeds (`wake_failures_total` grows) | WoL not enabled in BIOS, proxy/server on different L2, or `boot_timeout_s` too short for your model load |
| `ssh poweroff failed` | sudoers rule missing/wrong, `ssh_key_path` wrong, or `BatchMode` blocked (check `ssh -v proxy@host 'sudo -n poweroff'` from the proxy host) |
| Requests slow after wake | normal: first turn is a full prefill; check `llama_proxy_boot_seconds` |
| `LLM SERVER NOT REACHABLE` in selftest | `base_url` wrong / Tailscale not up / `--api-key` mismatch |
| Jobs classified Small but server slow | raise `large_threshold_tokens` or set `large_exclusive` behaviour expectations – check `/v1/queue` `is_large` field |
| LAN fallback identity not applied | `proxy.lan_prefix` empty or wrong (it is a *source-IP prefix*, e.g. `"192.168.1."`), or the request arrives via another interface (tailscale serve → source IP is `127.0.0.1`, login mask instead) |

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/pytest -q
```

## Roadmap

- [x] Phase 1: proxy + queue + power management + scheduler + monitoring
- [x] Phase 2: per-user API keys, priorities, rate limits, usage per user
- [x] Phase 3: web UI (personal + admin), consented shutdown, 12 h load graph,
      Prefill/Generation + idle split + energy (Wh)
- [ ] (optional) Grafana dashboards, scheduled off-windows

## License

MIT – see [LICENSE](LICENSE).
