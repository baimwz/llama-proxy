"""Minimal Prometheus text-exposition metrics (no external dependency)."""
from __future__ import annotations

from collections.abc import Callable


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_labels(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(pairs))
    return "{" + inner + "}"


class Counter:
    def __init__(self, name: str, help_: str) -> None:
        self._name = name
        self._help = help_
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        self._values[key] = self._values.get(key, 0.0) + amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self._name} {self._help}", f"# TYPE {self._name} counter"]
        for key, val in sorted(self._values.items(), key=lambda kv: repr(kv[0])):
            lines.append(f"{self._name}{_fmt_labels(key)} {val:g}")
        return lines


class Gauge:
    def __init__(self, name: str, help_: str, value_fn: Callable[[], float] | None = None) -> None:
        self._name = name
        self._help = help_
        self._value = 0.0
        self._value_fn = value_fn

    def set(self, value: float) -> None:
        self._value = value
        self._value_fn = None

    def render(self) -> list[str]:
        value = self._value_fn() if self._value_fn is not None else self._value
        return [
            f"# HELP {self._name} {self._help}",
            f"# TYPE {self._name} gauge",
            f"{self._name} {value:g}",
        ]


class Histogram:
    DEFAULT_BUCKETS = (
        0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
        30.0, 60.0, 120.0, 300.0, 600.0,
    )

    def __init__(self, name: str, help_: str, buckets: tuple[float, ...] | None = None) -> None:
        self._name = name
        self._help = help_
        self._buckets = tuple(sorted(buckets or self.DEFAULT_BUCKETS))
        self._counts: dict[tuple[tuple[str, str], ...], list[int]] = {}
        self._totals: dict[tuple[tuple[str, str], ...], int] = {}
        self._sums: dict[tuple[tuple[str, str], ...], float] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        row = self._counts.setdefault(key, [0] * (len(self._buckets) + 1))
        for i, bound in enumerate(self._buckets):
            if value <= bound:
                row[i] += 1
        row[-1] += 1
        self._totals[key] = self._totals.get(key, 0) + 1
        self._sums[key] = self._sums.get(key, 0.0) + value

    def render(self) -> list[str]:
        lines = [f"# HELP {self._name} {self._help}", f"# TYPE {self._name} histogram"]
        for key in sorted(self._counts, key=repr):
            row = self._counts[key]
            base = dict(key)
            for i, bound in enumerate(self._buckets):
                lb = dict(base)
                lb["le"] = f"{bound:g}"
                labels = _fmt_labels(tuple(sorted(lb.items())))
                lines.append(f"{self._name}_bucket{labels} {row[i]}")
            lb = dict(base)
            lb["le"] = "+Inf"
            labels = _fmt_labels(tuple(sorted(lb.items())))
            lines.append(f"{self._name}_bucket{labels} {row[-1]}")
            lines.append(f"{self._name}_sum{_fmt_labels(tuple(key))} {self._sums.get(key, 0.0):g}")
            lines.append(f"{self._name}_count{_fmt_labels(tuple(key))} {self._totals.get(key, 0)}")
        return lines


class Metrics:
    """Registry with the metrics used by this proxy."""

    def __init__(self) -> None:
        self.state = Gauge(
            "llama_proxy_state", "Proxy state: 0=off 1=waking 2=ready 3=shutting_down"
        )
        self.idle_seconds = Gauge("llama_proxy_idle_seconds", "Seconds since last client activity")
        self.queue_size = Gauge("llama_proxy_queue_size", "Jobs waiting in the queue")
        self.jobs_active_small = Gauge("llama_proxy_jobs_active_small", "Active small jobs")
        self.jobs_active_large = Gauge("llama_proxy_jobs_active_large", "Active large jobs")
        self.requests_total = Counter(
            "llama_proxy_requests_total", "Requests by outcome and kind"
        )
        self.tokens_total = Counter(
            "llama_proxy_tokens_total", "Tokens by kind (prompt|completion)"
        )
        self.ttfb_seconds = Histogram(
            "llama_proxy_ttfb_seconds", "Time to first byte/token (s)",
            buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
        )
        self.duration_seconds = Histogram(
            "llama_proxy_duration_seconds", "Total job duration (s)",
            buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1200),
        )
        self.boot_seconds = Histogram(
            "llama_proxy_boot_seconds", "Wake-to-ready duration (s)",
            buckets=(10, 30, 60, 120, 180, 300, 600),
        )
        self.shutdowns_total = Counter("llama_proxy_shutdowns_total", "Server shutdowns issued")
        self.wake_failures_total = Counter(
            "llama_proxy_wake_failures_total", "Failed wake attempts"
        )
        self._series: list = [
            self.state, self.idle_seconds, self.queue_size,
            self.jobs_active_small, self.jobs_active_large,
            self.requests_total, self.tokens_total,
            self.ttfb_seconds, self.duration_seconds, self.boot_seconds,
            self.shutdowns_total, self.wake_failures_total,
        ]

    def render(self) -> str:
        lines: list[str] = []
        for series in self._series:
            lines.extend(series.render())
        return "\n".join(lines) + "\n"
