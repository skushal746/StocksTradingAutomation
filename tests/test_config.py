import pytest

from config import ConfigError, Settings, TradingMode


def test_defaults_to_paper_trading():
    settings = Settings(_env_file=None)
    assert settings.trading_mode is TradingMode.PAPER
    settings.assert_live_allowed()  # no-op in paper mode


def test_live_mode_requires_explicit_acknowledgement():
    settings = Settings(_env_file=None, trading_mode="live")
    with pytest.raises(ConfigError):
        settings.assert_live_allowed()
    acknowledged = Settings(_env_file=None, trading_mode="live", live_trading_ack="I-UNDERSTAND-LIVE-TRADING-RISK")
    acknowledged.assert_live_allowed()


def test_orb_range_must_be_5_or_15():
    with pytest.raises(ValueError):
        Settings(_env_file=None, orb_range_minutes=10)


def test_target_must_exceed_stop():
    with pytest.raises(ValueError):
        Settings(_env_file=None, stop_loss_pct=0.01, target_pct=0.005)


def test_llm_model_defaults_per_provider():
    assert Settings(_env_file=None, llm_provider="anthropic").llm_model == "claude-haiku-5-5"
    assert Settings(_env_file=None).llm_model == "gpt-4o-mini"
