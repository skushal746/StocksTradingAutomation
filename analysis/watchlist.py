"""Watchlist construction: aggregate scored headlines into a ranked top-N list.

Scoring per ticker (pure function, see ``rank_watchlist``):
  * consider only material headlines with a directional bias and positive confidence
  * the dominant side is the bias with the larger summed confidence
  * best = highest confidence on the dominant side
  * corroboration adds up to +0.10 for multiple independent headlines
  * conflicting headlines on the opposing side reduce the score by up to 50%
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from db import repository as repo
from schemas import Bias, CatalystType, WatchlistEntry


@dataclass(frozen=True)
class ScoredHeadline:
    ticker: str
    bias: Bias
    catalyst: CatalystType
    confidence: float
    title: str


def rank_watchlist(headlines: Iterable[ScoredHeadline], *, size: int, allow_shorts: bool) -> list[WatchlistEntry]:
    grouped: dict[str, list[ScoredHeadline]] = defaultdict(list)
    for headline in headlines:
        if headline.bias is Bias.NEUTRAL or headline.catalyst is CatalystType.NONE or headline.confidence <= 0:
            continue
        grouped[headline.ticker].append(headline)

    candidates: list[tuple[float, int, str, Bias, ScoredHeadline, list[ScoredHeadline]]] = []
    for ticker, items in grouped.items():
        bullish = sum(h.confidence for h in items if h.bias is Bias.BULLISH)
        bearish = sum(h.confidence for h in items if h.bias is Bias.BEARISH)
        total = bullish + bearish
        dominant = Bias.BULLISH if bullish >= bearish else Bias.BEARISH
        if dominant is Bias.BEARISH and not allow_shorts:
            continue
        support = [h for h in items if h.bias is dominant]
        support_conf = sum(h.confidence for h in support)
        opposing_share = (total - support_conf) / total
        best = max(support, key=lambda h: h.confidence)
        corroboration = min(0.10, 0.05 * (len(support) - 1))
        score = min(1.0, best.confidence + corroboration) * (1 - 0.5 * opposing_share)
        candidates.append((round(score, 4), len(support), ticker, dominant, best, support))

    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
    entries: list[WatchlistEntry] = []
    for rank, (score, count, ticker, dominant, best, support) in enumerate(candidates[:size], start=1):
        entries.append(
            WatchlistEntry(
                rank=rank,
                ticker=ticker,
                bias=dominant,
                catalyst_type=best.catalyst,
                target_confidence=score,
                supporting_headlines=count,
                headlines=[h.title for h in support][:5],
            )
        )
    return entries


async def build_watchlist(
    session: AsyncSession,
    *,
    trade_date: date,
    since: datetime,
    size: int,
    allow_shorts: bool,
) -> list[WatchlistEntry]:
    """Read material news since ``since``, rank it, and persist the result for ``trade_date``."""
    events = await repo.material_events_since(session, since)
    headlines = [
        ScoredHeadline(
            ticker=event.ticker,
            bias=Bias(event.directional_bias),
            catalyst=CatalystType(event.catalyst_type),
            confidence=float(event.sentiment_score or 0.0),
            title=event.title,
        )
        for event in events
        if event.ticker and event.directional_bias and event.catalyst_type
    ]
    entries = rank_watchlist(headlines, size=size, allow_shorts=allow_shorts)
    await repo.replace_watchlist(session, trade_date, entries)
    return entries
