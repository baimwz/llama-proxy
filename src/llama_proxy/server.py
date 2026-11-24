"""Application wiring: builds all components and registers routes/tasks."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from . import __version__
from .auth import Auth
from .config import Config
from .conversation import ConversationTracker
from .keys import KeyStore
from .llmclient import LLMClient
from .load import RecentLoad
from .metrics import Metrics
from .power import PowerManager
from .ratelimit import RateLimiter
from .routes_admin import setup as setup_admin
from .routes_openai import setup as setup_openai
from .routes_ops import setup as setup_ops
from .routes_web import setup as setup_web
from .scheduler import Scheduler
from .state import JobQueue
from .tokenizer import TokenCounter
from .usage import UsageTracker

log = logging.getLogger("llama_proxy.server")


def create_app(cfg: Config) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["cfg"] = cfg

    metrics = Metrics()
    app["metrics"] = metrics
    app["counter"] = TokenCounter(
        cfg.tokenizer.url,
        cache_file=(Path(cfg.usage.data_dir) / "tokenizer.json"),
    )
    app["tracker"] = ConversationTracker(
        cfg.scheduler.conv_state_ttl_s, cfg.scheduler.conv_state_max
    )
    app["llm"] = LLMClient(cfg.llm)
    app["power"] = PowerManager(cfg, app["llm"].health, app["tracker"], metrics)
    app["queue"] = JobQueue()
    app["load"] = RecentLoad()
    app["usage"] = UsageTracker(cfg.usage.data_dir, cfg.usage.flush_interval_s)
    app["keys"] = KeyStore(
        Path(cfg.usage.data_dir) / "keys.json",
        default_priority=cfg.users.default_priority,
        default_max_priority=cfg.users.default_max_priority,
        default_requests_per_hour=cfg.users.default_requests_per_hour,
        default_tokens_per_day=cfg.users.default_tokens_per_day,
    )
    app["limiter"] = RateLimiter()
    app["auth"] = Auth(app["keys"], app["limiter"], app["usage"])
    app["scheduler"] = Scheduler(
        cfg, app["queue"], app["llm"], app["power"], app["tracker"],
        app["counter"], app["usage"], app["load"], metrics,
    )

    # live gauges
    metrics.state.value_fn = lambda: {
        "off": 0, "waking": 1, "ready": 2, "shutting_down": 3,
    }[app["power"].state.value]
    metrics.idle_seconds.value_fn = lambda: app["power"].idle_seconds
    metrics.queue_size.value_fn = lambda: app["queue"].pending_count()
    def _small_active() -> float:
        return sum(1 for j in app["queue"].active() if not j.is_large)

    def _large_active() -> float:
        return sum(1 for j in app["queue"].active() if j.is_large)

    metrics.jobs_active_small.value_fn = _small_active
    metrics.jobs_active_large.value_fn = _large_active

    def _record_idle_gap(gap_s: float, users: list[str], share_s: float) -> None:
        """Book a settled idle gap: equal share per user + idle energy (Wh)."""
        idle_w = cfg.power.power_idle_w
        for u in users:
            app["usage"].record(
                u, 0, 0, requests=0,
                idle_seconds=share_s,
                energy_wh=share_s * idle_w / 3600.0,
            )

    async def on_startup(_app: web.Application) -> None:
        log.info("llama-proxy %s starting (llm=%s)", __version__, cfg.llm.base_url)
        await app["counter"].load()
        app["power"].set_idle_gap_handler(_record_idle_gap)
        # probe: maybe the server is already up
        if await app["llm"].health():
            app["power"].mark_ready()
            log.info("LLM server already reachable; starting READY")
        tasks = [
            asyncio.create_task(app["scheduler"].run(), name="scheduler"),
            asyncio.create_task(
                app["power"].idle_watchdog(app["scheduler"].busy), name="idle-watchdog"
            ),
            asyncio.create_task(app["usage"].flush_loop(), name="usage-flush"),
        ]
        app["tasks"] = tasks

    async def on_cleanup(_app: web.Application) -> None:
        for t in app.get("tasks", []):
            t.cancel()
        await asyncio.gather(*app.get("tasks", []), return_exceptions=True)
        await app["power"].aclose()
        await app["llm"].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    setup_openai(app)
    setup_ops(app)
    setup_admin(app)
    setup_web(app)
    return app


def main(host: str, port: int, cfg: Config) -> None:
    web.run_app(create_app(cfg), host=host, port=port)
