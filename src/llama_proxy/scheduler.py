"""Priority scheduler with the Large/Small prefill policy.

Policy (configurable):
  * Large job (new prompt tokens >= large_threshold_tokens):
      - large_exclusive=true  -> runs alone (blocks all other jobs)
      - large_exclusive=false -> runs alone among large jobs
  * Small jobs: up to small_max_concurrent in parallel
  * Order: priority desc, then FIFO
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from .config import Config
from .conversation import ConversationTracker
from .llmclient import LLMClient, LLMError
from .metrics import Metrics
from .power import PowerManager, ProxyState
from .state import END, Job, JobCancelled, JobQueue, JobState
from .tokenizer import TokenCounter
from .usage import UsageTracker

log = logging.getLogger("llama_proxy.scheduler")


class SSEAccumulator:
    """Incremental SSE parser that extracts content deltas and usage."""

    def __init__(self) -> None:
        self._buf = b""
        self.content = ""
        self.usage: dict | None = None

    def feed(self, data: bytes) -> None:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            choices = obj.get("choices") or [{}]
            delta = (choices[0] or {}).get("delta") or {}
            chunk = delta.get("content")
            if isinstance(chunk, str):
                self.content += chunk
            if obj.get("usage"):
                self.usage = obj["usage"]


class Scheduler:
    def __init__(
        self,
        cfg: Config,
        queue: JobQueue,
        llm: LLMClient,
        power: PowerManager,
        tracker: ConversationTracker,
        counter: TokenCounter,
        usage: UsageTracker,
        metrics: Metrics,
    ) -> None:
        self._cfg = cfg
        self._queue = queue
        self._llm = llm
        self._power = power
        self._tracker = tracker
        self._counter = counter
        self._usage = usage
        self._metrics = metrics
        self._wake = asyncio.Event()

    # -- public API ---------------------------------------------------------

    def enqueue(self, job: Job) -> Job:
        total = self._counter.count(job.prompt_text)
        job.prompt_tokens_est = total
        if self._cfg.scheduler.classification == "delta" and self._power.state is ProxyState.READY:
            effective = self._tracker.delta_for(job.prompt_text, total, self._counter)
        else:
            effective = total  # cold server / first turn / "total" mode -> full prefill
        self._tracker.note(job.prompt_text, total)
        job.is_large = effective >= self._cfg.scheduler.large_threshold_tokens
        self._queue.add(job)
        log.info(
            "queued job %s kind=%s large=%s est_tokens=%d priority=%d",
            job.id, job.kind, job.is_large, total, job.priority,
        )
        self._wake.set()
        return job

    async def run(self) -> None:
        """Dispatch loop: starts jobs as their class concurrency allows."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            self.dispatch_once()

    def dispatch_once(self) -> list[Job]:
        """Start every queued job that the current concurrency policy allows."""
        started: list[Job] = []
        while True:
            job = self._queue.first_startable(self._can_start)
            if job is None:
                break
            self._spawn(job)
            started.append(job)
        return started

    def _spawn(self, job: Job) -> None:
        task = asyncio.create_task(self._run_job(job), name=f"job-{job.id}")
        task.add_done_callback(lambda _t: self._wake.set())

    def _can_start(self, job: Job) -> bool:
        active = self._queue.active()
        active_large = any(j.is_large for j in active)
        active_small = sum(1 for j in active if not j.is_large)
        if job.is_large:
            if self._cfg.scheduler.large_exclusive:
                return len(active) == 0
            return not active_large
        return active_small < self._cfg.scheduler.small_max_concurrent

    def busy(self) -> bool:
        return self._queue.busy()

    # -- job execution ------------------------------------------------------

    async def _run_job(self, job: Job) -> None:
        job.state = JobState.RUNNING
        job.started_at = time.time()
        self._power.note_activity()
        try:
            await self._execute(job)
            job.state = JobState.DONE
        except JobCancelled:
            job.state = JobState.FAILED
            job.error = "client disconnected"
            log.info("job %s cancelled (client gone)", job.id)
        except Exception as exc:  # noqa: BLE001 - any failure fails the job
            job.state = JobState.FAILED
            job.error = str(exc)
            log.warning("job %s failed: %s", job.id, exc)
            if job.is_stream and job._queue is not None:
                await self._signal_stream_error(job, str(exc))
        finally:
            job.finished_at = time.time()
            self._queue.finish(job)
            self._power.note_activity()
            self._finalize_metrics(job)

    async def _execute(self, job: Job) -> None:
        """Run one job; re-wakes the server once if it just went away."""
        await self._power.ensure_ready()
        if job.cancelled:
            raise JobCancelled()
        try:
            await self._serve(job)
        except Exception as exc:  # noqa: BLE001
            if not (
                not job.is_stream
                and self._is_connection_error(exc)
                and self._power.recently_healthy()
            ):
                raise
            # We believed the server was up but the request hit a dead
            # connection: the host probably just powered off (idle-shutdown
            # race) or crashed. Wake it, wait for boot, retry once.
            log.warning(
                "job %s: server unreachable (%s); waking and retrying once", job.id, exc
            )
            self._power.degrade()
            await self._power.ensure_ready()
            if job.cancelled:
                raise JobCancelled() from None
            await self._serve(job)

    async def _serve(self, job: Job) -> None:
        if job.is_stream and job._queue is not None:
            await self._run_stream(job)
        else:
            data = await self._llm.forward(job)
            job.ttfb_at = time.time()
            job.result = data

    @staticmethod
    def _is_connection_error(exc: BaseException) -> bool:
        """Transport-level failure (server gone), as opposed to an HTTP error."""
        return isinstance(exc, httpx.TransportError)

    async def _run_stream(self, job: Job) -> None:
        acc = SSEAccumulator()
        async with self._llm.stream(job) as upstream:
            if upstream.status_code >= 400:
                body = await upstream.aread()
                raise LLMError(f"upstream {upstream.status_code}: {body[:300]!r}")
            async for chunk in upstream.aiter_bytes():
                acc.feed(chunk)
                if job.ttfb_at is None:
                    job.ttfb_at = time.time()
                await job.put_chunk(chunk)
        # finalise
        usage = acc.usage or {}
        job.prompt_tokens_actual = usage.get("prompt_tokens") or job.prompt_tokens_est
        job.completion_tokens_actual = usage.get("completion_tokens")
        if job.completion_tokens_actual is None:
            job.completion_tokens_actual = self._counter.count(acc.content) if acc.content else 0
        job.result = {
            "role": "assistant",
            "content": acc.content,
            "usage": {
                "prompt_tokens": job.prompt_tokens_actual,
                "completion_tokens": job.completion_tokens_actual,
            },
        }
        await job.put_sentinel(END)

    async def _signal_stream_error(self, job: Job, message: str) -> None:
        try:
            await job.put_chunk(
                f'data: {json.dumps({"error": {"message": message}})}\n\n'.encode()
            )
            await job.put_chunk(b"data: [DONE]\n\n")
            await job.put_sentinel(END)
        except JobCancelled:
            pass
        except Exception:  # noqa: BLE001 - best effort
            pass

    def _finalize_metrics(self, job: Job) -> None:
        m = self._metrics
        outcome = "ok" if job.state is JobState.DONE else "failed"
        m.requests_total.inc(result=outcome, kind=job.kind)
        if job.started_at is not None and job.finished_at is not None:
            klass = "large" if job.is_large else "small"
            m.duration_seconds.observe(job.finished_at - job.started_at, class_=klass)
        if job.ttfb_at is not None and job.started_at is not None:
            m.ttfb_seconds.observe(job.ttfb_at - job.started_at)
        if job.state is JobState.DONE:
            p_tok = job.prompt_tokens_actual or 0
            c_tok = job.completion_tokens_actual or 0
            m.tokens_total.inc(kind="prompt", amount=float(p_tok), user=job.user)
            m.tokens_total.inc(kind="completion", amount=float(c_tok), user=job.user)
            self._usage.record(job.user, p_tok, c_tok)
