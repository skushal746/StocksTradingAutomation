"""Pydantic data contracts shared by ingestion, analysis, execution and persistence."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TICKER_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")


class Bias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class CatalystType(str, Enum):
    EARNINGS = "EARNINGS"
    CONTRACT_WIN = "CONTRACT_WIN"
    REGULATORY_APPROVAL = "REGULATORY_APPROVAL"
    M_AND_A = "M_AND_A"
    GUIDANCE = "GUIDANCE"
    BUYBACK_DIVIDEND = "BUYBACK_DIVIDEND"
    FUNDRAISE = "FUNDRAISE"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    OTHER = "OTHER"
    NONE = "NONE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class NewsItem(BaseModel):
    """A normalised headline produced by the ingestion layer."""

    model_config = ConfigDict(frozen=True)

    source: str
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(default="", max_length=1000)
    published_at: datetime | None = None
    fetched_at: datetime
    summary: str = ""
    dedupe_key: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class LLMAnalysis(BaseModel):
    """Validated structured output returned by the LLM for one headline."""

    model_config = ConfigDict(extra="ignore")

    ticker: str | None = None
    is_material_catalyst: bool
    catalyst_type: CatalystType = CatalystType.NONE
    sentiment_confidence: float = Field(ge=0.0, le=1.0)
    directional_bias: Bias = Bias.NEUTRAL
    rationale: str = Field(default="", max_length=500)

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalise_ticker(cls, value: Any) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip().upper().removeprefix("NSE:")
        if cleaned in {"", "NULL", "NONE", "N/A"}:
            return None
        return cleaned if _TICKER_RE.match(cleaned) else None


class WatchlistEntry(BaseModel):
    rank: int = Field(ge=1)
    ticker: str
    bias: Bias
    catalyst_type: CatalystType
    target_confidence: float = Field(ge=0.0, le=1.0)
    supporting_headlines: int = Field(ge=1)
    headlines: list[str] = Field(default_factory=list)


class Tick(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_token: int
    symbol: str
    last_price: float
    cumulative_volume: int = 0
    received_at: datetime


class TradeSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    instrument_token: int
    side: Side
    entry_price: float
    stop_loss: float
    take_profit: float
    range_high: float
    range_low: float
    vwap: float
    volume_ratio: float
    reason: str
    generated_at: datetime


class PositionSize(BaseModel):
    quantity: int = Field(ge=0)
    notional: float = Field(ge=0)
    required_margin: float = Field(ge=0)


class RiskDecision(BaseModel):
    approved: bool
    reason: str
    size: PositionSize | None = None


class OrderResult(BaseModel):
    order_id: str
    ticker: str
    side: Side
    quantity: int
    status: TradeStatus
    mode: str
    entry_price: float | None = None
    stop_loss: float
    take_profit: float
    created_at: datetime


class TradeUpdate(BaseModel):
    order_id: str
    ticker: str
    status: TradeStatus
    entry_price: float | None = None
    exit_price: float | None = None
    pnl: float | None = None
    exit_reason: str | None = None
