"""Order routing behind one protocol: a paper router (default) and a live Kite router.

Paper:  simulates fills at signal price (+ slippage) and closes SL/TP from live ticks.
Live:   MIS LIMIT entry -> on fill, SL (SL-LIMIT) and TP (LIMIT) exit legs -> OCO cancel
        on whichever leg fills. Exit legs are placed only after the entry is COMPLETE,
        so the router never holds naked exit orders for an unfilled position.
        If leg placement fails after a fill, the position is flattened at market.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from kiteconnect import KiteConnect
from loguru import logger

from config import MARKET_TZ
from core.kite_client import round_to_tick
from schemas import OrderResult, Side, Tick, TradeSignal, TradeStatus, TradeUpdate

ENTRY_LIMIT_BUFFER = 0.001  # cross the spread slightly so the LIMIT entry fills promptly
SL_LIMIT_BUFFER = 0.002     # SL-LIMIT price sits beyond the trigger so the exit actually fills


def _sign(side: Side) -> int:
    return 1 if side is Side.BUY else -1


def _exit_side(side: Side) -> Side:
    return Side.SELL if side is Side.BUY else Side.BUY


def _leg_pcts(signal: TradeSignal) -> tuple[float, float]:
    stop_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
    target_pct = abs(signal.take_profit - signal.entry_price) / signal.entry_price
    return stop_pct, target_pct


class OrderRouter(Protocol):
    mode: str

    async def place_bracket(self, signal: TradeSignal, quantity: int) -> OrderResult: ...
    async def on_tick(self, tick: Tick) -> list[TradeUpdate]: ...
    async def reconcile(self) -> list[TradeUpdate]: ...
    async def flatten_all(self, reason: str) -> list[TradeUpdate]: ...
    async def available_margin(self) -> float: ...
    def open_position_count(self) -> int: ...
    def realised_pnl(self) -> float: ...
    def unrealised_pnl(self) -> float: ...
    def seed_realised_pnl(self, value: float) -> None: ...


# ====================================================================== paper
@dataclass
class _PaperPosition:
    order_id: str
    ticker: str
    token: int
    side: Side
    quantity: int
    entry_price: float
    stop_loss: float
    take_profit: float
    last_price: float


class PaperOrderRouter:
    """Simulated execution driven by live ticks. Never contacts the broker."""

    mode = "paper"

    def __init__(self, *, capital: float, margin_pct: float, slippage_pct: float) -> None:
        self._capital = capital
        self._margin_pct = margin_pct
        self._slippage = slippage_pct
        self._positions: dict[str, _PaperPosition] = {}
        self._realised = 0.0

    async def place_bracket(self, signal: TradeSignal, quantity: int) -> OrderResult:
        sign = _sign(signal.side)
        fill = round(signal.entry_price * (1 + sign * self._slippage), 2)
        stop_pct, target_pct = _leg_pcts(signal)
        order_id = f"PAPER-{uuid.uuid4().hex[:10].upper()}"
        position = _PaperPosition(
            order_id=order_id,
            ticker=signal.ticker,
            token=signal.instrument_token,
            side=signal.side,
            quantity=quantity,
            entry_price=fill,
            stop_loss=round(fill * (1 - sign * stop_pct), 2),
            take_profit=round(fill * (1 + sign * target_pct), 2),
            last_price=fill,
        )
        self._positions[order_id] = position
        logger.info("paper.position_opened", order_id=order_id, ticker=signal.ticker, qty=quantity, fill=fill)
        return OrderResult(
            order_id=order_id,
            ticker=signal.ticker,
            side=signal.side,
            quantity=quantity,
            status=TradeStatus.OPEN,
            mode=self.mode,
            entry_price=fill,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            created_at=datetime.now(MARKET_TZ),
        )

    async def on_tick(self, tick: Tick) -> list[TradeUpdate]:
        updates: list[TradeUpdate] = []
        for position in list(self._positions.values()):
            if position.token != tick.instrument_token:
                continue
            position.last_price = tick.last_price
            sign = _sign(position.side)
            # Stop is checked first: when both levels are crossed in one tick, assume the worse outcome.
            if sign * (tick.last_price - position.stop_loss) <= 0:
                # Gap-through fills at the worse of the stop and the actual price.
                raw = min(position.stop_loss, tick.last_price) if sign > 0 else max(position.stop_loss, tick.last_price)
                updates.append(self._close(position, raw, "STOP_LOSS"))
            elif sign * (tick.last_price - position.take_profit) >= 0:
                updates.append(self._close(position, position.take_profit, "TAKE_PROFIT"))
        return updates

    async def reconcile(self) -> list[TradeUpdate]:
        return []  # exits are driven by on_tick in paper mode

    async def flatten_all(self, reason: str) -> list[TradeUpdate]:
        return [self._close(p, p.last_price, reason) for p in list(self._positions.values())]

    async def available_margin(self) -> float:
        used = sum(p.entry_price * p.quantity * self._margin_pct for p in self._positions.values())
        return self._capital + self._realised - used

    def open_position_count(self) -> int:
        return len(self._positions)

    def realised_pnl(self) -> float:
        return self._realised

    def unrealised_pnl(self) -> float:
        return sum(_sign(p.side) * (p.last_price - p.entry_price) * p.quantity for p in self._positions.values())

    def seed_realised_pnl(self, value: float) -> None:
        self._realised = value

    def _close(self, position: _PaperPosition, raw_exit: float, reason: str) -> TradeUpdate:
        sign = _sign(position.side)
        exit_fill = round(raw_exit * (1 - sign * self._slippage), 2)
        pnl = round(sign * (exit_fill - position.entry_price) * position.quantity, 2)
        self._realised += pnl
        del self._positions[position.order_id]
        logger.info(
            "paper.position_closed",
            order_id=position.order_id,
            ticker=position.ticker,
            exit=exit_fill,
            pnl=pnl,
            reason=reason,
        )
        return TradeUpdate(
            order_id=position.order_id,
            ticker=position.ticker,
            status=TradeStatus.CLOSED,
            entry_price=position.entry_price,
            exit_price=exit_fill,
            pnl=pnl,
            exit_reason=reason,
        )


# ====================================================================== live (Kite)
@dataclass
class _LiveTrade:
    entry_order_id: str
    ticker: str
    token: int
    side: Side
    quantity: int
    limit_price: float
    stop_pct: float
    target_pct: float
    status: TradeStatus = TradeStatus.PENDING
    entry_price: float | None = None
    stop_order_id: str | None = None
    target_order_id: str | None = None
    flatten_order_id: str | None = None
    exit_reason: str | None = None  # set when a flatten has been requested


def _tag() -> str:
    """Kite tags are at most 20 alphanumeric characters."""
    return f"orb{uuid.uuid4().hex[:15]}"


class KiteOrderRouter:
    """Places real MIS orders through the Kite Connect REST API. Blocking SDK calls run in threads."""

    mode = "live"

    def __init__(self, kite: KiteConnect, *, exchange: str, capital: float, margin_pct: float) -> None:
        self._kite = kite
        self._exchange = exchange
        self._capital = capital
        self._margin_pct = margin_pct
        self._trades: dict[str, _LiveTrade] = {}  # keyed by entry order id
        self._last_price: dict[int, float] = {}
        self._realised = 0.0

    # ------------------------------------------------------------------ SDK plumbing
    async def _call(self, fn: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, **kwargs)

    async def _place(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        order_type: str,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "variety": "regular",
            "exchange": self._exchange,
            "tradingsymbol": symbol,
            "transaction_type": side.value,
            "quantity": quantity,
            "product": "MIS",
            "order_type": order_type,
            "validity": "DAY",
            "tag": _tag(),
        }
        if price is not None:
            kwargs["price"] = price
        if trigger_price is not None:
            kwargs["trigger_price"] = trigger_price
        return str(await self._call(self._kite.place_order, **kwargs))

    async def _cancel(self, order_id: str | None) -> None:
        if not order_id:
            return
        try:
            await self._call(self._kite.cancel_order, variety="regular", order_id=order_id)
        except Exception as exc:  # the leg may already be complete or cancelled
            logger.warning("live.cancel_failed", order_id=order_id, error=repr(exc))

    # ------------------------------------------------------------------ OrderRouter API
    async def place_bracket(self, signal: TradeSignal, quantity: int) -> OrderResult:
        sign = _sign(signal.side)
        limit = round_to_tick(signal.entry_price * (1 + sign * ENTRY_LIMIT_BUFFER))
        order_id = await self._place(
            symbol=signal.ticker, side=signal.side, quantity=quantity, order_type="LIMIT", price=limit
        )
        stop_pct, target_pct = _leg_pcts(signal)
        self._trades[order_id] = _LiveTrade(
            entry_order_id=order_id,
            ticker=signal.ticker,
            token=signal.instrument_token,
            side=signal.side,
            quantity=quantity,
            limit_price=limit,
            stop_pct=stop_pct,
            target_pct=target_pct,
        )
        logger.info("live.entry_submitted", order_id=order_id, ticker=signal.ticker, qty=quantity, limit=limit)
        return OrderResult(
            order_id=order_id,
            ticker=signal.ticker,
            side=signal.side,
            quantity=quantity,
            status=TradeStatus.PENDING,
            mode=self.mode,
            entry_price=None,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            created_at=datetime.now(MARKET_TZ),
        )

    async def on_tick(self, tick: Tick) -> list[TradeUpdate]:
        self._last_price[tick.instrument_token] = tick.last_price  # exits are broker-side; used for PnL only
        return []

    async def reconcile(self) -> list[TradeUpdate]:
        if not self._trades:
            return []
        orders = await self._call(self._kite.orders)
        by_id = {str(order["order_id"]): order for order in orders}
        updates: list[TradeUpdate] = []
        for trade in list(self._trades.values()):
            try:
                update = await self._advance(trade, by_id)
            except Exception as exc:
                logger.error("live.reconcile_failed", order_id=trade.entry_order_id, error=repr(exc))
                continue
            if update is not None:
                updates.append(update)
        return updates

    async def flatten_all(self, reason: str) -> list[TradeUpdate]:
        updates: list[TradeUpdate] = []
        for trade in list(self._trades.values()):
            if trade.flatten_order_id is not None or trade.exit_reason is not None:
                continue  # already closing
            if trade.status is TradeStatus.PENDING:
                trade.exit_reason = reason
                await self._cancel(trade.entry_order_id)  # reconcile() reports CANCELLED or fills + exits
            else:
                await self._cancel(trade.stop_order_id)
                await self._cancel(trade.target_order_id)
                await self._market_exit(trade, reason)
        return updates

    async def available_margin(self) -> float:
        margins = await self._call(self._kite.margins, segment="equity")
        return float(margins["available"]["live_balance"])

    def open_position_count(self) -> int:
        return len(self._trades)

    def realised_pnl(self) -> float:
        return self._realised

    def unrealised_pnl(self) -> float:
        total = 0.0
        for trade in self._trades.values():
            if trade.status is TradeStatus.OPEN and trade.entry_price is not None:
                last = self._last_price.get(trade.token)
                if last is not None:
                    total += _sign(trade.side) * (last - trade.entry_price) * trade.quantity
        return total

    def seed_realised_pnl(self, value: float) -> None:
        self._realised = value

    # ------------------------------------------------------------------ internals
    async def _advance(self, trade: _LiveTrade, by_id: dict[str, Any]) -> TradeUpdate | None:
        if trade.status is TradeStatus.PENDING:
            entry = by_id.get(trade.entry_order_id)
            if entry is None:
                return None
            if entry["status"] == "COMPLETE":
                trade.entry_price = float(entry.get("average_price") or trade.limit_price)
                trade.quantity = int(entry.get("filled_quantity") or trade.quantity)
                if trade.exit_reason is not None:  # flatten was requested while the entry was working
                    await self._market_exit(trade, trade.exit_reason)
                    return None
                await self._place_exit_legs(trade)
                trade.status = TradeStatus.OPEN
                return TradeUpdate(
                    order_id=trade.entry_order_id,
                    ticker=trade.ticker,
                    status=TradeStatus.OPEN,
                    entry_price=trade.entry_price,
                )
            if entry["status"] in ("REJECTED", "CANCELLED"):
                self._trades.pop(trade.entry_order_id, None)
                status = TradeStatus.REJECTED if entry["status"] == "REJECTED" else TradeStatus.CANCELLED
                return TradeUpdate(
                    order_id=trade.entry_order_id,
                    ticker=trade.ticker,
                    status=status,
                    exit_reason=trade.exit_reason or entry.get("status_message"),
                )
            return None

        if trade.flatten_order_id is not None:
            flatten = by_id.get(trade.flatten_order_id)
            if flatten is not None and flatten["status"] == "COMPLETE":
                return self._finish(trade, float(flatten.get("average_price") or 0.0), trade.exit_reason or "FLATTEN")
            return None

        stop = by_id.get(trade.stop_order_id or "")
        target = by_id.get(trade.target_order_id or "")
        if stop is not None and stop["status"] == "COMPLETE":
            await self._cancel(trade.target_order_id)
            return self._finish(trade, float(stop.get("average_price") or 0.0), "STOP_LOSS")
        if target is not None and target["status"] == "COMPLETE":
            await self._cancel(trade.stop_order_id)
            return self._finish(trade, float(target.get("average_price") or 0.0), "TAKE_PROFIT")
        return None

    async def _place_exit_legs(self, trade: _LiveTrade) -> None:
        assert trade.entry_price is not None
        sign = _sign(trade.side)
        exit_side = _exit_side(trade.side)
        stop_trigger = round_to_tick(trade.entry_price * (1 - sign * trade.stop_pct))
        stop_limit = round_to_tick(stop_trigger * (1 - sign * SL_LIMIT_BUFFER))
        target_price = round_to_tick(trade.entry_price * (1 + sign * trade.target_pct))
        try:
            trade.stop_order_id = await self._place(
                symbol=trade.ticker,
                side=exit_side,
                quantity=trade.quantity,
                order_type="SL",
                price=stop_limit,
                trigger_price=stop_trigger,
            )
            trade.target_order_id = await self._place(
                symbol=trade.ticker,
                side=exit_side,
                quantity=trade.quantity,
                order_type="LIMIT",
                price=target_price,
            )
        except Exception as exc:
            logger.critical("live.exit_legs_failed_flattening", order_id=trade.entry_order_id, error=repr(exc))
            await self._cancel(trade.stop_order_id)
            await self._market_exit(trade, "EXIT_LEG_PLACEMENT_FAILED")
            raise
        logger.info(
            "live.exit_legs_placed",
            order_id=trade.entry_order_id,
            stop_trigger=stop_trigger,
            target=target_price,
        )

    async def _market_exit(self, trade: _LiveTrade, reason: str) -> None:
        trade.exit_reason = reason
        trade.flatten_order_id = await self._place(
            symbol=trade.ticker,
            side=_exit_side(trade.side),
            quantity=trade.quantity,
            order_type="MARKET",
        )
        logger.warning("live.flatten_submitted", order_id=trade.entry_order_id, reason=reason)

    def _finish(self, trade: _LiveTrade, exit_price: float, reason: str) -> TradeUpdate:
        entry = trade.entry_price or trade.limit_price
        pnl = round(_sign(trade.side) * (exit_price - entry) * trade.quantity, 2)
        self._realised += pnl
        self._trades.pop(trade.entry_order_id, None)
        logger.info("live.position_closed", order_id=trade.entry_order_id, exit=exit_price, pnl=pnl, reason=reason)
        return TradeUpdate(
            order_id=trade.entry_order_id,
            ticker=trade.ticker,
            status=TradeStatus.CLOSED,
            entry_price=entry,
            exit_price=exit_price,
            pnl=pnl,
            exit_reason=reason,
        )
