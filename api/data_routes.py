"""Read-only endpoints for the news, the watchlist and the trades. Each one requires a login."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from auth.routes import CurrentUser, SessionDep
from config import MARKET_TZ
from db import repository as repo

router = APIRouter(tags=["data"])


class NewsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timestamp: datetime
    source: str
    title: str
    url: str
    summary: str
    ticker: str | None
    sentiment_score: float | None
    directional_bias: str | None
    catalyst_type: str | None
    is_material: bool
    rationale: str | None


class WatchlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trade_date: date
    rank: int
    ticker: str
    bias: str
    catalyst_type: str
    target_confidence: float
    supporting_headlines: int
    generated_at: datetime | None


class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: str
    trade_date: date
    mode: str
    ticker: str
    side: str
    quantity: int
    entry_price: float | None
    exit_price: float | None
    stop_loss: float
    take_profit: float
    pnl: float | None
    status: str


def _today() -> date:
    return datetime.now(MARKET_TZ).date()


@router.get("/news", response_model=list[NewsOut])
async def news(
    _: CurrentUser,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=500),
    material_only: bool = False,
) -> list:
    return await repo.list_news(session, limit=limit, material_only=material_only)


@router.get("/watchlist", response_model=list[WatchlistOut])
async def watchlist(_: CurrentUser, session: SessionDep, trade_date: date | None = None) -> list:
    return await repo.load_watchlist(session, trade_date or _today())


@router.get("/trades", response_model=list[TradeOut])
async def trades(_: CurrentUser, session: SessionDep, trade_date: date | None = None) -> list:
    return await repo.list_trades(session, trade_date or _today())
