from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

import httpx

from paperless_sources import config as config_mod
from paperless_sources.paperless import PaperlessClient
from paperless_sources.providers import ProviderAuthError
from paperless_sources.providers import ProviderError
from paperless_sources.providers.registry import discover_providers
from paperless_sources.runner import SourceRunner
from paperless_sources.state import StateError
from paperless_sources.state import StateStore

logger = logging.getLogger("paperless_sources")

USER_AGENT = "paperless-sources/0.1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paperless-sources")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path(os.environ.get("PAPERLESS_SOURCES_CONFIG", "/config/config.toml")),
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Log in, list what would be imported and exit. Nothing is downloaded",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("PAPERLESS_SOURCES_LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO, and some URLs carry session IDs
    logging.getLogger("httpx").setLevel(logging.WARNING)

    providers = discover_providers()
    try:
        cfg = config_mod.load(args.config, providers)
    except config_mod.ConfigError as e:
        logger.error("Config: %s", e)
        return 2
    if not cfg.sources:
        logger.error("No enabled source in %s", args.config)
        return 2

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    try:
        state = StateStore(cfg.state_path)
    except StateError as e:
        logger.error("State: %s", e)
        return 2

    paperless = PaperlessClient(cfg.paperless_url, cfg.paperless_token)
    with (
        state,
        httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as http,
    ):
        runners = [
            SourceRunner(
                source,
                providers[source.provider],
                paperless,
                state,
                http,
                max_attempts=cfg.max_attempts,
                should_stop=stop.is_set,
            )
            for source in cfg.sources
        ]
        try:
            while not stop.is_set():
                for runner in runners:
                    if stop.is_set():
                        break
                    _run_one(runner, list_only=args.list_only)
                if args.once or args.list_only:
                    break
                stop.wait(cfg.poll_interval)
        finally:
            paperless.close()
    return 0


def _run_one(runner: SourceRunner, *, list_only: bool) -> None:
    # One broken source must not stop the others
    try:
        if list_only:
            runner.list_only()
        else:
            runner.run_cycle()
    except ProviderAuthError as e:
        logger.error("%s: login rejected, check the credentials: %s", runner.name, e)
    except ProviderError as e:
        logger.error("%s: provider failed: %s", runner.name, e)
    except Exception:
        logger.exception("%s: cycle failed", runner.name)


if __name__ == "__main__":
    sys.exit(main())
