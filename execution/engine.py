"""Trading engine: ties the tick stream, ORB strategy, risk checks and order router together."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime, time

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import MARKET_TZ
from core.logging_setup import audit
from db import repository as repo
from execution.order_router import OrderRouter
from execution.orb_strategy import ORBStrategy
from execution.risk_manager import AccountState, RiskManager
from ingestion.tick_listener import TickListener
from schemas import TradeSignal, TradeUpdate


class TradingEngine:
    def __init__(
        self,
        *,
        strategy: ORBStrategy,
        router: OrderRouter,
        risk: RiskManager,
        listener: TickListener,
        session_factory: async_sessionmaker,
        square_off_time: time,
        reconcile_interval: float = 3.0,
    ) -> None:
        self._strategy = strategy
        self._router = router
        self._risk = risk
        self._listener = listener
        self._sf = session_factory
        self._square_off = square_off_time
        self._reconcile_interval = reconcile_interval

    async def run(self, stop: asyncio.Event) -> None:
        """Consume ticks until ``stop`` is set, square-off time passes, or the feed is lost."""
        reconciler = asyncio.create_task(self._reconcile_loop())
        audit("session_started", mode=self._router.mode, tracked=self._strategy.tracked_symbols)
        try:
            while not stop.is_set():
                if self._past_square_off():
                    logger.info("engine.square_off_time_reached")
                    break
                if self._listener.fatal.is_set():
                    logger.critical("engine.feed_lost_stopping")
                    break
                tick = await self._listener.next_tick(timeout=1.0)
                if tick is None:
                    continue
                await self._persist(await self._router.on_tick(tick))
                signal = self._strategy.on_tick(tick)
                if signal is not None:
                    await self._handle_signal(signal)
        finally:
            reconciler.cancel()
            with suppress(asyncio.CancelledError):
                await reconciler

    async def shutdown(self, reason: str) -> None:
        """Flatten all exposure and wait (bounded) until the broker confirms it."""
        await self._persist(await self._router.flatten_all(reason))
        for _ in range(20):
            if self._router.open_position_count() == 0:
                break
            await asyncio.sleep(3)
            await self._persist(await self._router.reconcile())
        audit(
            "session_ended",
            reason=reason,
            open_positions=self._router.open_position_count(),
            realised_pnl=round(self._router.realised_pnl(), 2),
        )

    # ------------------------------------------------------------------ internals
    def _past_square_off(self) -> bool:
        return datetime.now(MARKET_TZ).time() >= self._square_off

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self._persist(await self._router.reconcile())
            except Exception as exc:
                logger.error("engine.reconcile_error", error=repr(exc))
            await asyncio.sleep(self._reconcile_interval)

    async def _handle_signal(self, signal: TradeSignal) -> None:
        state = AccountState(
            available_margin=await self._router.available_margin(),
            realised_pnl=self._router.realised_pnl(),
            unrealised_pnl=self._router.unrealised_pnl(),
            open_positions=self._router.open_position_count(),
        )
        decision = self._risk.evaluate(signal, state)
        audit(
            "signal_evaluated",
            ticker=signal.ticker,
            side=signal.side.value,
            entry=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            volume_ratio=signal.volume_ratio,
            reason=signal.reason,
            approved=decision.approved,
            decision=decision.reason,
        )
        if not decision.approved or decision.size is None:
            return

        order = await self._router.place_bracket(signal, decision.size.quantity)
        async with self._sf() as session, session.begin():
            await repo.create_trade(
                session,
                trade_date=datetime.now(MARKET_TZ).date(),
                order=order,
                signal_reason=signal.reason,
            )
        audit(
            "order_placed",
            order_id=order.order_id,
            ticker=order.ticker,
            side=order.side.value,
            quantity=order.quantity,
            status=order.status.value,
            entry_price=order.entry_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            mode=order.mode,
        )

    async def _persist(self, updates: Sequence[TradeUpdate]) -> None:
        if not updates:
            return
        now = datetime.now(MARKET_TZ)
        for update in updates:
            audit(
                "trade_update",
                order_id=update.order_id,
                ticker=update.ticker,
                status=update.status.value,
                entry_price=update.entry_price,
                exit_price=update.exit_price,
                pnl=update.pnl,
                exit_reason=update.exit_reason,
            )
        async with self._sf() as session, session.begin():
            for update in updates:
                await repo.apply_trade_update(session, update, closed_at=now)
