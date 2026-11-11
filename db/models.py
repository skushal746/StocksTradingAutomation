"""SQLAlchemy 2.0 ORM models for news, watchlists and trades."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class NewsEvent(Base):
    """An ingested headline plus its LLM analysis.

    ``sentiment_score`` holds the LLM confidence (0.0-1.0). NULL means "not yet analysed",
    which is how the backfill job finds work to do.
    """

    __tablename__ = "news_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(1000), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    ticker: Mapped[str | None] = mapped_column(String(20), index=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    directional_bias: Mapped[str | None] = mapped_column(String(10))
    catalyst_type: Mapped[str | None] = mapped_column(String(40))
    is_material: Mapped[bool] = mapped_column(Boolean, default=False)
    rationale: Mapped[str | None] = mapped_column(String(500))
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)


class WatchlistHistory(Base):
    __tablename__ = "watchlist_history"
    __table_args__ = (UniqueConstraint("trade_date", "ticker", name="uq_watchlist_date_ticker"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    rank: Mapped[int] = mapped_column(Integer)
    ticker: Mapped[str] = mapped_column(String(20))
    bias: Mapped[str] = mapped_column(String(10))
    catalyst_type: Mapped[str] = mapped_column(String(40))
    target_confidence: Mapped[float] = mapped_column(Float)
    supporting_headlines: Mapped[int] = mapped_column(Integer)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TradeLog(Base):
    """One row per entry order. Exit fields are filled in as the trade closes."""

    __tablename__ = "trade_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    mode: Mapped[str] = mapped_column(String(10))
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float | None] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(12), index=True)
    exit_reason: Mapped[str | None] = mapped_column(String(40))
    signal_reason: Mapped[str] = mapped_column(String(500), default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
