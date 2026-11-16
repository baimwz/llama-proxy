"""Power management: wake the Proxmox host (WoL), shut it down (SSH),
track the proxy state machine and run the idle watchdog.

States: OFF -> WAKING -> READY -> SHUTTING_DOWN -> OFF
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from urllib.parse import urlparse

from .config import Config
from .conversation import ConversationTracker
from .metrics import Metrics

log = logging.getLogger("llama_proxy.power")


class ProxyState(StrEnum):
    OFF = "off"
    WAKING = "waking"
    READY = "ready"
    SHUTTING = "shutting_down"


STATE_VALUES = {
    ProxyState.OFF: 0,
    ProxyState.WAKING: 1,
    ProxyState.READY: 2,
    ProxyState.SHUTTING: 3,
}


class PowerError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Wake-on-LAN
# ---------------------------------------------------------------------------

# Ports to try, in order. 9 and 7 are the legacy ports (confirmed working on
# an ASRock B450 Pro4); 40/4711 are the common modern ones. NICs differ, so
# send to all of them - the extra datagrams are free.
WOL_PORTS = (9, 7, 40, 4711)


def build_magic_packet(mac: str) -> bytes:
    b = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(b) != 6:
        raise ValueError(f"invalid MAC address: {mac!r}")
    return b"\xff" * 6 + b * 16


async def send_magic_packet(mac: str, broadcast: str = "255.255.255.255") -> None:
    pkt = build_magic_packet(mac)

    def _send() -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for port in WOL_PORTS:
                sock.sendto(pkt, (broadcast, port))
        finally:
            sock.close()

    await asyncio.to_thread(_send)


# ---------------------------------------------------------------------------
# SSH poweroff
# ---------------------------------------------------------------------------


def default_remote_poweroff(user: str) -> list[str]:
    """poweroff for root, `sudo -n poweroff` for a locked-down non-root user."""
    return ["poweroff"] if user == "root" else ["sudo", "-n", "poweroff"]


class SshPoweroff:
    def __init__(
        self,
        host: str,
        user: str,
        key_path: str,
        command: str = "",
    ) -> None:
        self._host = host
        self._user = user
        self._key = key_path
        # custom remote command wins; otherwise the sensible default for the user
        self._remote = command.split() if command else default_remote_poweroff(user)

    @property
    def remote_command(self) -> list[str]:
        return list(self._remote)

    async def _ssh(self, remote: list[str], timeout: float) -> None:
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-i", self._key,
            f"{self._user}@{self._host}",
            *remote,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (TimeoutError, OSError) as exc:
            raise PowerError(f"ssh command failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (err or b"").decode(errors="replace")[:300]
            raise PowerError(f"ssh command rc={proc.returncode}: {detail}")

    async def poweroff(self) -> None:
        await self._ssh(self._remote, timeout=30)

    async def run(self, remote_command: str, timeout: float = 60.0) -> None:
        """Run an arbitrary remote command (e.g. 'pct stop 100')."""
        if not remote_command.strip():
            return
        await self._ssh(remote_command.split(), timeout=timeout)


# ---------------------------------------------------------------------------
# Power manager
# ---------------------------------------------------------------------------


class PowerManager:
    def __init__(
        self,
        cfg: Config,
        health_check,
        tracker: ConversationTracker,
        metrics: Metrics,
    ) -> None:
        self._cfg = cfg
        self._health = health_check  # async () -> bool
        self._tracker = tracker
        self._metrics = metrics
        self._lock = asyncio.Lock()
        self._wake_task: asyncio.Task | None = None
        self._last_activity = time.time()
        self.state = ProxyState.OFF
        self.last_boot_s: float | None = None
        self.last_healthy_at: float | None = None

        p = cfg.power
        self._power_on = None
        if p.wol_mac:
            bcast = p.wol_bcast or "255.255.255.255"
            mac = p.wol_mac
            self._power_on = lambda: send_magic_packet(mac, bcast)
        self._pre_shutdown: Callable[[], Awaitable[None]] | None = None
        self._power_off: Callable[[], Awaitable[None]] | None = None
        if p.ssh_host:
            ssh = SshPoweroff(p.ssh_host, p.ssh_user, p.ssh_key_path, p.ssh_command)
            self._power_off = ssh.poweroff
            if p.pre_shutdown_command:
                self._pre_shutdown = lambda: ssh.run(p.pre_shutdown_command)
        if self._power_on is None and self._power_off is None:
            log.info("no power backend configured - assuming the LLM server is always on")

    # -- activity / idle ----------------------------------------------------

    def note_activity(self) -> None:
        self._last_activity = time.time()

    @property
    def idle_seconds(self) -> float:
        return max(0.0, time.time() - self._last_activity)

    def note_healthy(self) -> None:
        """Record that the server answered a health check."""
        self.last_healthy_at = time.time()

    def recently_healthy(self, within_s: float = 120.0) -> bool:
        """True if the server was verified healthy within `within_s`.

        Used to decide whether a dead connection means "the host just went
        off" (re-wake) versus "it was never reachable in the first place".
        """
        ts = self.last_healthy_at
        return ts is not None and (time.time() - ts) <= within_s

    def degrade(self) -> None:
        """Server stopped responding (powered off / crashed) -> back to OFF."""
        self.state = ProxyState.OFF
        self._tracker.invalidate()

    def mark_ready(self) -> None:
        self.state = ProxyState.READY
        self.last_healthy_at = time.time()

    # -- readiness ----------------------------------------------------------

    async def ensure_ready(self) -> None:
        """Wait until the LLM server answers health checks; wake it if needed."""
        if self.state is ProxyState.READY:
            return
        async with self._lock:
            if self.state is ProxyState.READY:
                return
            if self._wake_task is None or self._wake_task.done():
                self._wake_task = asyncio.create_task(self._do_wake())
            task = self._wake_task
        await task  # raises PowerError on failure

    async def _do_wake(self) -> None:
        t0 = time.monotonic()
        self.state = ProxyState.WAKING
        if self._power_on is not None:
            try:
                await self._power_on()
            except Exception as exc:
                self.state = ProxyState.OFF
                self._metrics.wake_failures_total.inc()
                raise PowerError(f"power-on failed: {exc}") from exc
        else:
            log.info("no power-on backend; polling health only")
        deadline = time.monotonic() + self._cfg.llm.boot_timeout_s
        while time.monotonic() < deadline:
            try:
                if await self._health():
                    self._tracker.invalidate()
                    self.state = ProxyState.READY
                    self.note_healthy()
                    self.note_activity()
                    self.last_boot_s = time.monotonic() - t0
                    self._metrics.boot_seconds.observe(self.last_boot_s)
                    log.info("server ready after %.1fs", self.last_boot_s)
                    return
            except Exception:  # noqa: BLE001 - health check is best-effort
                pass
            await asyncio.sleep(self._cfg.llm.health_interval_s)
        self.state = ProxyState.OFF
        self._metrics.wake_failures_total.inc()
        raise PowerError(f"server not healthy within {int(time.monotonic() - t0)}s after wake")

    # -- shutdown -----------------------------------------------------------

    async def shutdown(self) -> None:
        if self.state is not ProxyState.READY:
            return
        if self._power_off is None:
            log.info("no power-off backend configured; skipping shutdown")
            return
        self.state = ProxyState.SHUTTING
        if self._pre_shutdown is not None:
            try:
                await self._pre_shutdown()
                log.info("pre-shutdown command finished")
            except Exception as exc:  # noqa: BLE001
                # not fatal: a host poweroff stops guests anyway
                log.warning("pre-shutdown command failed (continuing): %s", exc)
        try:
            await self._power_off()
        except Exception as exc:  # noqa: BLE001
            log.warning("power-off failed: %s", exc)
            self.state = ProxyState.READY
            return
        try:
            await self._verify_off()
        except Exception as exc:  # noqa: BLE001
            log.warning("off-verification failed (assuming off): %s", exc)
        self._tracker.invalidate()
        self.state = ProxyState.OFF
        self._metrics.shutdowns_total.inc()
        log.info("server powered off")

    async def _verify_off(self) -> None:
        """Best effort: wait until the host stops answering ICMP."""
        host = urlparse(self._cfg.llm.base_url).hostname or self._cfg.power.ssh_host
        if not host:
            await asyncio.sleep(3)
            return
        deadline = time.monotonic() + self._cfg.power.shutdown_verify_timeout_s
        while time.monotonic() < deadline:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ping", "-c", "1", "-W", "1", host,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, OSError):
                rc = 1  # ping missing/unusable -> assume off
            if rc != 0:
                return
            await asyncio.sleep(1)
        # timeout: assume off anyway

    # -- idle watchdog ------------------------------------------------------

    async def idle_watchdog(self, busy_check, interval: float | None = None) -> None:
        """Keep the state honest and shut the server down after the idle
        timeout with nothing queued.

        Also polls the LLM server's health while we believe it is READY: if
        it stops answering (powered off, crashed), the state goes back to
        OFF so the next request triggers a proper wake instead of failing.
        """
        tick = interval if interval is not None else max(5.0, self._cfg.llm.health_interval_s)
        failures = 0
        while True:
            await asyncio.sleep(tick)
            if self.state is ProxyState.READY:
                try:
                    healthy = await self._health()
                except Exception:  # noqa: BLE001 - health check is best-effort
                    healthy = False
                if healthy:
                    failures = 0
                    self.note_healthy()
                else:
                    failures += 1
                    if failures >= 2:
                        log.warning("LLM server stopped responding; state -> off")
                        self.degrade()
                        failures = 0
                        continue
            if (
                self.state is ProxyState.READY
                and not busy_check()
                and self.idle_seconds >= self._cfg.proxy.idle_timeout_s
            ):
                log.info("idle timeout reached (%.0fs); shutting server down", self.idle_seconds)
                await self.shutdown()

    # -- lifecycle ----------------------------------------------------------

    def set_backends(self, power_on=None, power_off=None, pre_shutdown=None) -> None:
        """Override wake/shutdown backends (used in tests)."""
        if power_on is not None:
            self._power_on = power_on
        if power_off is not None:
            self._power_off = power_off
        if pre_shutdown is not None:
            self._pre_shutdown = pre_shutdown

    async def aclose(self) -> None:
        task = self._wake_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
