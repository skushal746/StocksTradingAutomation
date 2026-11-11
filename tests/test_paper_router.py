import asyncio
from datetime import datetime

from config import MARKET_TZ
from execution.order_router import PaperOrderRouter
from schemas import Side, Tick, TradeSignal, TradeStatus

TOKEN = 1


def _signal() -> TradeSignal:
    return TradeSignal(
        ticker="TEST",
        instrument_token=TOKEN,
        side=Side.BUY,
        entry_price=100.0,
        stop_loss=99.5,
        take_profit=101.0,
        range_high=99.9,
        range_low=98.0,
        vwap=99.0,
        volume_ratio=2.0,
        reason="test",
        generated_at=datetime(2026, 11, 11, 9, 30, tzinfo=MARKET_TZ),
    )


def _tick(price: float) -> Tick:
    return Tick(
        instrument_token=TOKEN,
        symbol="TEST",
        last_price=price,
        cumulative_volume=0,
        received_at=datetime(2026, 11, 11, 9, 40, tzinfo=MARKET_TZ),
    )


def _router() -> PaperOrderRouter:
    return PaperOrderRouter(capital=500_000, margin_pct=0.20, slippage_pct=0.0)


def test_stop_loss_exit_books_negative_pnl():
    async def scenario():
        router = _router()
        order = await router.place_bracket(_signal(), quantity=10)
        assert order.status is TradeStatus.OPEN and order.stop_loss == 99.5
        updates = await router.on_tick(_tick(99.5))
        return router, updates

    router, updates = asyncio.run(scenario())
    assert len(updates) == 1
    assert updates[0].exit_reason == "STOP_LOSS"
    assert updates[0].pnl == -5.0
    assert router.realised_pnl() == -5.0
    assert router.open_position_count() == 0


def test_take_profit_exit_books_positive_pnl():
    async def scenario():
        router = _router()
        await router.place_bracket(_signal(), quantity=10)
        return await router.on_tick(_tick(101.2))

    updates = asyncio.run(scenario())
    assert updates[0].exit_reason == "TAKE_PROFIT"
    assert updates[0].exit_price == 101.0
    assert updates[0].pnl == 10.0


def test_gap_through_stop_fills_at_worse_price():
    async def scenario():
        router = _router()
        await router.place_bracket(_signal(), quantity=10)
        return await router.on_tick(_tick(98.0))

    updates = asyncio.run(scenario())
    assert updates[0].exit_price == 98.0
    assert updates[0].pnl == -20.0


def test_available_margin_reflects_open_exposure():
    async def scenario():
        router = _router()
        before = await router.available_margin()
        await router.place_bracket(_signal(), quantity=100)  # notional 10,000 -> margin 2,000
        after = await router.available_margin()
        return before, after

    before, after = asyncio.run(scenario())
    assert before == 500_000
    assert after == 500_000 - 2_000


def test_flatten_closes_open_positions_at_last_price():
    async def scenario():
        router = _router()
        await router.place_bracket(_signal(), quantity=10)
        await router.on_tick(_tick(100.5))
        return await router.flatten_all("SESSION_END")

    updates = asyncio.run(scenario())
    assert updates[0].exit_reason == "SESSION_END"
    assert updates[0].pnl == 5.0
