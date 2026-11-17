#!/usr/bin/env bash
# proxmox-idle-poweroff.sh -- server-side safety net for llama-proxy.
#
# Powers off this Proxmox host when the LLM has been idle for TIMEOUT
# seconds. This is a FALLBACK: normally llama-proxy (on the Pi) shuts the
# host down after 20 min of idle. The net catches the case where the Pi is
# down/stuck and the server would otherwise keep burning power.
#
# It runs on the HOST: an LXC container cannot power off the host
# (poweroff inside a container only stops the container).
#
# Activity signals (either one resets the idle timer):
#   1. GPU power draw (primary): nvidia-smi power.draw, read on the host
#      (driver on host, LXC shares /dev/nvidia*) or via `pct exec` inside
#      the LXC (GPU passed through). Draw >= threshold => a job is running.
#      No guessing: a busy GPU means real work, even for long
#      non-streaming generations with no network traffic.
#   2. LXC network counters (fallback): rx/tx bytes of the LXC interface
#      moving => request traffic. Also the only signal when no GPU reading
#      is available.
#
# Usage (on the Proxmox host, as root):
#   sudo proxmox-idle-poweroff.sh show             # config + current signals
#   sudo proxmox-idle-poweroff.sh install          # stable path + systemd unit + enable
#   sudo proxmox-idle-poweroff.sh run [opts]       # daemon loop (used by the unit)
#   sudo proxmox-idle-poweroff.sh uninstall        # remove unit + installed copy
#
# Options:
#   --lxc-id N        LXC id of the llama.cpp container (default: 100)
#   --lxc-iface NAME  interface inside the LXC (default: eth0)
#   --timeout SECS    idle window before poweroff (default: 7200 = 2 h)
#   --interval SECS   probe interval (default: 30)
#   --gpu-threshold W draw >= W counts as active (default: 100)
#   --no-gpu          disable the GPU signal (LXC network only)
#   --probe CMD       override the network probe (testing; must print "rx tx")
#   --gpu-probe CMD   override the GPU probe (testing; must print watts)
#   --dry-run         print poweroff instead of executing; no file changes

set -euo pipefail

LXC_ID="100"
LXC_IF="eth0"
TIMEOUT="7200"
INTERVAL="30"
GPU_THRESHOLD="100"
NO_GPU=0
PROBE=""
GPU_PROBE=""
DRY_RUN=0
CMD="show"

BACKUP_DIR="/var/backups/proxmox-idle-poweroff"
UNIT_FILE="/etc/systemd/system/proxmox-idle-poweroff.service"
INSTALL_PATH="/usr/local/bin/proxmox-idle-poweroff"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
log()  { printf '  \033[32m==>\033[0m %s\n' "$*"; }
warn() { printf '  \033[33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '  \033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'; }

backup() {  # backup FILE
    local f="$1"
    [ -f "$f" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  \033[36mdry-run:\033[0m would back up %s\n' "$f"
        return 0
    fi
    mkdir -p "$BACKUP_DIR"
    local ts; ts="$(date +%Y%m%d-%H%M%S)"
    cp -a "$f" "$BACKUP_DIR/$(basename "$f").$ts"
    log "backed up $f -> $BACKUP_DIR/"
}

probe_activity() {  # prints "rx_bytes tx_bytes"; non-zero on failure
    if [ -n "$PROBE" ]; then
        bash -c "$PROBE"
        return
    fi
    command -v pct >/dev/null 2>&1 || return 1
    pct exec "$LXC_ID" -- cat \
        "/sys/class/net/${LXC_IF}/statistics/rx_bytes" \
        "/sys/class/net/${LXC_IF}/statistics/tx_bytes"
}

parse_gpu_max() {  # stdin: nvidia-smi power.draw lines; prints max watts (int)
    awk '
        { gsub(/[^0-9.]/, "", $1) }
        $1 != "" { v = $1 + 0; if (v > m) m = v }
        END { if (m > 0) printf "%d\n", m; else exit 1 }'
}

gpu_draw() {  # prints current max GPU draw (W); non-zero when no signal
    if [ -n "$GPU_PROBE" ]; then
        bash -c "$GPU_PROBE"
        return
    fi
    [ "$NO_GPU" -eq 1 ] && return 1
    local out
    # 1) driver on the host (usual Proxmox + LXC GPU setup)
    if command -v nvidia-smi >/dev/null 2>&1; then
        out="$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits 2>/dev/null || true)"
        if printf '%s\n' "$out" | parse_gpu_max >/dev/null 2>&1; then
            printf '%s\n' "$out" | parse_gpu_max
            return 0
        fi
    fi
    # 2) GPU inside the LXC (passed through)
    if command -v pct >/dev/null 2>&1; then
        out="$(pct exec "$LXC_ID" -- nvidia-smi --query-gpu=power.draw \
            --format=csv,noheader,nounits 2>/dev/null || true)"
        if printf '%s\n' "$out" | parse_gpu_max >/dev/null 2>&1; then
            printf '%s\n' "$out" | parse_gpu_max
            return 0
        fi
    fi
    return 1
}

# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
cmd_show() {
    echo "proxmox-idle-poweroff configuration"
    echo "  LXC id            : $LXC_ID"
    echo "  LXC interface     : $LXC_IF"
    echo "  idle timeout      : ${TIMEOUT}s"
    echo "  probe interval    : ${INTERVAL}s"
    echo "  GPU threshold     : ${GPU_THRESHOLD} W (draw >= threshold => active)"
    echo "  GPU signal        : $( [ "$NO_GPU" -eq 1 ] && echo disabled || echo enabled )"
    echo "  probe overrides   : net=${PROBE:-<none>} gpu=${GPU_PROBE:-<none>}"
    echo
    if [ -f "$UNIT_FILE" ]; then
        local enabled
        enabled="$(systemctl is-enabled proxmox-idle-poweroff 2>/dev/null || echo unknown)"
        echo "  systemd unit      : installed ($enabled)"
    else
        echo "  systemd unit      : not installed (run: install)"
    fi
    echo
    local gw
    if [ "$NO_GPU" -eq 1 ]; then
        echo "  GPU power signal  : disabled (--no-gpu)"
    elif gw="$(gpu_draw 2>/dev/null)"; then
        echo "  GPU power signal  : available (current draw: ${gw} W)"
    else
        echo "  GPU power signal  : not available (needs nvidia-smi on host or in LXC)"
    fi
    if command -v pct >/dev/null 2>&1; then
        local state bytes
        state="$(pct status "$LXC_ID" 2>/dev/null || echo unknown)"
        echo "  LXC $LXC_ID state    : $state"
        if bytes="$(probe_activity 2>/dev/null)"; then
            echo "  current counters  : rx=${bytes%% *} tx=${bytes##* }"
        else
            warn "network probe failed (LXC stopped? interface name?)"
        fi
    else
        warn "pct not found - not a Proxmox host (or use --probe for testing)"
    fi
}

cmd_run() {
    local signals=""
    if [ "$NO_GPU" -eq 0 ] && gpu_draw >/dev/null 2>&1; then
        signals="GPU power (>= ${GPU_THRESHOLD} W)"
    fi
    if probe_activity >/dev/null 2>&1; then
        signals="${signals:+$signals + }LXC $LXC_ID network"
    fi
    if [ -z "$signals" ]; then
        warn "no activity signal found (no GPU reading, no LXC probe) - will power off after ${TIMEOUT}s of inactivity"
    else
        log "activity signals: $signals"
    fi
    log "safety net active: idle timeout ${TIMEOUT}s, probe every ${INTERVAL}s"
    local last_active now active reason
    last_active="$(date +%s)"
    local prev_rx="" prev_tx=""
    while true; do
        sleep "$INTERVAL"
        now="$(date +%s)"
        active=0
        reason=""
        # 1) GPU power: draw >= threshold => a job is running
        local gw
        if gw="$(gpu_draw 2>/dev/null)" && [[ "$gw" =~ ^[0-9]+$ ]] \
            && [ "$gw" -ge "$GPU_THRESHOLD" ]; then
            active=1
            reason="GPU ${gw} W >= ${GPU_THRESHOLD} W threshold"
        fi
        # 2) LXC network counters: traffic moved => requests are flowing
        if [ -z "$reason" ]; then
            local bytes rx tx
            if bytes="$(probe_activity 2>/dev/null)"; then
                rx="${bytes%% *}"
                tx="${bytes##* }"
                if [ -n "$prev_rx" ] && { [ "$rx" != "$prev_rx" ] || [ "$tx" != "$prev_tx" ]; }; then
                    active=1
                    reason="LXC $LXC_ID network activity (rx=$rx tx=$tx)"
                fi
                prev_rx="$rx"
                prev_tx="$tx"
            fi
        fi
        if [ "$active" -eq 1 ]; then
            log "activity: $reason - idle timer reset"
            last_active="$now"
        fi
        local idle; idle=$(( now - last_active ))
        if [ "$idle" -ge "$TIMEOUT" ]; then
            if [ "$DRY_RUN" -eq 1 ]; then
                log "DRY-RUN: would power off after ${idle}s without activity"
                last_active="$now"  # keep printing instead of spinning
            else
                log "no activity for ${idle}s (>= ${TIMEOUT}s) - powering off"
                poweroff
            fi
        fi
    done
}

render_unit() {
    local gpu_extra=""
    [ "$NO_GPU" -eq 1 ] && gpu_extra="--no-gpu "
    cat <<EOF
# generated by proxmox-idle-poweroff.sh
[Unit]
Description=Idle poweroff safety net (llama-proxy)
After=network.target

[Service]
Type=simple
ExecStart=$INSTALL_PATH run --lxc-id $LXC_ID --lxc-iface $LXC_IF --timeout $TIMEOUT --interval $INTERVAL --gpu-threshold $GPU_THRESHOLD $gpu_extra
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
}

cmd_install() {
    if [ "$DRY_RUN" -eq 0 ]; then
        [ "$(id -u)" -eq 0 ] || die "run as root"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  \033[36mdry-run:\033[0m would install %s\n' "$INSTALL_PATH"
    else
        backup "$INSTALL_PATH"
        install -m 0755 "$0" "$INSTALL_PATH"
        log "installed $INSTALL_PATH"
    fi
    local unit
    unit="$(render_unit)"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  \033[36mdry-run:\033[0m write %s\n' "$UNIT_FILE"
        printf '%s\n' "$unit" | sed 's/^/    | /'
    else
        backup "$UNIT_FILE"
        printf '%s\n' "$unit" > "$UNIT_FILE"
        systemctl daemon-reload
        systemctl enable proxmox-idle-poweroff
        log "enabled proxmox-idle-poweroff.service"
    fi
    echo
    echo "check with:  systemctl status proxmox-idle-poweroff"
    echo "             journalctl -fu proxmox-idle-poweroff"
}

cmd_uninstall() {
    if [ "$DRY_RUN" -eq 0 ]; then
        [ "$(id -u)" -eq 0 ] || die "run as root"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  \033[36mdry-run:\033[0m would disable unit + remove %s and %s\n' "$UNIT_FILE" "$INSTALL_PATH"
        return
    fi
    systemctl disable --now proxmox-idle-poweroff 2>/dev/null || true
    backup "$UNIT_FILE"
    rm -f "$UNIT_FILE"
    backup "$INSTALL_PATH"
    rm -f "$INSTALL_PATH"
    systemctl daemon-reload
    log "uninstalled (backups in $BACKUP_DIR)"
}

# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        show|run|install|uninstall) CMD="$1" ;;
        --lxc-id)        LXC_ID="$2";        shift ;;
        --lxc-iface)     LXC_IF="$2";        shift ;;
        --timeout)       TIMEOUT="$2";       shift ;;
        --interval)      INTERVAL="$2";      shift ;;
        --gpu-threshold) GPU_THRESHOLD="$2"; shift ;;
        --no-gpu)        NO_GPU=1 ;;
        --probe)         PROBE="$2";         shift ;;
        --gpu-probe)     GPU_PROBE="$2";     shift ;;
        --dry-run)       DRY_RUN=1 ;;
        -h|--help)       usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

case "$CMD" in
    show)      cmd_show ;;
    run)       cmd_run ;;
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
esac
