"""structlog configuration for DollOS.

Configures cascade-specific structured JSONL output to file.
General daemon logging continues via stdlib `logging`.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import structlog


def configure_cascade_logging(log_root: Path) -> None:
    """Configure the 'cascade' structlog logger to emit JSONL to
    {log_root}/{date}.jsonl. Idempotent."""
    log_root.mkdir(parents=True, exist_ok=True)
    log_file = log_root / f"{date.today():%Y-%m-%d}.jsonl"

    # Ensure stdlib root logger has a file handler for cascade only.
    cascade_logger = logging.getLogger("cascade")
    cascade_logger.handlers.clear()
    cascade_logger.propagate = False  # don't bleed into root

    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    cascade_logger.addHandler(handler)
    cascade_logger.setLevel(logging.INFO)

    # structlog processor chain: timestamp + JSON
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
