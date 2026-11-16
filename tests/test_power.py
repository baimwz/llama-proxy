import asyncio

import pytest

from llama_proxy.config import Config
from llama_proxy.conversation import ConversationTracker
from llama_proxy.metrics import Metrics
from llama_proxy.power import PowerError, PowerManager, ProxyState


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
