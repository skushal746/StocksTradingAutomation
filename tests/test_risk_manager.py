from datetime import datetime

from config import MARKET_TZ, RiskLimits
from execution.risk_manager import AccountState, RiskManager
from schemas import Side, TradeSignal

LIMITS = RiskLimits(
    capital_inr=500_000,
    max_daily_loss_pct=0.02,
    stop_loss_pct=0.005,
    target_pct=0.01,
    risk_per_trade_pct=0.005,
    max_open_positions=3,
    sebi_peak_margin_pct=0.20,
    max_margin_utilisation=0.80,
)


def _signal(entry: float = 100.0, stop: float = 99.5) -> TradeSignal:
    return TradeSignal(
        ticker="TEST",
        instrument_token=1,
        side=Side.BUY,
        entry_price=entry,
        stop_loss=stop,
        take_profit=101.0,
        range_high=99.8,
        range_low=98.0,
        vwap=99.5,
        volume_ratio=2.0,
        reason="test",
        generated_at=datetime(2026, 11, 11, 9, 30, tzinfo=MARKET_TZ),
    )


def _state(**overrides) -> AccountState:
    base = {"available_margin": 200_000.0, "realised_pnl": 0.0, "unrealised_pnl": 0.0, "open_positions": 0}
    base.update(overrides)
    return AccountState(**base)


def test_sizing_is_limited_by_risk_budget_when_margin_is_ample():
    rm = RiskManager(LIMITS)
    size = rm.size_position(entry_price=100.0, stop_loss=99.5, available_margin=10_000_000)
    # risk budget = 500000 * 0.5% = 2500 INR; risk/share = 0.5 -> 5000 shares
    assert size.quantity == 5000
    assert size.required_margin == round(5000 * 100 * 0.20, 2)


def test_sizing_is_limited_by_margin_when_margin_is_tight():
    rm = RiskManager(LIMITS)
    size = rm.size_position(entry_price=100.0, stop_loss=99.5, available_margin=100_000)
    # max notional = 100000 * 0.80 / 0.20 = 400000 -> 4000 shares (< 5000 by risk)
    assert size.quantity == 4000
    assert size.required_margin <= 100_000 * 0.80


def test_approves_valid_signal_with_sized_position():
    decision = RiskManager(LIMITS).evaluate(_signal(), _state())
    assert decision.approved
    assert decision.size is not None and decision.size.quantity > 0


def test_rejects_when_daily_loss_cap_is_breached_using_realised_and_unrealised():
    rm = RiskManager(LIMITS)  # cap = 10,000 INR
    decision = rm.evaluate(_signal(), _state(realised_pnl=-6_000, unrealised_pnl=-4_500))
    assert not decision.approved
    assert "daily loss cap" in decision.reason


def test_rejects_when_max_open_positions_reached():
    decision = RiskManager(LIMITS).evaluate(_signal(), _state(open_positions=3))
    assert not decision.approved
    assert "max open positions" in decision.reason


def test_rejects_when_margin_cannot_buy_one_share():
    decision = RiskManager(LIMITS).evaluate(_signal(entry=5000.0, stop=4975.0), _state(available_margin=1000))
    assert not decision.approved
    assert "rounds to zero" in decision.reason


def test_kill_switch_blocks_everything():
    rm = RiskManager(LIMITS)
    rm.engage_kill_switch("manual halt")
    decision = rm.evaluate(_signal(), _state())
    assert not decision.approved
    assert "kill switch" in decision.reason
