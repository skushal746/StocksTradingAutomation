"""Data-access functions. Callers own the transaction boundary."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import NewsEvent, TradeLog, WatchlistHistory
from schemas import LLMAnalysis, NewsItem, OrderResult, TradeStatus, TradeUpdate, WatchlistEntry


# ---------------------------------------------------------------- news_events
async def insert_news_items(session: AsyncSession, items: Sequence[NewsItem]) -> int:
    """Insert headlines, silently skipping those whose dedupe_key already exists.

    Returns the number of rows actually inserted.
    """
    if not items:
        return 0
    rows = [
        {
            "dedupe_key": item.dedupe_key,
            "timestamp": item.published_at or item.fetched_at,
            "ingested_at": item.fetched_at,
            "source": item.source[:100],
            "title": item.title[:500],
            "url": item.url[:1000],
            "summary": item.summary,
            "raw_payload": item.raw_payload,
            "is_material": False,
        }
        for item in items
    ]
    stmt = (
        pg_insert(NewsEvent)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(NewsEvent.id)
    )
    result = await session.execute(stmt)
    return len(result.all())


async def pending_analysis(session: AsyncSession, since: datetime, limit: int) -> list[NewsEvent]:
    stmt = (
        select(NewsEvent)
        .where(NewsEvent.sentiment_score.is_(None), NewsEvent.timestamp >= since)
        .order_by(NewsEvent.timestamp.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def save_analysis(session: AsyncSession, event_id: int, analysis: LLMAnalysis) -> None:
    await session.execute(
        update(NewsEvent)
        .where(NewsEvent.id == event_id)
        .values(
            ticker=analysis.ticker,
            sentiment_score=analysis.sentiment_confidence,
            directional_bias=analysis.directional_bias.value,
            catalyst_type=analysis.catalyst_type.value,
            is_material=analysis.is_material_catalyst,
            rationale=analysis.rationale,
        )
    )


async def material_events_since(session: AsyncSession, since: datetime) -> list[NewsEvent]:
    stmt = select(NewsEvent).where(
        NewsEvent.is_material.is_(True),
        NewsEvent.ticker.is_not(None),
        NewsEvent.sentiment_score.is_not(None),
        NewsEvent.timestamp >= since,
    )
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------- watchlist_history
async def replace_watchlist(session: AsyncSession, trade_date: date, entries: Sequence[WatchlistEntry]) -> None:
    """Idempotently replace the watchlist for a trading day."""
    await session.execute(delete(WatchlistHistory).where(WatchlistHistory.trade_date == trade_date))
    session.add_all(
        [
            WatchlistHistory(
                trade_date=trade_date,
                rank=entry.rank,
                ticker=entry.ticker,
                bias=entry.bias.value,
                catalyst_type=entry.catalyst_type.value,
                target_confidence=entry.target_confidence,
                supporting_headlines=entry.supporting_headlines,
            )
            for entry in entries
        ]
    )


async def load_watchlist(session: AsyncSession, trade_date: date) -> list[WatchlistHistory]:
    stmt = select(WatchlistHistory).where(WatchlistHistory.trade_date == trade_date).order_by(WatchlistHistory.rank)
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------- trade_logs
async def create_trade(session: AsyncSession, *, trade_date: date, order: OrderResult, signal_reason: str) -> None:
    session.add(
        TradeLog(
            order_id=order.order_id,
            trade_date=trade_date,
            mode=order.mode,
            ticker=order.ticker,
            side=order.side.value,
            quantity=order.quantity,
            entry_price=order.entry_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            status=order.status.value,
            signal_reason=signal_reason[:500],
            opened_at=order.created_at,
        )
    )


async def apply_trade_update(session: AsyncSession, update_: TradeUpdate, *, closed_at: datetime) -> None:
    values: dict[str, object] = {"status": update_.status.value}
    if update_.entry_price is not None:
        values["entry_price"] = update_.entry_price
    if update_.exit_price is not None:
        values["exit_price"] = update_.exit_price
    if update_.pnl is not None:
        values["pnl"] = update_.pnl
    if update_.exit_reason is not None:
        values["exit_reason"] = update_.exit_reason
    if update_.status in (TradeStatus.CLOSED, TradeStatus.CANCELLED, TradeStatus.REJECTED):
        values["closed_at"] = closed_at
    await session.execute(update(TradeLog).where(TradeLog.order_id == update_.order_id).values(**values))


async def realised_pnl_for_day(session: AsyncSession, trade_date: date, mode: str) -> float:
    stmt = select(func.coalesce(func.sum(TradeLog.pnl), 0.0)).where(
        TradeLog.trade_date == trade_date,
        TradeLog.mode == mode,
        TradeLog.status == TradeStatus.CLOSED.value,
    )
    return float((await session.execute(stmt)).scalar_one())
