#!/usr/bin/env bash
# proxmox-idle-poweroff.sh -- server-side safety net for llama-proxy.
#
# Powers off this Proxmox host when the LLM LXC shows no network activity
# for TIMEOUT seconds. This is a FALLBACK: normally llama-proxy (on the Pi)
# shuts the host down after 20 min of idle. The net catches the case where
# the Pi is down/stuck and the server would otherwise keep burning power.
#
# Activity signal: rx/tx byte counters of the LXC's interface (any request
# to llama.cpp moves bytes; nothing for N seconds => idle). Works without
# depending on llama.cpp's own metrics endpoints.
#
# Usage (on the Proxmox host, as root):
#   sudo proxmox-idle-poweroff.sh show             # config + current LXC activity
#   sudo proxmox-idle-poweroff.sh install          # stable path + systemd unit + enable
#   sudo proxmox-idle-poweroff.sh run [opts]       # daemon loop (used by the unit)
#   sudo proxmox-idle-poweroff.sh uninstall        # remove unit + installed copy
#
# Options:
#   --lxc-id N        LXC id of the llama.cpp container (default: 100)
#   --lxc-iface NAME  interface inside the LXC (default: eth0)
#   --timeout SECS    idle window before poweroff (default: 7200 = 2 h)
#   --interval SECS   probe interval (default: 30)
#   --probe CMD       override the activity probe (testing; must print "rx tx")
#   --dry-run         print poweroff instead of executing; no file changes

set -euo pipefail

LXC_ID="100"
LXC_IF="eth0"
TIMEOUT="7200"
INTERVAL="30"
PROBE=""
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

usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; }

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

# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
cmd_show() {
    echo "proxmox-idle-poweroff configuration"
    echo "  LXC id            : $LXC_ID"
    echo "  LXC interface     : $LXC_IF"
    echo "  idle timeout      : ${TIMEOUT}s"
    echo "  probe interval    : ${INTERVAL}s"
    echo "  probe override    : ${PROBE:-<default: pct exec $LXC_ID>}"
    echo
    if [ -f "$UNIT_FILE" ]; then
        local enabled
        enabled="$(systemctl is-enabled proxmox-idle-poweroff 2>/dev/null || echo unknown)"
        echo "  systemd unit      : installed ($enabled)"
    else
        echo "  systemd unit      : not installed (run: install)"
    fi
    echo
    if command -v pct >/dev/null 2>&1; then
        local state bytes
        state="$(pct status "$LXC_ID" 2>/dev/null || echo unknown)"
        echo "  LXC $LXC_ID state    : $state"
        if bytes="$(probe_activity 2>/dev/null)"; then
            echo "  current counters  : rx=${bytes%% *} tx=${bytes##* }"
        else
            warn "activity probe failed (LXC stopped? interface name?)"
        fi
    else
        warn "pct not found - not a Proxmox host (or use --probe for testing)"
    fi
}

cmd_run() {
    log "safety net active: LXC $LXC_ID ($LXC_IF), idle timeout ${TIMEOUT}s, probe every ${INTERVAL}s"
    local last_active now rx tx prev_rx prev_tx
    last_active="$(date +%s)"
    prev_rx=""
    prev_tx=""
    while true; do
        sleep "$INTERVAL"
        now="$(date +%s)"
        rx=""
        tx=""
        local bytes
        if bytes="$(probe_activity 2>/dev/null)"; then
            rx="${bytes%% *}"
            tx="${bytes##* }"
            if [ -n "$prev_rx" ] && { [ "$rx" != "$prev_rx" ] || [ "$tx" != "$prev_tx" ]; }; then
                log "activity on LXC $LXC_ID (rx=$rx tx=$tx); idle timer reset"
                last_active="$now"
            fi
            prev_rx="$rx"
            prev_tx="$tx"
        else
            warn "probe failed (LXC $LXC_ID stopped or $LXC_IF missing) - counting as idle"
        fi
        local idle; idle=$(( now - last_active ))
        if [ "$idle" -ge "$TIMEOUT" ]; then
            if [ "$DRY_RUN" -eq 1 ]; then
                log "DRY-RUN: would power off after ${idle}s without LLM activity"
                last_active="$now"  # keep printing instead of spinning
            else
                log "no LLM activity for ${idle}s (>= ${TIMEOUT}s) - powering off"
                poweroff
            fi
        fi
    done
}

render_unit() {
    cat <<EOF
# generated by proxmox-idle-poweroff.sh
[Unit]
Description=Idle poweroff safety net (llama-proxy)
After=network.target

[Service]
Type=simple
ExecStart=$INSTALL_PATH run --lxc-id $LXC_ID --lxc-iface $LXC_IF --timeout $TIMEOUT --interval $INTERVAL
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
        --lxc-id)    LXC_ID="$2";    shift ;;
        --lxc-iface) LXC_IF="$2";    shift ;;
        --timeout)   TIMEOUT="$2";   shift ;;
        --interval)  INTERVAL="$2";  shift ;;
        --probe)     PROBE="$2";     shift ;;
        --dry-run)   DRY_RUN=1 ;;
        -h|--help)   usage; exit 0 ;;
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
