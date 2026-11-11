"""Asynchronous wrapper around Kite Connect's KiteTicker WebSocket.

KiteTicker is thread-based (it runs a Twisted reactor in a worker thread). All of its
callbacks execute on that thread, so every hand-off into asyncio goes through
``loop.call_soon_threadsafe``.

Reconnection: KiteTicker performs exponential-backoff reconnects itself
(``reconnect_max_tries`` / ``reconnect_max_delay``). Its reactor cannot be restarted
inside one process, so if the library gives up (``on_noreconnect``) we set ``fatal``
and the engine flattens positions and exits. Run it under a supervisor (systemd, etc.)
to restart the process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from kiteconnect import KiteTicker
from loguru import logger

from config import MARKET_TZ
from schemas import Tick


class TickListener:
    def __init__(
        self,
        *,
        api_key: str,
        access_token: str,
        symbols_by_token: Mapping[int, str],
        reconnect_max_tries: int = 50,
        reconnect_max_delay: int = 60,
        queue_size: int = 10_000,
    ) -> None:
        if not symbols_by_token:
            raise ValueError("TickListener needs at least one instrument")
        self._api_key = api_key
        self._access_token = access_token
        self._symbols = dict(symbols_by_token)
        self._tokens = sorted(self._symbols)
        self._reconnect_max_tries = reconnect_max_tries
        self._reconnect_max_delay = reconnect_max_delay
        self._queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=queue_size)
        self._fatal = asyncio.Event()
        self._ticker: KiteTicker | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.dropped_ticks = 0

    @property
    def fatal(self) -> asyncio.Event:
        """Set when the WebSocket is permanently lost and will not reconnect."""
        return self._fatal

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        ticker = KiteTicker(
            self._api_key,
            self._access_token,
            reconnect=True,
            reconnect_max_tries=self._reconnect_max_tries,
            reconnect_max_delay=self._reconnect_max_delay,
        )
        ticker.on_ticks = self._on_ticks
        ticker.on_connect = self._on_connect
        ticker.on_close = self._on_close
        ticker.on_error = self._on_error
        ticker.on_reconnect = self._on_reconnect
        ticker.on_noreconnect = self._on_noreconnect
        self._ticker = ticker
        ticker.connect(threaded=True)  # non-blocking
        logger.info("tick_listener.started", instruments=len(self._tokens))

    async def next_tick(self, timeout: float) -> Tick | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def stop(self) -> None:
        ticker, self._ticker = self._ticker, None
        if ticker is None:
            return
        try:
            await asyncio.to_thread(ticker.close)
        except Exception as exc:  # shutdown must not raise
            logger.warning("tick_listener.close_failed", error=repr(exc))
        logger.info("tick_listener.stopped", dropped_ticks=self.dropped_ticks)

    # ------------------------------------------------------------------ ticker-thread callbacks
    def _dispatch(self, fn: Any, *args: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # loop shut down while a callback was in flight
            pass

    def _on_connect(self, ws: Any, response: Any) -> None:
        logger.info("tick_listener.connected")
        ws.subscribe(self._tokens)
        ws.set_mode(ws.MODE_FULL, self._tokens)

    def _on_ticks(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        received_at = datetime.now(MARKET_TZ)
        batch: list[Tick] = []
        for raw in ticks:
            token_raw = raw.get("instrument_token")
            price = raw.get("last_price")
            if token_raw is None or price is None:
                continue
            token = int(token_raw)
            symbol = self._symbols.get(token)
            if symbol is None:
                continue
            batch.append(
                Tick(
                    instrument_token=token,
                    symbol=symbol,
                    last_price=float(price),
                    cumulative_volume=int(raw.get("volume_traded") or 0),
                    received_at=received_at,
                )
            )
        if batch:
            self._dispatch(self._enqueue, batch)

    def _on_close(self, ws: Any, code: Any, reason: Any) -> None:
        logger.warning("tick_listener.closed", code=code, reason=reason)

    def _on_error(self, ws: Any, code: Any, reason: Any) -> None:
        logger.error("tick_listener.error", code=code, reason=reason)

    def _on_reconnect(self, ws: Any, attempts: Any) -> None:
        logger.warning("tick_listener.reconnecting", attempt=attempts)

    def _on_noreconnect(self, ws: Any) -> None:
        logger.critical("tick_listener.gave_up")
        self._dispatch(self._fatal.set)

    # ------------------------------------------------------------------ event-loop side
    def _enqueue(self, batch: list[Tick]) -> None:
        """Runs on the event loop. Drops the OLDEST tick when full: stale prices are worthless."""
        for tick in batch:
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                    self.dropped_ticks += 1
                except asyncio.QueueEmpty:
                    pass
            self._queue.put_nowait(tick)
