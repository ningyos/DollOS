"""Entry point: python -m dollos --config <path>."""

import argparse
import asyncio
import sys
from pathlib import Path

from dollos.config import load_settings
from dollos.daemon import Daemon
from dollos.log import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(prog="dollos")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="path to TOML config file",
    )
    args = parser.parse_args()

    settings = load_settings(args.config)
    setup_logging(settings.log.level)

    daemon = Daemon(settings)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
