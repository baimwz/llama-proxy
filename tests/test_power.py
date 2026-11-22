import asyncio

import pytest

from llama_proxy.config import Config
from llama_proxy.conversation import ConversationTracker
from llama_proxy.metrics import Metrics
from llama_proxy.power import (
    WOL_PORTS,
    PowerError,
    PowerManager,
    ProxyState,
    SshPoweroff,
    build_magic_packet,
    default_remote_poweroff,
)


class FakeLLM:
    def __init__(self, ready_after: int = 0, healthy: bool = True) -> None:
        self.ready_after = ready_after
        self.healthy = healthy
        self.calls = 0

    async def health(self) -> bool:
        self.calls += 1
        return self.healthy and self.calls > self.ready_after


def make_pm(tmp_path, fake: FakeLLM, **cfg_over) -> PowerManager:
    cfg = Config.defaults()
    for section, value in cfg_over.items():
        for k, v in value.items():
            setattr(getattr(cfg, section), k, v)
    tracker = ConversationTracker(1800, 128)
    metrics = Metrics()
    return PowerManager(cfg, fake.health, tracker, metrics)


async def test_wake_success(tmp_path):
    fake = FakeLLM(ready_after=2)
    pm = make_pm(tmp_path, fake, llm={"health_interval_s": 0.01, "boot_timeout_s": 5})
    pm.state = ProxyState.OFF
    await pm.ensure_ready()
    assert pm.state is ProxyState.READY


async def test_wake_failure(tmp_path):
    fake = FakeLLM(ready_after=999)
    pm = make_pm(
        tmp_path, fake,
        llm={"health_interval_s": 0.01, "boot_timeout_s": 0.2},
    )
    pm.state = ProxyState.OFF
    with pytest.raises(PowerError):
        await pm.ensure_ready()
    assert pm.state is ProxyState.OFF


async def test_ready_short_circuit(tmp_path):
    fake = FakeLLM()
    pm = make_pm(tmp_path, fake)
    pm.mark_ready()
    await pm.ensure_ready()
    assert fake.calls == 0


async def test_concurrent_wake_single_poweron(tmp_path):
    fake = FakeLLM(ready_after=3)
    pm = make_pm(tmp_path, fake, llm={"health_interval_s": 0.01, "boot_timeout_s": 5})
    on_calls: list[int] = []

    async def power_on() -> None:
        on_calls.append(1)
        await asyncio.sleep(0.01)

    pm.set_backends(power_on=power_on)
    pm.state = ProxyState.OFF
    results = await asyncio.gather(*[pm.ensure_ready() for _ in range(5)], return_exceptions=True)
    assert all(not isinstance(r, Exception) for r in results)
    assert len(on_calls) == 1
    assert pm.state is ProxyState.READY


async def test_wol_resends_while_booting(tmp_path):
    # The host may still be finishing its shutdown when the first WoL packet
    # goes out and only reach S5 later. WoL must be re-sent during the wake
    # window so a late-reaching host still gets woken.
    fake = FakeLLM(ready_after=50)
    pm = make_pm(
        tmp_path, fake,
        power={"wol_resend_interval_s": 0.05},
        llm={"health_interval_s": 0.01, "boot_timeout_s": 5},
    )
    on_calls: list[int] = []

    async def power_on() -> None:
        on_calls.append(1)

    pm.set_backends(power_on=power_on)
    pm.state = ProxyState.OFF
    await pm.ensure_ready()
    assert pm.state is ProxyState.READY
    assert len(on_calls) >= 3  # initial send plus re-sends


async def test_wol_single_send_when_resend_disabled(tmp_path):
    fake = FakeLLM(ready_after=50)
    pm = make_pm(
        tmp_path, fake,
        power={"wol_resend_interval_s": 0.0},
        llm={"health_interval_s": 0.01, "boot_timeout_s": 5},
    )
    on_calls: list[int] = []

    async def power_on() -> None:
        on_calls.append(1)

    pm.set_backends(power_on=power_on)
    pm.state = ProxyState.OFF
    await pm.ensure_ready()
    assert pm.state is ProxyState.READY
    assert len(on_calls) == 1  # resend disabled -> exactly one packet


async def test_wol_resend_failure_not_fatal(tmp_path):
    # A failing re-send must not abort the wake (the first packet already went
    # out); only a failing FIRST send is fatal.
    fake = FakeLLM(ready_after=50)
    pm = make_pm(
        tmp_path, fake,
        power={"wol_resend_interval_s": 0.05},
        llm={"health_interval_s": 0.01, "boot_timeout_s": 5},
    )
    calls = {"n": 0}

    async def power_on() -> None:
        calls["n"] += 1
        if calls["n"] >= 3:
            raise OSError("no route to broadcast")

    pm.set_backends(power_on=power_on)
    pm.state = ProxyState.OFF
    await pm.ensure_ready()
    assert pm.state is ProxyState.READY


async def test_wol_first_send_failure_raises(tmp_path):
    fake = FakeLLM()
    pm = make_pm(
        tmp_path, fake,
        power={"wol_resend_interval_s": 0.0},
        llm={"health_interval_s": 0.01, "boot_timeout_s": 1},
    )

    async def boom() -> None:
        raise OSError("socket error")

    pm.set_backends(power_on=boom)
    pm.state = ProxyState.OFF
    with pytest.raises(PowerError):
        await pm.ensure_ready()
    assert pm.state is ProxyState.OFF


async def test_tracker_invalidate_on_wake(tmp_path):
    fake = FakeLLM(ready_after=1)
    cfg = Config.defaults()
    cfg.llm.health_interval_s = 0.01
    tracker = ConversationTracker(1800, 10)
    tracker.note("some prompt text that is long enough to count", 50)
    assert len(tracker) == 1
    pm = PowerManager(cfg, fake.health, tracker, Metrics())
    pm.state = ProxyState.OFF
    await pm.ensure_ready()
    assert len(tracker) == 0


async def test_shutdown_flow(tmp_path):
    fake = FakeLLM()
    pm = make_pm(
        tmp_path, fake,
        power={"shutdown_verify_timeout_s": 0.2},
        llm={"base_url": "http://nonexistent.invalid:8080"},
    )
    off_calls: list[int] = []

    async def power_off() -> None:
        off_calls.append(1)

    pm.set_backends(power_off=power_off)
    pm.mark_ready()
    await pm.shutdown()
    assert off_calls == [1]
    assert pm.state is ProxyState.OFF


async def test_shutdown_failure_keeps_ready(tmp_path):
    fake = FakeLLM()
    pm = make_pm(tmp_path, fake, power={"shutdown_verify_timeout_s": 0.2})

    async def boom() -> None:
        raise RuntimeError("no ssh")

    pm.set_backends(power_off=boom)
    pm.mark_ready()
    await pm.shutdown()
    assert pm.state is ProxyState.READY


async def test_no_backends_shutdown_noop(tmp_path):
    fake = FakeLLM()
    pm = make_pm(tmp_path, fake)
    pm.mark_ready()
    await pm.shutdown()
    assert pm.state is ProxyState.READY


async def test_idle_watchdog_triggers_shutdown(tmp_path):
    fake = FakeLLM()
    # unresolvable host -> off-verification (ping) fails fast, like a real poweroff
    pm = make_pm(
        tmp_path, fake,
        proxy={"idle_timeout_s": 0.05},
        llm={"base_url": "http://nonexistent.invalid:8080"},
    )

    async def power_off() -> None:
        pass

    pm.set_backends(power_off=power_off)
    pm.mark_ready()
    task = asyncio.create_task(pm.idle_watchdog(lambda: False, interval=0.05))
    try:
        for _ in range(200):
            if pm.state is ProxyState.OFF:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
    assert pm.state is ProxyState.OFF


async def test_idle_watchdog_stays_busy(tmp_path):
    fake = FakeLLM()
    pm = make_pm(tmp_path, fake, proxy={"idle_timeout_s": 0.05})
    pm.mark_ready()
    task = asyncio.create_task(pm.idle_watchdog(lambda: True, interval=0.05))  # always busy
    try:
        await asyncio.sleep(0.3)
    finally:
        task.cancel()
    assert pm.state is ProxyState.READY


async def test_liveness_marks_off_when_server_dies(tmp_path):
    fake = FakeLLM(healthy=False)
    tracker = ConversationTracker(1800, 10)
    tracker.note("some long prompt text that should be remembered", 50)
    cfg = Config.defaults()
    cfg.llm.health_interval_s = 0.01
    pm = PowerManager(cfg, fake.health, tracker, Metrics())
    pm.mark_ready()
    task = asyncio.create_task(pm.idle_watchdog(lambda: False, interval=0.05))
    try:
        for _ in range(200):
            if pm.state is ProxyState.OFF:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
    assert pm.state is ProxyState.OFF
    assert len(tracker) == 0  # server gone -> KV cache empty -> states invalid
    assert pm.last_healthy_at is not None  # "was healthy" anchor kept for re-wake


async def test_liveness_keeps_healthy_server_ready(tmp_path):
    fake = FakeLLM()
    pm = make_pm(tmp_path, fake, llm={"health_interval_s": 0.01})
    pm.mark_ready()
    task = asyncio.create_task(pm.idle_watchdog(lambda: True, interval=0.05))
    try:
        await asyncio.sleep(0.3)
    finally:
        task.cancel()
    assert pm.state is ProxyState.READY
    assert pm.recently_healthy()


async def test_recently_healthy_window(tmp_path):
    import time as _time

    fake = FakeLLM()
    pm = make_pm(tmp_path, fake)
    assert not pm.recently_healthy()  # never verified
    pm.mark_ready()
    assert pm.recently_healthy()
    pm.last_healthy_at = _time.time() - 999
    assert not pm.recently_healthy()


async def test_wol_ports_includes_confirmed(tmp_path):
    # Port 9 is the legacy port confirmed working on an ASRock B450 Pro4;
    # the common modern ports must also be tried.
    assert 9 in WOL_PORTS
    assert 7 in WOL_PORTS
    assert 40 in WOL_PORTS
    assert 4711 in WOL_PORTS


def test_magic_packet_format():
    pkt = build_magic_packet("aa:bb:cc:dd:ee:ff")
    assert len(pkt) == 6 + 6 * 16
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("aabbccddeeff")
    # the MAC is repeated 16 times
    assert pkt == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16


def test_default_remote_poweroff_by_user():
    assert default_remote_poweroff("root") == ["poweroff"]
    assert default_remote_poweroff("proxy") == ["sudo", "-n", "poweroff"]


def test_sshpoweroff_command_selection():
    # root -> plain poweroff
    assert SshPoweroff("h", "root", "k").remote_command == ["poweroff"]
    # non-root -> sudo -n poweroff
    assert SshPoweroff("h", "proxy", "k").remote_command == ["sudo", "-n", "poweroff"]
    # explicit custom command wins
    assert SshPoweroff("h", "root", "k", "pct stop 100 && poweroff").remote_command == [
        "pct", "stop", "100", "&&", "poweroff"
    ]


async def test_pre_shutdown_runs_before_poweroff(tmp_path):
    fake = FakeLLM()
    pm = make_pm(tmp_path, fake, power={"shutdown_verify_timeout_s": 0.2})
    order: list[str] = []

    async def pre() -> None:
        order.append("pre")

    async def off() -> None:
        order.append("off")

    pm.set_backends(power_off=off, pre_shutdown=pre)
    pm.mark_ready()
    await pm.shutdown()
    assert order == ["pre", "off"]
    assert pm.state is ProxyState.OFF


async def test_pre_shutdown_failure_still_powers_off(tmp_path):
    fake = FakeLLM()
    pm = make_pm(tmp_path, fake, power={"shutdown_verify_timeout_s": 0.2})
    order: list[str] = []

    async def pre_boom() -> None:
        order.append("pre")
        raise RuntimeError("pct not found")

    async def off() -> None:
        order.append("off")

    pm.set_backends(power_off=off, pre_shutdown=pre_boom)
    pm.mark_ready()
    await pm.shutdown()
    # pre-shutdown failed but we still powered off (host stops guests anyway)
    assert order == ["pre", "off"]
    assert pm.state is ProxyState.OFF
