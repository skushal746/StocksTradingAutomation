"""Structured JSON logging and the trade audit trail (loguru)."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def _is_audit(record) -> bool:  # type: ignore[no-untyped-def]
    return bool(record["extra"].get("audit"))


def configure_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """Configure JSON sinks: stderr, rotating app log, and an append-only audit trail."""
    logger.remove()
    logger.add(sys.stderr, level=level, serialize=True, enqueue=True)

    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    logger.add(
        path / "app.jsonl",
        level=level,
        serialize=True,
        rotation="50 MB",
        retention="30 days",
        enqueue=True,
    )
    # Audit trail: every signal decision, order, fill and exit. Kept for a year.
    logger.add(
        path / "trade_audit.jsonl",
        level="INFO",
        serialize=True,
        rotation="20 MB",
        retention="365 days",
        enqueue=True,
        filter=_is_audit,
    )


def audit(event: str, **fields: object) -> None:
    """Write a record to the trade audit trail."""
    logger.bind(audit=True, audit_event=event, **fields).info(event)
