"""Opening Range Breakout (ORB) with VWAP and volume confirmation.

Pure logic: consumes ``Tick`` objects and emits at most one ``TradeSignal`` per symbol per day.

Timeline per symbol (IST):
  market_open .. range_end   build opening range high/low and range volume
  range_end  .. entry_cutoff evaluate breakouts:
      BUY  if price > range_high AND price > VWAP AND volume_ratio >= multiplier
      SELL if price < range_low  AND price < VWAP AND volume_ratio >= multiplier (shorts only)

volume_ratio = volume traded in the last 60 seconds / average per-minute volume during the range.
VWAP is accumulated from the day's first tick, weighted by traded-volume deltas.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from config import MARKET_TZ, StrategyParams
from schemas import Bias, Side, Tick, TradeSignal

VOLUME_WINDOW = timedelta(seconds=60)


@dataclass
class _SymbolState:
    symbol: str
    bias: Bias
    range_high: float | None = None
    range_low: float | None = None
    range_volume: int = 0
    last_cumulative_volume: int | None = None
    price_volume_sum: float = 0.0
    volume_sum: int = 0
    recent_volume: deque[tuple[datetime, int]] = field(default_factory=deque)
    fired: bool = False


def _add_minutes(start: time, minutes: int) -> time:
    return (datetime.combine(date(2000, 1, 1), start) + timedelta(minutes=minutes)).time()


class ORBStrategy:
    def __init__(self, params: StrategyParams, candidates: Mapping[int, tuple[str, Bias]]) -> None:
        """``candidates`` maps instrument_token -> (symbol, bias) from the morning watchlist."""
        self._params = params
        self._range_end = _add_minutes(params.market_open, params.range_minutes)
        self._states: dict[int, _SymbolState] = {}
        for token, (symbol, bias) in candidates.items():
            if bias is Bias.BULLISH or (bias is Bias.BEARISH and params.allow_shorts):
                self._states[token] = _SymbolState(symbol=symbol, bias=bias)

    @property
    def tracked_symbols(self) -> list[str]:
        return sorted(state.symbol for state in self._states.values())

    def on_tick(self, tick: Tick) -> TradeSignal | None:
        state = self._states.get(tick.instrument_token)
        if state is None or state.fired:
            return None

        now = tick.received_at.astimezone(MARKET_TZ)
        clock = now.time()
        if clock < self._params.market_open:
            return None

        price = tick.last_price
        delta = self._volume_delta(state, tick.cumulative_volume)
        if delta > 0:
            state.price_volume_sum += price * delta
            state.volume_sum += delta

        if clock < self._range_end:
            state.range_high = price if state.range_high is None else max(state.range_high, price)
            state.range_low = price if state.range_low is None else min(state.range_low, price)
            state.range_volume += delta
            return None

        if delta > 0:
            state.recent_volume.append((now, delta))
        self._prune_recent(state, now)

        if clock >= self._params.entry_cutoff or state.range_high is None or state.range_low is None:
            return None
        return self._evaluate_breakout(state, tick, now)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _volume_delta(state: _SymbolState, cumulative: int) -> int:
        """Convert cumulative day volume into per-tick traded volume."""
        last = state.last_cumulative_volume
        state.last_cumulative_volume = cumulative
        if last is None or cumulative < last:  # first tick, or a counter reset
            return 0
        return cumulative - last

    @staticmethod
    def _prune_recent(state: _SymbolState, now: datetime) -> None:
        while state.recent_volume and now - state.recent_volume[0][0] > VOLUME_WINDOW:
            state.recent_volume.popleft()

    def _evaluate_breakout(self, state: _SymbolState, tick: Tick, now: datetime) -> TradeSignal | None:
        if state.volume_sum == 0 or state.range_volume <= 0 or state.range_high is None or state.range_low is None:
            return None
        vwap = state.price_volume_sum / state.volume_sum
        range_avg_per_minute = state.range_volume / self._params.range_minutes
        rolling_volume = sum(volume for _, volume in state.recent_volume)
        volume_ratio = rolling_volume / range_avg_per_minute
        if volume_ratio < self._params.volume_multiplier:
            return None

        price = tick.last_price
        stop_pct = self._params.stop_loss_pct
        target_pct = self._params.target_pct
        if state.bias is Bias.BULLISH and price > state.range_high and price > vwap:
            side = Side.BUY
            stop, target = price * (1 - stop_pct), price * (1 + target_pct)
            reason = f"ORB long: close {price:.2f} > range high {state.range_high:.2f}, > VWAP {vwap:.2f}"
        elif state.bias is Bias.BEARISH and price < state.range_low and price < vwap:
            side = Side.SELL
            stop, target = price * (1 + stop_pct), price * (1 - target_pct)
            reason = f"ORB short: price {price:.2f} < range low {state.range_low:.2f}, < VWAP {vwap:.2f}"
        else:
            return None

        state.fired = True
        return TradeSignal(
            ticker=state.symbol,
            instrument_token=tick.instrument_token,
            side=side,
            entry_price=round(price, 2),
            stop_loss=round(stop, 2),
            take_profit=round(target, 2),
            range_high=state.range_high,
            range_low=state.range_low,
            vwap=round(vwap, 2),
            volume_ratio=round(volume_ratio, 2),
            reason=reason,
            generated_at=now,
        )
