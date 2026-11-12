"""Typed configuration loaded from environment variables and a local .env file.

Safety defaults: TRADING_MODE is PAPER. Switching to LIVE requires both
TRADING_MODE=live and LIVE_TRADING_ACK set to the exact acknowledgement token.
"""

from __future__ import annotations

from datetime import time
from enum import Enum
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MARKET_TZ = ZoneInfo("Asia/Kolkata")
LIVE_TRADING_ACK = "I-UNDERSTAND-LIVE-TRADING-RISK"

DEFAULT_NEWS_FEEDS = (
    "https://www.moneycontrol.com/rss/MCtopnews.xml",
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.livemint.com/rss/markets",
)
DEFAULT_LLM_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-haiku-5-5"}


class ConfigError(RuntimeError):
    """Configuration is missing or inconsistent."""


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class LLMProvider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class RiskLimits(BaseModel):
    """Immutable snapshot of risk parameters handed to the RiskManager."""

    capital_inr: float
    max_daily_loss_pct: float
    stop_loss_pct: float
    target_pct: float
    risk_per_trade_pct: float
    max_open_positions: int
    sebi_peak_margin_pct: float
    max_margin_utilisation: float


class StrategyParams(BaseModel):
    """Immutable snapshot of ORB strategy parameters."""

    range_minutes: int
    volume_multiplier: float
    stop_loss_pct: float
    target_pct: float
    market_open: time
    entry_cutoff: time
    allow_shorts: bool


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General
    log_level: str = "INFO"
    log_dir: str = "logs"

    # --- Trading mode (PAPER by default)
    trading_mode: TradingMode = TradingMode.PAPER
    live_trading_ack: str = ""

    # --- Persistence
    database_url: str = "postgresql+asyncpg://trader:trader@localhost:5432/trading"

    # --- Zerodha Kite Connect
    kite_api_key: str = ""
    kite_api_secret: SecretStr = SecretStr("")
    kite_access_token: SecretStr = SecretStr("")
    kite_exchange: str = "NSE"
    ws_reconnect_max_tries: int = Field(default=50, ge=1)
    ws_reconnect_max_delay: int = Field(default=60, ge=1)

    # --- LLM
    llm_provider: LLMProvider = LLMProvider.OPENAI
    llm_model: str = ""
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    llm_concurrency: int = Field(default=5, ge=1, le=20)
    llm_timeout_seconds: float = Field(default=30.0, gt=0)

    # --- News ingestion
    news_feed_urls: str = ",".join(DEFAULT_NEWS_FEEDS)
    news_request_timeout_seconds: float = Field(default=10.0, gt=0)
    news_lookback_hours: int = Field(default=18, ge=1, le=96)

    # --- NewsData.io (optional; free plan: 200 credits/day, 1-10 articles per credit, 12h delay, 48h history)
    newsdata_api_key: SecretStr = SecretStr("")
    newsdata_query: str = ""
    newsdata_country: str = "in"
    newsdata_language: str = "en"
    newsdata_size: int = Field(default=10, ge=1, le=10)

    # --- Watchlist
    watchlist_size: int = Field(default=5, ge=1, le=20)
    premarket_cutoff: time = time(8, 45)
    watchlist_allow_shorts: bool = False

    # --- Risk limits
    capital_inr: float = Field(default=500_000.0, gt=0)
    max_daily_loss_pct: float = Field(default=0.02, gt=0, le=0.20)
    stop_loss_pct: float = Field(default=0.005, gt=0, le=0.10)
    target_pct: float = Field(default=0.01, gt=0, le=0.50)
    risk_per_trade_pct: float = Field(default=0.005, gt=0, le=0.05)
    max_open_positions: int = Field(default=3, ge=1, le=20)
    sebi_peak_margin_pct: float = Field(default=0.20, gt=0, le=1.0)
    max_margin_utilisation: float = Field(default=0.80, gt=0, le=1.0)

    # --- ORB strategy
    orb_range_minutes: int = 15
    orb_volume_multiplier: float = Field(default=1.5, gt=0)
    market_open: time = time(9, 15)
    entry_cutoff: time = time(14, 30)
    square_off_time: time = time(15, 10)
    paper_slippage_pct: float = Field(default=0.0005, ge=0, le=0.01)

    # ------------------------------------------------------------------ validators
    @field_validator("orb_range_minutes")
    @classmethod
    def _check_range_minutes(cls, value: int) -> int:
        if value not in (5, 15):
            raise ValueError("ORB_RANGE_MINUTES must be 5 or 15")
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _cross_field_checks(self) -> Settings:
        if not (self.market_open < self.entry_cutoff < self.square_off_time):
            raise ValueError("Require MARKET_OPEN < ENTRY_CUTOFF < SQUARE_OFF_TIME")
        if self.risk_per_trade_pct > self.max_daily_loss_pct:
            raise ValueError("RISK_PER_TRADE_PCT cannot exceed MAX_DAILY_LOSS_PCT")
        if self.target_pct <= self.stop_loss_pct:
            raise ValueError("TARGET_PCT must be greater than STOP_LOSS_PCT")
        if not self.llm_model:
            self.llm_model = DEFAULT_LLM_MODELS[self.llm_provider.value]
        return self

    # ------------------------------------------------------------------ derived views
    @property
    def feed_urls(self) -> list[str]:
        return [url.strip() for url in self.news_feed_urls.split(",") if url.strip()]

    @property
    def llm_api_key(self) -> str:
        key = self.openai_api_key if self.llm_provider is LLMProvider.OPENAI else self.anthropic_api_key
        return key.get_secret_value()

    @property
    def risk_limits(self) -> RiskLimits:
        return RiskLimits(
            capital_inr=self.capital_inr,
            max_daily_loss_pct=self.max_daily_loss_pct,
            stop_loss_pct=self.stop_loss_pct,
            target_pct=self.target_pct,
            risk_per_trade_pct=self.risk_per_trade_pct,
            max_open_positions=self.max_open_positions,
            sebi_peak_margin_pct=self.sebi_peak_margin_pct,
            max_margin_utilisation=self.max_margin_utilisation,
        )

    @property
    def strategy_params(self) -> StrategyParams:
        return StrategyParams(
            range_minutes=self.orb_range_minutes,
            volume_multiplier=self.orb_volume_multiplier,
            stop_loss_pct=self.stop_loss_pct,
            target_pct=self.target_pct,
            market_open=self.market_open,
            entry_cutoff=self.entry_cutoff,
            allow_shorts=self.watchlist_allow_shorts,
        )

    def assert_live_allowed(self) -> None:
        if self.trading_mode is TradingMode.LIVE and self.live_trading_ack != LIVE_TRADING_ACK:
            raise ConfigError(
                f"TRADING_MODE=live requires LIVE_TRADING_ACK={LIVE_TRADING_ACK}. "
                "Stay in paper mode unless you have validated the strategy."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
