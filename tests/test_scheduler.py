import asyncio
import tempfile
from contextlib import asynccontextmanager

import pytest

from llama_proxy.config import Config
from llama_proxy.conversation import ConversationTracker
from llama_proxy.load import RecentLoad
from llama_proxy.metrics import Metrics
from llama_proxy.power import PowerManager, ProxyState
from llama_proxy.scheduler import Scheduler
from llama_proxy.state import Job, JobQueue, JobState
from llama_proxy.tokenizer import TokenCounter
from llama_proxy.usage import UsageTracker


class StubLLM:
    """LLMClient stand-in: non-streaming only."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    async def health(self) -> bool:
        return True

    async def forward(self, job: Job) -> dict:
        await asyncio.sleep(self.delay)
        job.prompt_tokens_actual = 10
        job.completion_tokens_actual = 5
        return {"choices": [{"message": {"content": "ok"}}]}

    @asynccontextmanager
    async def stream(self, job):
        raise AssertionError("stream not used in these tests")

    async def close(self) -> None:
        pass


def make(**cfg_over) -> tuple[Scheduler, JobQueue, PowerManager]:
    cfg = Config.defaults()
    for section, value in cfg_over.items():
        for k, v in value.items():
            setattr(getattr(cfg, section), k, v)
    queue = JobQueue()
    llm = StubLLM()
    tracker = ConversationTracker(cfg.scheduler.conv_state_ttl_s, cfg.scheduler.conv_state_max)
    counter = TokenCounter("", None)  # heuristic, no network
    usage = UsageTracker(tempfile.mkdtemp(prefix="lp-test-"), 60)
    power = PowerManager(cfg, llm.health, tracker, Metrics())
    power.mark_ready()
    sched = Scheduler(
        cfg, queue, llm, power, tracker, counter, usage, RecentLoad(), Metrics()
    )
    return sched, queue, power


def mkjob(prompt: str, priority: int = 0) -> Job:
    return Job(
        id=None,
        kind="chat",
        payload={"messages": [{"role": "user", "content": prompt}]},
        prompt_text=prompt,
        priority=priority,
    )


async def wait_idle(queue: JobQueue, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while queue.active() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------


async def drain(sched, queue, jobs, rounds=5) -> None:
    """Mimic the live dispatch loop: dispatch, wait, dispatch, until all finish."""
    for _ in range(rounds):
        sched.dispatch_once()
        await wait_idle(queue)
        if all(j.state in (JobState.DONE, JobState.FAILED) for j in jobs):
            return
    raise AssertionError("jobs did not finish")


async def test_small_concurrency_limit():
    sched, queue, _ = make(scheduler={"small_max_concurrent": 2})
    jobs = [mkjob("a" * 100) for _ in range(3)]
    for j in jobs:
        sched.enqueue(j)
    started = sched.dispatch_once()
    assert len(started) == 2  # third waits for a slot
    await drain(sched, queue, jobs)
    assert all(j.state is JobState.DONE for j in jobs)


async def test_third_small_starts_after_slot_frees():
    sched, queue, _ = make(scheduler={"small_max_concurrent": 1})
    a, b = mkjob("a" * 50), mkjob("b" * 50)
    sched.enqueue(a)
    sched.enqueue(b)
    started = sched.dispatch_once()
    assert [j.id for j in started] == [a.id]
    await wait_idle(queue)
    started2 = sched.dispatch_once()
    assert [j.id for j in started2] == [b.id]
    await wait_idle(queue)
    assert a.state is JobState.DONE and b.state is JobState.DONE


async def test_large_exclusive_blocks_small():
    sched, queue, _ = make(
        scheduler={"large_threshold_tokens": 10, "large_exclusive": True, "small_max_concurrent": 2}
    )
    small = mkjob("x" * 20)       # 7 tokens -> small
    large = mkjob("y" * 200)      # 67 tokens -> large
    sched.enqueue(small)
    sched.enqueue(large)
    assert small.is_large is False and large.is_large is True
    started = sched.dispatch_once()
    assert [j.id for j in started] == [small.id]  # large blocked while small active
    await wait_idle(queue)
    started2 = sched.dispatch_once()
    assert [j.id for j in started2] == [large.id]
    await wait_idle(queue)


async def test_large_runs_alone():
    sched, queue, _ = make(scheduler={"large_threshold_tokens": 10, "large_exclusive": True})
    a, b = mkjob("y" * 200), mkjob("z" * 200)
    sched.enqueue(a)
    sched.enqueue(b)
    started = sched.dispatch_once()
    assert [j.id for j in started] == [a.id]  # second large waits
    await wait_idle(queue)
    started2 = sched.dispatch_once()
    assert [j.id for j in started2] == [b.id]


async def test_non_exclusive_allows_small_next_to_large():
    sched, queue, _ = make(
        scheduler={
            "large_threshold_tokens": 10,
            "large_exclusive": False,
            "small_max_concurrent": 2,
        }
    )
    small = mkjob("x" * 20)
    large = mkjob("y" * 200)
    sched.enqueue(small)
    sched.enqueue(large)
    started = sched.dispatch_once()
    assert {j.id for j in started} == {small.id, large.id}
    await wait_idle(queue)


async def test_priority_order():
    sched, queue, _ = make(scheduler={"small_max_concurrent": 2})
    low1, high, low2 = mkjob("a" * 50, 1), mkjob("b" * 50, 10), mkjob("c" * 50, 1)
    sched.enqueue(low1)
    sched.enqueue(high)
    sched.enqueue(low2)
    started = sched.dispatch_once()
    assert [j.id for j in started] == [high.id, low1.id]  # high priority first, then FIFO
    await wait_idle(queue)
    started2 = sched.dispatch_once()
    assert [j.id for j in started2] == [low2.id]


async def test_delta_classification_large_then_small():
    sched, queue, _ = make(scheduler={"large_threshold_tokens": 100})
    p1 = "a" * 900            # 300 tokens -> large
    p2 = p1 + "b" * 30        # +10 tokens -> small (delta)
    j1 = mkjob(p1)
    j2 = mkjob(p2)
    sched.enqueue(j1)
    sched.enqueue(j2)
    assert j1.is_large is True
    assert j2.is_large is False


async def test_cold_server_counts_full_prompt():
    sched, queue, power = make(scheduler={"large_threshold_tokens": 100})
    from llama_proxy.power import ProxyState

    p1 = "a" * 900
    sched.enqueue(mkjob(p1))  # READY -> large
    power.state = ProxyState.OFF  # simulate wake-from-sleep: KV cache is cold
    j2 = mkjob(p1 + "b" * 30)
    sched.enqueue(j2)
    assert j2.is_large is True  # full 310 tokens counted, not the delta


async def test_total_mode_ignores_delta():
    sched, queue, _ = make(scheduler={"large_threshold_tokens": 100, "classification": "total"})
    p1 = "a" * 900
    p2 = p1 + "b" * 30
    sched.enqueue(mkjob(p1))
    j2 = mkjob(p2)
    sched.enqueue(j2)
    assert j2.is_large is True  # 310 tokens >= 100 regardless of delta


async def test_failed_job_recorded():
    sched, queue, _ = make(scheduler={})

    class BrokenLLM(StubLLM):
        async def forward(self, job: Job) -> dict:
            raise RuntimeError("boom")

    # swap in the broken llm
    sched._llm = BrokenLLM()
    job = mkjob("hello")
    sched.enqueue(job)
    sched.dispatch_once()
    await wait_idle(queue)
    assert job.state is JobState.FAILED
    assert "boom" in (job.error or "")


async def test_phase_split_and_energy_recorded():
    sched, queue, _ = make(scheduler={})

    class TimedLLM(StubLLM):
        async def forward(self, job: Job) -> dict:
            await asyncio.sleep(0.01)
            job.prompt_tokens_actual = 10
            job.completion_tokens_actual = 5
            job.prefill_seconds_actual = 0.5
            job.generation_seconds_actual = 1.5
            job.compute_seconds_actual = 2.0
            return {"choices": []}

    sched._llm = TimedLLM()
    job = mkjob("hello")
    sched.enqueue(job)
    sched.dispatch_once()
    await wait_idle(queue)
    assert job.state is JobState.DONE
    t = sched._usage.totals()["default"]
    assert t["compute_seconds"] == pytest.approx(2.0)
    assert t["prefill_seconds"] == pytest.approx(0.5)
    assert t["generation_seconds"] == pytest.approx(1.5)
    # 0.5 s @ 270 W + 1.5 s @ 320 W (config defaults)
    assert t["energy_wh"] == pytest.approx((0.5 * 270 + 1.5 * 320) / 3600, rel=1e-6)
    assert t["idle_seconds"] == 0


async def test_phase_fallback_without_timings():
    # StubLLM reports no timings: the whole job duration goes to generation
    sched, queue, _ = make(scheduler={})
    job = mkjob("hello")
    sched.enqueue(job)
    sched.dispatch_once()
    await wait_idle(queue)
    assert job.state is JobState.DONE
    assert job.compute_seconds_actual is None
    t = sched._usage.totals()["default"]
    assert t["compute_seconds"] > 0
    assert t["prefill_seconds"] == 0
    assert t["generation_seconds"] == pytest.approx(t["compute_seconds"])
    # tolerance covers the 2-decimal rounding of the displayed seconds
    assert t["energy_wh"] == pytest.approx(
        t["generation_seconds"] * 320 / 3600, abs=0.001
    )


async def test_custom_power_config_changes_energy():
    # the watt values are config: changing them changes the energy, not the logic
    sched, queue, _ = make(power={"power_prefill_w": 100.0, "power_generation_w": 200.0})

    class TimedLLM(StubLLM):
        async def forward(self, job: Job) -> dict:
            job.prefill_seconds_actual = 1.0
            job.generation_seconds_actual = 1.0
            job.compute_seconds_actual = 2.0
            return {"choices": []}

    sched._llm = TimedLLM()
    job = mkjob("hello")
    sched.enqueue(job)
    sched.dispatch_once()
    await wait_idle(queue)
    t = sched._usage.totals()["default"]
    assert t["energy_wh"] == pytest.approx((1.0 * 100 + 1.0 * 200) / 3600, rel=1e-6)


async def test_nonstream_rewakes_on_dead_connection():
    """Server dies right before the request: re-wake once, then succeed."""
    import httpx

    sched, queue, power = make(scheduler={})

    class FlakyLLM(StubLLM):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def forward(self, job: Job) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("connection refused")
            await asyncio.sleep(0.02)
            job.prompt_tokens_actual = 10
            job.completion_tokens_actual = 5
            return {"choices": [{"message": {"content": "ok"}}]}

    llm = FlakyLLM()
    sched._llm = llm
    job = mkjob("hello")
    sched.enqueue(job)
    sched.dispatch_once()
    await wait_idle(queue)
    assert job.state is JobState.DONE
    assert llm.calls == 2
    assert power.state is ProxyState.READY  # woke itself up again


async def test_stream_not_retried_on_dead_connection():
    """Streams are partially delivered, so they must fail instead of retrying."""
    import httpx

    sched, queue, _ = make(scheduler={})

    class BrokenStreamLLM(StubLLM):
        async def forward(self, job: Job) -> dict:
            raise AssertionError("forward must not be called for stream jobs")

        @asynccontextmanager
        async def stream(self, job):
            raise httpx.ConnectError("connection refused")
            yield  # pragma: no cover

    sched._llm = BrokenStreamLLM()
    job = mkjob("hello")
    job.is_stream = True
    job.init_stream()
    sched.enqueue(job)
    sched.dispatch_once()
    await wait_idle(queue)
    assert job.state is JobState.FAILED
    assert "connection refused" in (job.error or "")
