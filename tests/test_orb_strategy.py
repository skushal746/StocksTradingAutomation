from datetime import datetime, time

from config import MARKET_TZ, StrategyParams
from execution.orb_strategy import ORBStrategy
from schemas import Bias, Side, Tick

TOKEN = 738561
PARAMS = StrategyParams(
    range_minutes=5,
    volume_multiplier=1.5,
    stop_loss_pct=0.005,
    target_pct=0.01,
    market_open=time(9, 15),
    entry_cutoff=time(14, 30),
    allow_shorts=False,
)


def _tick(hh: int, mm: int, ss: int, price: float, cumulative: int, token: int = TOKEN) -> Tick:
    return Tick(
        instrument_token=token,
        symbol="RELIANCE",
        last_price=price,
        cumulative_volume=cumulative,
        received_at=datetime(2026, 11, 11, hh, mm, ss, tzinfo=MARKET_TZ),
    )


def _feed_opening_range(strategy: ORBStrategy) -> None:
    """09:15-09:19 with a 1,000-share increment per minute: range volume 4,000 -> 800/min average."""
    prices = [100.0, 100.5, 101.0, 101.5, 102.0]
    for i, price in enumerate(prices):
        assert strategy.on_tick(_tick(9, 15 + i, 0, price, cumulative=1000 * i)) is None


def test_fires_long_breakout_on_price_above_range_vwap_and_volume():
    strategy = ORBStrategy(PARAMS, {TOKEN: ("RELIANCE", Bias.BULLISH)})
    _feed_opening_range(strategy)

    # Inside the range window is over (09:20). Price stays under range high: no signal.
    assert strategy.on_tick(_tick(9, 20, 30, 101.0, cumulative=4000)) is None

    # Breakout with a 2,000-share burst in the last minute: ratio = 2000 / 800 = 2.5 >= 1.5
    signal = strategy.on_tick(_tick(9, 21, 0, 103.0, cumulative=6000))
    assert signal is not None
    assert signal.side is Side.BUY
    assert signal.entry_price == 103.0
    assert signal.stop_loss == round(103.0 * (1 - 0.005), 2)
    assert signal.take_profit == round(103.0 * (1 + 0.01), 2)
    assert signal.range_high == 102.0 and signal.range_low == 100.0
    assert signal.volume_ratio >= 1.5


def test_one_signal_per_symbol_per_day():
    strategy = ORBStrategy(PARAMS, {TOKEN: ("RELIANCE", Bias.BULLISH)})
    _feed_opening_range(strategy)
    assert strategy.on_tick(_tick(9, 21, 0, 103.0, cumulative=6000)) is not None
    assert strategy.on_tick(_tick(9, 21, 10, 104.0, cumulative=9000)) is None


def test_no_signal_without_volume_confirmation():
    strategy = ORBStrategy(PARAMS, {TOKEN: ("RELIANCE", Bias.BULLISH)})
    _feed_opening_range(strategy)
    # Price breaks out but cumulative volume is unchanged: ratio = 0.
    assert strategy.on_tick(_tick(9, 21, 0, 103.0, cumulative=4000)) is None


def test_signal_reports_vwap_below_entry_price_and_volume_ratio():
    strategy = ORBStrategy(PARAMS, {TOKEN: ("RELIANCE", Bias.BULLISH)})
    _feed_opening_range(strategy)
    signal = strategy.on_tick(_tick(9, 21, 0, 103.0, cumulative=6000))
    assert signal is not None
    assert signal.vwap < signal.entry_price
    assert signal.volume_ratio == 2.5


def test_ignores_ticks_before_open_and_after_entry_cutoff():
    strategy = ORBStrategy(PARAMS, {TOKEN: ("RELIANCE", Bias.BULLISH)})
    assert strategy.on_tick(_tick(9, 10, 0, 100.0, cumulative=0)) is None
    _feed_opening_range(strategy)
    assert strategy.on_tick(_tick(14, 31, 0, 105.0, cumulative=9_000)) is None


def test_bearish_candidates_are_untracked_when_shorts_disabled():
    strategy = ORBStrategy(PARAMS, {TOKEN: ("RELIANCE", Bias.BEARISH)})
    assert strategy.tracked_symbols == []
    assert strategy.on_tick(_tick(9, 21, 0, 90.0, cumulative=9_000)) is None


def test_unknown_instrument_is_ignored():
    strategy = ORBStrategy(PARAMS, {TOKEN: ("RELIANCE", Bias.BULLISH)})
    assert strategy.on_tick(_tick(9, 16, 0, 100.0, cumulative=100, token=999)) is None
