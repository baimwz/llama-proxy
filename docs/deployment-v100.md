# Deployment guide: sleeping Proxmox LLM host (WoL + SSH poweroff)

A portable reference for wiring llama-proxy up to a Proxmox host that is
powered **off** at rest, wakes on request (Wake-on-LAN) and is shut down via
SSH after an idle timeout. The concrete, machine-specific values for one
confirmed setup (MAC, hardware, LXC id) live in
`docs/deployment-local.md`, which is **git-ignored** — fill it in yourself.

## Stable vs. variable values

**STABLE (hardware-bound, survive a network move):**

| Value | What |
|---|---|
| the WoL MAC | the server's onboard LAN NIC MAC (`ethtool <nic>`). A kernel name like `enx<mac>` is only a systemd fallback and does **not** mean USB. |
| BIOS WoL settings | the working BIOS configuration (below) |
| `ethtool -s <nic> wol g` via `post-up` | the working, reboot-persistent WoL enablement |
| magic-packet recipe | legacy UDP ports to the subnet broadcast, same L2 |
| `poweroff` over SSH | the shutdown path (Proxmox stops guests) |

**VARIABLE (change with the network — parameterize, never hardcode):**

| Value | Where it is set |
|---|---|
| server IP / subnet / broadcast / gateway / router | `config.toml`: `llm.base_url`, `power.ssh_host`, `power.wol_bcast` |
| SSH user/key | `config.toml`: `power.ssh_user`, `power.ssh_key_path` |
| the Pi's position in the target network | physical cabling (must be **same L2** as the server) |

Everything else in llama-proxy (state machine, scheduler, keys) is
network-agnostic.

## Server-side prerequisites

- **Proxmox VE** as the host OS. The LLM normally runs in an **LXC container**
  (not a host service) → `llm.base_url` points at the container's IP/port,
  and the container must start at boot.
- If the GPU is passively cooled (e.g. a datacenter SXM card on a consumer
  board), you probably want a thermal-guard service that **stops the LXC**
  when the GPU overheats. The llama-proxy watchdog copes with that: two
  failed health checks → state `off`, and the next request re-wakes.
- **Wake-on-LAN**: the NIC's WoL flag must be set *and* survive reboots —
  see below.
- **Tailscale** (optional) on both machines for stable names and remote
  access. Note: Tailscale **cannot carry WoL**.

## Wake-on-LAN — BIOS + Proxmox (confirmed working recipe)

### BIOS (ASRock B450 Pro4; other boards: find the equivalents)

| Setting | Value | Note |
|---|---|---|
| Advanced → ACPI Configuration → **PCIE Devices Power On** | Enabled | |
| Advanced → Boot → **Boot From Onboard LAN** | Enabled | This is the *actual* WoL switch (not PXE). It was greyed out until **Fast Boot = Disabled**. |
| Advanced → South Bridge Configuration → **Deep Sleep** | Disabled | Must be off, otherwise no S5 standby power → WoL dead. |

### Proxmox (`/etc/network/interfaces`, reboot-persistent — confirmed)

```
iface nic0 inet manual
        post-up /sbin/ethtool -s nic0 wol g
```

- Set it under the **physical NIC stanza (e.g. `nic0`), NOT under the bridge**
  (`vmbr0`) — the bridge carries the same MAC, but the flag belongs on the
  physical interface.
- `inet manual` is required: the NIC exists only for WoL, it carries no
  address.
- Any future edit of `/etc/network/interfaces` must keep the `post-up` line.

### Magic packet (wake)

- **UDP, port 9** (port 7 also works) to the **subnet broadcast address** —
  never to the server's IP (in S5 the server has no IP).
- The sender (Pi) must be in the **same L2 segment/subnet** as the server.
  Isolated/guest WLANs don't work. Tailscale doesn't work.
- For a /24 the broadcast is typically `.255`; derive it from the actual
  subnet at deployment time.

llama-proxy sends the packet to ports **9, 7, 40, 4711** (`WOL_PORTS` in
`power.py`) — the legacy ports are confirmed for the reference board, the
others cover other NICs. Extra datagrams are free.

Minimal reference implementation (manual wake from any machine in the LAN,
Python 3):

```python
import socket
mac = "AA:BB:CC:DD:EE:FF".replace(":", "")            # your WoL NIC MAC (stable)
data = bytes.fromhex("FF" * 6) + bytes.fromhex(mac) * 16
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.sendto(data, ("<SUBNET_BROADCAST>", 9))              # VARIABLE: derive at deploy
```

### Manual wake via the proxy (testing)

With the master API key:

```bash
curl -X POST http://<pi>:8080/admin/wake -H "Authorization: Bearer <master>"
```

Returns `{"state": "ready", "boot_seconds": <float>}` once the LLM answers
health checks. This is the fastest WoL round-trip test once the Pi is up.

## Shutdown (Pi → server)

```
ssh root@<SERVER_IP> 'poweroff'
```

- Proxmox `poweroff` stops the guests (incl. the LLM container) as part of
  the host shutdown — no separate container stop needed in the common case.
- llama-proxy mapping (`[power]` in `config.toml`):
  - `ssh_host` / `ssh_user` / `ssh_key_path` — the SSH access (LAN IP;
    Tailscale name also works for the *shutdown*, not for the wake)
  - `ssh_command` — optional override of the remote command; **empty =
    sensible default**: `poweroff` for user `root`, `sudo -n poweroff` for a
    locked-down non-root user
  - `pre_shutdown_command` — optional remote command run **before** the
    poweroff, e.g. `pct stop 100` to stop the LLM container gracefully.
    A failure here is logged and does **not** abort the shutdown (the host
    poweroff stops guests anyway).

## Gotchas (learned the hard way)

1. `enx<MAC>` kernel names are a systemd fallback for **onboard** NICs —
   don't chase them as a "USB adapter" problem.
2. **"Boot From Onboard LAN" in the BIOS is the real WoL switch** — it is not
   PXE, and it is disabled (or greyed out) by default.
3. **Deep Sleep must be Disabled**, otherwise there is no S5 standby power and
   WoL is dead.
4. **Fast Boot blocks** the "Boot From Onboard LAN" option — set Fast Boot to
   Disabled first, then enable it.
5. `ethtool -s wol g` under the wrong interface (e.g. the bridge) looks
   correct and does nothing.
6. **Tailscale does not carry WoL** — wake only works from the physical LAN.
7. Isolated/guest WLANs drop broadcast magic packets — wake from the normal
   LAN segment.
8. Send to the **subnet broadcast**, never to the server's IP.
9. WoL enablement is **reboot-persistent** via `post-up` — confirmed; but
   every future `/etc/network/interfaces` change must keep that line.
10. The **network (IP/subnet/broadcast/gateway) is expected to change** —
    only the MAC + BIOS settings + `post-up` + the logic are portable. Keep
    all network values in `config.toml`.

## Deployment checklist (when the Pi is cabled in)

- [ ] Pi and server in the **same L2 segment** (cable or same switch)
- [ ] `config.toml`: `llm.base_url` = LLM container IP:port,
      `power.ssh_host`, `power.ssh_user` (e.g. `root`),
      `power.ssh_key_path`, `power.wol_bcast` = subnet broadcast (or leave
      empty for `255.255.255.255`)
- [ ] graceful container stop decided → `power.pre_shutdown_command`
      (e.g. `pct stop 100`) or left empty (host poweroff stops guests)
- [ ] idle timeout decided → `proxy.idle_timeout_s` (default 1200 s)
- [ ] round-trip test: `POST /admin/wake` → server boots → request works →
      idle → `poweroff` → server off → next request re-wakes
- [ ] optional: `scripts/setup-llm-gateway.sh` for LLM-host internet (see README)

## Open design questions (current answers)

| Question | Current answer |
|---|---|
| What triggers "Bedarf" (wake)? | Any inbound API request on `:8080`, or manual `POST /admin/wake`. |
| SSH access for shutdown? | `root@<server>` + key (assumed — **to confirm**: key file, host name). |
| Pi position in target network? | Same L2 as the server via LAN cable (**to confirm**: direct cable vs. switch/VLAN). |
| Graceful LXC/VM stop before poweroff? | Optional `pre_shutdown_command`; failure does not block shutdown. |
| Idle timeout value? | Default 20 min (`idle_timeout_s = 1200`); **to confirm**. |
