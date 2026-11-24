"""In-RAM job queue and job state.

Deliberately not persisted: the queue survives server sleep cycles (the proxy
stays up) and a lost queue on a Pi restart only costs in-flight jobs, which
clients retry - the retry re-triggers the wake automatically.
"""
from __future__ import annotations

import asyncio
import heapq
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum

JOB_HISTORY_LIMIT = 512
END = object()  # stream sentinel


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobCancelled(Exception):
    """Client went away; stop working on this job."""


@dataclass
class Job:
    id: str
    kind: str  # "chat" | "completions"
    payload: dict
    prompt_text: str
    user: str = "default"
    priority: int = 0
    is_stream: bool = False
    prompt_tokens_est: int = 0
    is_large: bool = False
    state: JobState = JobState.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    ttfb_at: float | None = None
    error: str | None = None
    result: dict | None = None
    prompt_tokens_actual: int | None = None
    completion_tokens_actual: int | None = None
    cached_tokens_actual: int | None = None  # prompt tokens served from KV cache (subset of prompt)
    # LLM GPU time in seconds (llama.cpp timings: prompt_ms + predicted_ms).
    # None when the server did not report timings (fallback = job duration).
    compute_seconds_actual: float | None = None
    _seq: int = field(default=0, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.id is None:
            self.id = uuid.uuid4().hex

    # -- stream plumbing (route pumps, scheduler fills) --

    def init_stream(self) -> None:
        self._q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._cancel: asyncio.Event = asyncio.Event()

    @property
    def _queue(self) -> asyncio.Queue | None:
        return getattr(self, "_q", None)

    @property
    def cancelled(self) -> bool:
        ev = getattr(self, "_cancel", None)
        return ev is not None and ev.is_set()

    def request_cancel(self) -> None:
        ev = getattr(self, "_cancel", None)
        if ev is not None:
            ev.set()

    async def put_chunk(self, data: bytes) -> None:
        q = self._queue
        if q is None:
            return
        if self.cancelled:
            raise JobCancelled()
        while True:
            try:
                await asyncio.wait_for(q.put(data), timeout=1.0)
                return
            except TimeoutError:
                if self.cancelled:
                    raise JobCancelled() from None
                # else: slow client -> backpressure, retry

    async def put_sentinel(self, item: object) -> None:
        q = self._queue
        if q is None:
            return
        if self.cancelled:
            raise JobCancelled()
        while True:
            try:
                await asyncio.wait_for(q.put(item), timeout=1.0)
                return
            except TimeoutError:
                if self.cancelled:
                    raise JobCancelled() from None

    async def chunks(self):
        q = self._queue
        if q is None:
            return
        while True:
            item = await q.get()
            if item is END:
                break
            yield item


class JobQueue:
    """Priority heap: (-priority, created_at, seq). Higher priority first,
    then FIFO. Finished jobs are removed lazily."""

    def __init__(self) -> None:
        self._heap: list[tuple[float, float, int, Job]] = []
        self._by_id: OrderedDict[str, Job] = OrderedDict()
        self._in_flight: set[str] = set()
        self._seq = 0

    def add(self, job: Job) -> None:
        self._seq += 1
        job._seq = self._seq
        self._by_id[job.id] = job
        heapq.heappush(self._heap, (-job.priority, job.created_at, job._seq, job))
        while len(self._by_id) > JOB_HISTORY_LIMIT:
            self._by_id.popitem(last=False)

    def first_startable(self, can_start) -> Job | None:
        skipped: list[Job] = []
        while self._heap:
            job = heapq.heappop(self._heap)[3]
            if job.state is not JobState.QUEUED:
                continue  # stale heap entry
            if can_start(job):
                self._restore(skipped)
                self._in_flight.add(job.id)
                return job
            skipped.append(job)
        self._restore(skipped)
        return None

    def _restore(self, jobs: list[Job]) -> None:
        for j in jobs:
            heapq.heappush(self._heap, (-j.priority, j.created_at, j._seq, j))

    def finish(self, job: Job) -> None:
        self._in_flight.discard(job.id)

    def get(self, job_id: str) -> Job | None:
        return self._by_id.get(job_id)

    def pending(self) -> list[Job]:
        jobs = [e[3] for e in self._heap if e[3].state is JobState.QUEUED]
        jobs.sort(key=lambda j: (-j.priority, j.created_at, j._seq))
        return jobs

    def active(self) -> list[Job]:
        return [self._by_id[jid] for jid in self._in_flight if jid in self._by_id]

    def pending_count(self) -> int:
        return sum(1 for e in self._heap if e[3].state is JobState.QUEUED)

    def all_jobs(self) -> list[Job]:
        return list(self._by_id.values())

    def busy(self) -> bool:
        return bool(self._in_flight) or self.pending_count() > 0

    def public(self, job: Job, include_result: bool = False) -> dict:
        data = {
            "id": job.id,
            "kind": job.kind,
            "user": job.user,
            "priority": job.priority,
            "state": job.state.value,
            "is_large": job.is_large,
            "prompt_tokens_est": job.prompt_tokens_est,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "error": job.error,
        }
        if include_result and job.result is not None:
            data["result"] = job.result
        return data
