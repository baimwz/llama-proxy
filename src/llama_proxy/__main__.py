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

    args = parser.parse_args(argv)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "version":
        print(f"llama-proxy {__version__}")
        return 0
    if args.command == "selftest":
        return cmd_selftest(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
