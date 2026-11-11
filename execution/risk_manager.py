"""Pre-trade risk checks and position sizing.

Checks, in order: kill switch, daily loss cap, max open positions, then sizing.
Sizing takes the smaller of
  * risk budget:  capital * risk_per_trade_pct / per-share stop distance
  * margin cap:   available_margin * max_margin_utilisation / sebi_peak_margin_pct

SEBI peak-margin rule: intraday exposure must be backed by at least
``sebi_peak_margin_pct`` of notional as margin. The default of 20% caps intraday
leverage near 5x. Confirm the current figure with your broker and adjust the setting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from config import RiskLimits
from schemas import PositionSize, RiskDecision, TradeSignal


@dataclass(frozen=True)
class AccountState:
    available_margin: float
    realised_pnl: float
    unrealised_pnl: float
    open_positions: int


class RiskManager:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits
        self._kill_reason: str | None = None

    @property
    def daily_loss_limit(self) -> float:
        return self._limits.capital_inr * self._limits.max_daily_loss_pct

    def engage_kill_switch(self, reason: str) -> None:
        """Block all new entries for the rest of the session."""
        self._kill_reason = reason

    def size_position(self, entry_price: float, stop_loss: float, available_margin: float) -> PositionSize:
        limits = self._limits
        if entry_price <= 0 or stop_loss <= 0 or entry_price == stop_loss or available_margin <= 0:
            return PositionSize(quantity=0, notional=0.0, required_margin=0.0)

        risk_per_share = abs(entry_price - stop_loss)
        risk_budget = limits.capital_inr * limits.risk_per_trade_pct
        qty_by_risk = math.floor(risk_budget / risk_per_share)

        max_notional = available_margin * limits.max_margin_utilisation / limits.sebi_peak_margin_pct
        qty_by_margin = math.floor(max_notional / entry_price)

        quantity = max(0, min(qty_by_risk, qty_by_margin))
        notional = quantity * entry_price
        return PositionSize(
            quantity=quantity,
            notional=round(notional, 2),
            required_margin=round(notional * limits.sebi_peak_margin_pct, 2),
        )

    def evaluate(self, signal: TradeSignal, state: AccountState) -> RiskDecision:
        limits = self._limits
        if self._kill_reason is not None:
            return RiskDecision(approved=False, reason=f"kill switch engaged: {self._kill_reason}")

        daily_pnl = state.realised_pnl + state.unrealised_pnl
        if daily_pnl <= -self.daily_loss_limit:
            return RiskDecision(
                approved=False,
                reason=f"daily loss cap breached: pnl={daily_pnl:.2f} limit=-{self.daily_loss_limit:.2f}",
            )

        if state.open_positions >= limits.max_open_positions:
            return RiskDecision(
                approved=False,
                reason=f"max open positions reached ({state.open_positions}/{limits.max_open_positions})",
            )

        size = self.size_position(signal.entry_price, signal.stop_loss, state.available_margin)
        if size.quantity < 1:
            return RiskDecision(
                approved=False,
                reason="position size rounds to zero (insufficient margin or risk budget)",
            )

        usable_margin = state.available_margin * limits.max_margin_utilisation
        if size.required_margin > usable_margin:  # defensive; sizing already enforces this
            return RiskDecision(approved=False, reason="required margin exceeds usable margin")

        return RiskDecision(approved=True, reason="within risk limits", size=size)
