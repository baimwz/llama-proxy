# llama-proxy

A small, low-maintenance proxy (Raspberry Pi 4 friendly) that sits between
your clients and a `llama.cpp` server, so the LLM server can **sleep when idle**
and be **woken on demand**.

```
 client (LAN / Tailscale)
        │  OpenAI-compatible API
        ▼
┌─────────────────────── Raspberry Pi: llama-proxy ───────────────────────┐
│  • durable-in-RAM priority queue        (no DB on the SD card)          │
│  • wake-on-request  (Wake-on-LAN)                                            │
│  • idle shutdown      (SSH `sudo poweroff` after 20 min, configurable)     │
│  • prefill scheduler  (large prompts run exclusively)                     │
│  • local token counting (HuggingFace tokenizers, heuristic fallback)     │
│  • Prometheus /metrics + minimal HTML dashboard                          │
└──────────────────────────────────────────────────────────────────────────┘
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
  - the next request sends a **Wake-on-LAN** magic packet and polls the
    llama.cpp `/health` endpoint until the model is loaded
  - while the server boots, streaming clients wait on a live SSE stream with
    heartbeats; non-streaming clients get a `202` + `job_id` to poll
- **Request queue (in RAM)** – every request is accepted and queued before the
  server is reachable; jobs are never rejected while booting. The queue is
  intentionally *not* written to the SD card (see *SD card & data*).
- **Prefill scheduler**
  - prompts are tokenized **locally on the Pi** (no server round-trip needed)
  - a job is **Large** when its *new* tokens (delta vs. the conversation's last
    state, i.e. what still has to be prefilled) are ≥ `large_threshold_tokens`
      (default 4096) – a coding agent re-sending 30 k context with only 2 k new
      tokens is a **Small** job
  - Large jobs run **exclusively** (`large_exclusive = true`): while one runs,
    no other job is dispatched; Small jobs run up to `small_max_concurrent`
      (default 2, keep ≤ llama.cpp `--parallel`) in parallel
  - queue order: `X-Priority` header (0–10) desc, then FIFO
- **Monitoring** – Prometheus metrics at `/metrics`, live dashboard at `/`,
  usage accounting per day (flushed to one JSON file every 60 s)
- **Auth** – single shared API key (Bearer), required in production

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
| GET    | `/` or `/ui`        | small auto-refreshing HTML dashboard             |

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

## Raspberry Pi 4 deployment

1. **OS & runtime** – Raspberry Pi OS (64-bit) or Debian bookworm, Python ≥ 3.11
   (both ship it). A good SD card (industrial/endurance grade) or an eMMC
   module is recommended.
2. **Install**
   ```bash
   git clone https://github.com/<you>/llama-proxy ~/llama-proxy
   cd ~/llama-proxy
   python3 -m venv .venv
   .venv/bin/pip install .
   cp config.example.toml config.toml   # edit: secrets stay HERE, never in git
   ```
3. **Config** – see [config reference](#configuration-reference). The real
   `config.toml` (with API keys) is git-ignored and stays on the Pi.
4. **systemd**
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
  --api-key <service-key>` – or a VM/LXC with *Start at boot*.
- a **dedicated SSH user** `proxy` with key login and a locked-down sudoers
  rule (no full root):
  ```
  # /etc/sudoers.d/llama-proxy
  proxy ALL=(root) NOPASSWD: /usr/sbin/poweroff
  ```
  (`visudo -cf /etc/sudoers.d/llama-proxy` to check syntax)
- **Wake-on-LAN**: enable WoL for the server NIC in BIOS/UEFI. The Pi must be
  in the same L2 segment (home LAN) for the magic packet to arrive – WoL does
  *not* work over Tailscale/WireGuard. If the server sits behind a router on
  another VLAN, configure the router to forward the magic packet.
- **Tailscale** on both machines (optional but recommended) for stable MagicDNS
  names and remote access.

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

## How the sleep/wake cycle works

```
READY ── idle 20 min, queue empty ──▶ SHUTTING_DOWN ── host gone ──▶ OFF
OFF ── first request ──▶ WAKING (WoL + health poll) ──▶ READY
```

- The idle clock only starts when the queue is empty and no job is running,
  and it resets on every request – long generations never trigger a shutdown.
- A request arriving while the host is shutting down simply wakes it again.
- The proxy keeps a live picture of the server's state: if the host stops
  answering unexpectedly (crash, manual power-off), the state goes back to
  `off` within a few seconds, and a request that hits a dead connection right
  after a shutdown race automatically re-wakes the host and retries once.
- Boot time is measured and exported as `llama_proxy_boot_seconds`.

## SD card & data (why there is no database)

The queue, job states and conversation states live **in RAM only**. Consequence:
if the Pi itself restarts, in-flight jobs are lost – but clients retry, and the
retry re-triggers the wake automatically, so nothing is broken. The only
disk writes are:

- `data/usage.json` – one small atomic write per flush interval (default 60 s)
- `data/tokenizer.json` – one download on first start (~3 MB)
- the config file, only when you edit it

That keeps SD-card wear negligible. If you want zero writes at all, set
`usage.flush_interval_s` high; usage stats are the only thing that would be
lost.

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

## Configuration reference

See [`config.example.toml`](config.example.toml) for the annotated template.
Key settings:

| Key | Default | Meaning |
|-----|---------|---------|
| `proxy.master_api_key` | – | Bearer key for clients (SECRET) |
| `proxy.idle_timeout_s` | 1200 | sleep after 20 min idle |
| `llm.base_url` | – | llama.cpp endpoint |
| `llm.boot_timeout_s` | 300 | max wake→ready wait |
| `power.ssh_host` / `ssh_user` / `ssh_key_path` | – | host power-off path |
| `power.wol_mac` | – | wake-on-LAN target MAC |
| `scheduler.large_threshold_tokens` | 4096 | "new" tokens → Large job |
| `scheduler.small_max_concurrent` | 2 | parallel Small jobs |
| `scheduler.large_exclusive` | true | Large blocks everything |
| `scheduler.classification` | delta | `delta` or `total` |
| `usage.flush_interval_s` | 60 | usage file flush period |

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

- Always set `proxy.master_api_key` (the API is open without it – dev only).
- Secrets live in `config.toml` (git-ignored) and the SSH key file – never in
  the repository.
- Tailscale/LAN is the intended perimeter; no public TLS is required, but add
  one if you expose the port publicly.
- The `proxy` SSH user on the Proxmox host can only run `poweroff`.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Wake never succeeds (`wake_failures_total` grows) | WoL not enabled in BIOS, Pi/server on different L2, or `boot_timeout_s` too short for your model load |
| `ssh poweroff failed` | sudoers rule missing/wrong, `ssh_key_path` wrong, or `BatchMode` blocked (check `ssh -v proxy@host 'sudo -n poweroff'` from the Pi) |
| Requests slow after wake | normal: first turn is a full prefill; check `llama_proxy_boot_seconds` |
| `LLM SERVER NOT REACHABLE` in selftest | `base_url` wrong / Tailscale not up / `--api-key` mismatch |
| Jobs classified Small but server slow | raise `large_threshold_tokens` or set `large_exclusive` behaviour expectations – check `/v1/queue` `is_large` field |

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/pytest -q
```

## Roadmap

- [x] Phase 1: proxy + queue + power management + scheduler + monitoring
- [ ] Phase 2: per-user API keys, priorities, rate limits, usage per user
- [ ] Phase 3 (optional): Grafana dashboards, scheduled off-windows, job cancel

## License

MIT – see [LICENSE](LICENSE).
