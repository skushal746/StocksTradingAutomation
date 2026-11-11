"""Retry helpers with exponential backoff and jitter."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from loguru import logger

T = TypeVar("T")


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 60.0) -> float:
    """Equal-jitter exponential backoff. ``attempt`` is zero-based."""
    ceiling = min(cap, base * (2**attempt))
    return ceiling / 2 + random.uniform(0, ceiling / 2)


def _always(_exc: BaseException) -> bool:
    return True


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_if: Callable[[BaseException], bool] = _always,
    op_name: str = "operation",
) -> T:
    """Run ``fn`` up to ``attempts`` times, retrying only when ``retry_if`` says so."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for attempt in range(attempts):
        try:
            return await fn()
        except Exception as exc:
            if attempt >= attempts - 1 or not retry_if(exc):
                raise
            delay = backoff_delay(attempt, base=base_delay, cap=max_delay)
            logger.warning(
                "retry.scheduled",
                op=op_name,
                attempt=attempt + 1,
                delay_s=round(delay, 2),
                error=repr(exc),
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
