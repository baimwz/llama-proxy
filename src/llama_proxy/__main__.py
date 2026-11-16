"""Command line interface: llama-proxy serve | version | selftest."""
from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import Config


def cmd_serve(args: argparse.Namespace) -> int:
    from aiohttp import web

    from .server import create_app

    cfg = Config.load(args.config)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not cfg.proxy.master_api_key:
        logging.getLogger("llama_proxy").warning(
            "proxy.master_api_key is empty - the API is OPEN. Only do this on a trusted network."
        )
    web.run_app(
        create_app(cfg),
        host=args.host or cfg.proxy.listen_host,
        port=args.port or cfg.proxy.listen_port,
    )
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """Check the configured LLM server and the local token counter."""
    import asyncio

    cfg = Config.load(args.config)

    async def run() -> int:
        import httpx

        ok = True
        url = cfg.llm.base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{url}/health")
                print(f"LLM server {url}: HTTP {resp.status_code} {resp.text[:120]}")
                ok = resp.status_code == 200
        except Exception as exc:  # noqa: BLE001
            print(f"LLM server {url}: UNREACHABLE ({exc})")
            ok = False

        from .tokenizer import TokenCounter

        counter = TokenCounter(cfg.tokenizer.url, None)
        await counter.load()
        sample = "Hello world, this is a tokenizer selftest." * 10
        suffix = f" ({counter.load_error})" if counter.backend != "huggingface" else ""
        print(f"Tokenizer backend: {counter.backend}{suffix}")
        print(f"Token count sample: {counter.count(sample)} tokens for {len(sample)} chars")
        sched = cfg.scheduler
        print(
            f"Scheduler: threshold={sched.large_threshold_tokens} tokens, "
            f"exclusive={sched.large_exclusive}, small_max={sched.small_max_concurrent}"
        )
        print("RESULT:", "OK" if ok else "LLM SERVER NOT REACHABLE (proxy would queue + wake)")
        return 0 if ok else 1

    return asyncio.run(run())


def _load_keystore(args: argparse.Namespace):
    from pathlib import Path

    from .keys import KeyStore

    cfg = Config.load(args.config)
    return KeyStore(
        Path(cfg.usage.data_dir) / "keys.json",
        default_priority=cfg.users.default_priority,
        default_max_priority=cfg.users.default_max_priority,
        default_requests_per_hour=cfg.users.default_requests_per_hour,
        default_tokens_per_day=cfg.users.default_tokens_per_day,
    )


def _print_users(users: list) -> None:
    if not users:
        print("no user keys yet")
        return
    header = (
        f"{'NAME':<16} {'PREFIX':<12} {'PRIO':<5} {'MAX':<4} "
        f"{'REQ/H':<7} {'TOK/DAY':<9} {'STATUS':<9} SINCE"
    )
    print(header)
    print("-" * len(header))
    for u in users:
        req_h = "unltd" if u.requests_per_hour == 0 else str(u.requests_per_hour)
        tok_d = "unltd" if u.tokens_per_day == 0 else f"{u.tokens_per_day}"
        status = "disabled" if u.disabled else "active"
        print(
            f"{u.name:<16} {u.prefix:<12} {u.priority:<5} {u.max_priority:<4} "
            f"{req_h:<7} {tok_d:<9} {status:<9} {u.created_at}"
        )


def cmd_keys(args: argparse.Namespace) -> int:
    """Manage per-user API keys (llama-proxy keys add|list|disable|enable|remove)."""
    store = _load_keystore(args)
    try:
        if args.keys_cmd == "add":
            record, plaintext = store.create(
                args.name,
                priority=args.priority,
                max_priority=args.max_priority,
                requests_per_hour=args.requests_per_hour,
                tokens_per_day=args.tokens_per_day,
            )
            print(f"API key for '{record.name}':")
            print(f"  {plaintext}")
            print()
            print("Store it now - it will not be shown again (only its hash is kept).")
        elif args.keys_cmd == "list":
            _print_users(store.list())
        elif args.keys_cmd == "disable":
            store.set_disabled(args.name, True)
            print(f"user '{args.name}' disabled")
        elif args.keys_cmd == "enable":
            store.set_disabled(args.name, False)
            print(f"user '{args.name}' enabled")
        elif args.keys_cmd == "remove":
            store.remove(args.name)
            print(f"user '{args.name}' removed")
    except (KeyError, ValueError) as exc:
        arg = exc.args[0] if exc.args else exc
        print(f"error: {arg}", file=sys.stderr)
        return 1
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    """Show per-user usage totals (llama-proxy usage [--days N] [--user NAME])."""
    from .usage import UsageTracker

    cfg = Config.load(args.config)
    tracker = UsageTracker(cfg.usage.data_dir, cfg.usage.flush_interval_s)
    totals = tracker.totals(days=args.days)
    if args.user:
        totals = {args.user: totals.get(args.user, {"prompt": 0, "completion": 0, "requests": 0})}
    if not totals:
        print("no usage data yet")
        return 0
    print(f"usage over the last {args.days} days")
    print(f"{'USER':<16} {'PROMPT':>12} {'COMPLETION':>12} {'REQUESTS':>10}")
    print("-" * 54)
    for name, row in sorted(totals.items()):
        print(f"{name:<16} {row['prompt']:>12,} {row['completion']:>12,} {row['requests']:>10,}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llama-proxy", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the proxy server")
    serve.add_argument("--config", default=None, help="path to config.toml")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("version", help="print version")
    selftest = sub.add_parser("selftest", help="check LLM reachability + tokenizer")
    selftest.add_argument("--config", default=None)

    keys = sub.add_parser("keys", help="manage per-user API keys (add|list|disable|enable|remove)")
    keys_sub = keys.add_subparsers(dest="keys_cmd", required=True)

    def _with_config(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--config", default=None, help="path to config.toml")
        return p

    add = _with_config(keys_sub.add_parser("add", help="create a new key"))
    add.add_argument("name", help="user name (a-z 0-9 . _ -)")
    add.add_argument("--priority", type=int, default=None, help="default priority 0-10")
    add.add_argument("--max-priority", type=int, default=None, help="X-Priority ceiling 0-10")
    add.add_argument("--requests-per-hour", type=int, default=None, help="0 = unlimited")
    add.add_argument("--tokens-per-day", type=int, default=None, help="0 = unlimited")

    _with_config(keys_sub.add_parser("list", help="list all users"))
    for verb in ("disable", "enable", "remove"):
        _with_config(keys_sub.add_parser(verb, help=f"{verb} a user's key")).add_argument("name")

    usage = sub.add_parser("usage", help="show per-user usage totals")
    usage.add_argument("--config", default=None)
    usage.add_argument("--days", type=int, default=7)
    usage.add_argument("--user", default=None)

    args = parser.parse_args(argv)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "version":
        print(f"llama-proxy {__version__}")
        return 0
    if args.command == "selftest":
        return cmd_selftest(args)
    if args.command == "keys":
        return cmd_keys(args)
    if args.command == "usage":
        return cmd_usage(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
