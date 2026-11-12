"""CLI entrypoint.

    python main.py init-db
    python main.py kite-login-url
    python main.py kite-session --request-token <TOKEN>
    python main.py backfill-news [--days 2] [--no-analyse]
    python main.py run-premarket [--skip-llm]
    python main.py run-live

Paper trading is the default. See README.md for setup.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import datetime, timedelta

import typer
from kiteconnect import KiteConnect
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from analysis.llm_analyzer import LLMAnalyzer, build_llm_client
from analysis.watchlist import build_watchlist
from config import MARKET_TZ, ConfigError, Settings, TradingMode, get_settings
from core.kite_client import build_kite_client, resolve_instrument_tokens
from core.logging_setup import configure_logging
from db import repository as repo
from db.models import NewsEvent
from db.session import create_db_engine, create_session_factory, init_db
from execution.engine import TradingEngine
from execution.order_router import KiteOrderRouter, OrderRouter, PaperOrderRouter
from execution.orb_strategy import ORBStrategy
from execution.risk_manager import RiskManager
from ingestion.news_scraper import NewsScraper, dedupe_items
from ingestion.newsdata_client import fetch_newsdata
from ingestion.tick_listener import TickListener
from schemas import Bias, NewsItem

app = typer.Typer(
    help="Automated intraday ORB trading platform. Paper mode by default.",
    no_args_is_help=True,
    add_completion=False,
)


# ====================================================================== helpers
def _execute(coro) -> None:  # type: ignore[no-untyped-def]
    try:
        asyncio.run(coro)
    except ConfigError as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


async def _open_db(settings: Settings) -> tuple[AsyncEngine, async_sessionmaker]:
    engine = create_db_engine(settings.database_url)
    await init_db(engine)
    return engine, create_session_factory(engine)


def _event_to_item(event: NewsEvent) -> NewsItem:
    return NewsItem(
        source=event.source,
        title=event.title,
        url=event.url or "",
        published_at=event.timestamp,
        fetched_at=event.ingested_at or datetime.now(MARKET_TZ),
        summary=event.summary or "",
        dedupe_key=event.dedupe_key,
        raw_payload=event.raw_payload or {},
    )


async def _ingest_news(settings: Settings, sf: async_sessionmaker) -> tuple[int, int]:
    async with NewsScraper(settings.feed_urls, timeout=settings.news_request_timeout_seconds) as scraper:
        items = await scraper.fetch_all()
    if settings.newsdata_api_key.get_secret_value():
        try:
            items.extend(
                await fetch_newsdata(
                    settings.newsdata_api_key.get_secret_value(),
                    query=settings.newsdata_query,
                    country=settings.newsdata_country,
                    language=settings.newsdata_language,
                    size=settings.newsdata_size,
                    timeout=settings.news_request_timeout_seconds,
                )
            )
        except Exception as exc:  # the RSS feeds must still run if NewsData is down or out of credits
            logger.error("newsdata.failed", error=repr(exc))
        items = dedupe_items(items)
    inserted = 0
    async with sf() as session, session.begin():
        for start in range(0, len(items), 500):
            inserted += await repo.insert_news_items(session, items[start : start + 500])
    logger.info("news.ingested", fetched=len(items), new=inserted)
    return len(items), inserted


async def _analyse_pending(settings: Settings, sf: async_sessionmaker, since: datetime, limit: int) -> tuple[int, int]:
    async with sf() as session:
        events = await repo.pending_analysis(session, since, limit)
    if not events:
        return 0, 0
    analyser = LLMAnalyzer(build_llm_client(settings), concurrency=settings.llm_concurrency)
    results = await analyser.analyse_batch([_event_to_item(e) for e in events])
    analysed = material = 0
    async with sf() as session, session.begin():
        for event, (_, analysis) in zip(events, results, strict=True):
            if analysis is None:
                continue  # left pending; retried on the next run
            await repo.save_analysis(session, event.id, analysis)
            analysed += 1
            material += int(analysis.is_material_catalyst)
    logger.info("news.analysed", pending=len(events), analysed=analysed, material=material)
    return analysed, material


def _build_router(settings: Settings, kite: KiteConnect) -> OrderRouter:
    common = {"capital": settings.capital_inr, "margin_pct": settings.sebi_peak_margin_pct}
    if settings.trading_mode is TradingMode.LIVE:
        return KiteOrderRouter(kite, exchange=settings.kite_exchange, **common)
    return PaperOrderRouter(slippage_pct=settings.paper_slippage_pct, **common)


# ====================================================================== premarket
async def _premarket(lookback_hours: int | None, skip_llm: bool) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    now = datetime.now(MARKET_TZ)
    if now.time() > settings.premarket_cutoff:
        logger.warning("premarket.after_cutoff", now=now.isoformat(), cutoff=settings.premarket_cutoff.isoformat())
    since = now - timedelta(hours=lookback_hours or settings.news_lookback_hours)

    engine, sf = await _open_db(settings)
    try:
        fetched, inserted = await _ingest_news(settings, sf)
        analysed = material = 0
        if not skip_llm:
            analysed, material = await _analyse_pending(settings, sf, since, limit=500)
        async with sf() as session, session.begin():
            entries = await build_watchlist(
                session,
                trade_date=now.date(),
                since=since,
                size=settings.watchlist_size,
                allow_shorts=settings.watchlist_allow_shorts,
            )
    finally:
        await engine.dispose()

    typer.echo(f"\nPre-market run {now:%Y-%m-%d %H:%M} IST")
    typer.echo(f"  fetched={fetched} new={inserted} analysed={analysed} material={material}")
    if not entries:
        typer.echo("  No actionable catalysts found; watchlist is empty.")
        return
    typer.echo(f"\nWatchlist for {now.date()}:")
    typer.echo(f"  {'#':>2}  {'TICKER':<12} {'BIAS':<8} {'CATALYST':<22} {'CONF':>5}  HEADS")
    for e in entries:
        typer.echo(
            f"  {e.rank:>2}  {e.ticker:<12} {e.bias.value:<8} {e.catalyst_type.value:<22} "
            f"{e.target_confidence:>5.2f}  {e.supporting_headlines}"
        )


# ====================================================================== live
async def _live() -> None:
    settings = get_settings()
    settings.assert_live_allowed()
    configure_logging(settings.log_level, settings.log_dir)
    if not settings.kite_access_token.get_secret_value():
        raise ConfigError("KITE_ACCESS_TOKEN is missing. Run `kite-login-url` then `kite-session`.")

    today = datetime.now(MARKET_TZ).date()
    engine, sf = await _open_db(settings)
    try:
        async with sf() as session:
            watchlist = await repo.load_watchlist(session, today)
        if not watchlist:
            raise ConfigError("No watchlist for today. Run `run-premarket` first.")

        kite = build_kite_client(settings)
        symbols = [w.ticker for w in watchlist]
        token_map = await asyncio.to_thread(resolve_instrument_tokens, kite, settings.kite_exchange, symbols)
        unresolved = sorted(set(symbols) - set(token_map))
        if unresolved:
            logger.warning("live.unresolved_symbols", symbols=unresolved)
        candidates = {token_map[w.ticker]: (w.ticker, Bias(w.bias)) for w in watchlist if w.ticker in token_map}
        if not candidates:
            raise ConfigError("None of the watchlist symbols resolved to NSE instruments.")

        strategy = ORBStrategy(settings.strategy_params, candidates)
        if not strategy.tracked_symbols:
            raise ConfigError("No watchlist entries survive the strategy filters (check WATCHLIST_ALLOW_SHORTS).")

        risk = RiskManager(settings.risk_limits)
        router = _build_router(settings, kite)
        async with sf() as session:
            router.seed_realised_pnl(await repo.realised_pnl_for_day(session, today, router.mode))

        listener = TickListener(
            api_key=settings.kite_api_key,
            access_token=settings.kite_access_token.get_secret_value(),
            symbols_by_token={token: symbol for symbol, token in token_map.items() if token in candidates},
            reconnect_max_tries=settings.ws_reconnect_max_tries,
            reconnect_max_delay=settings.ws_reconnect_max_delay,
        )
        trading = TradingEngine(
            strategy=strategy,
            router=router,
            risk=risk,
            listener=listener,
            session_factory=sf,
            square_off_time=settings.square_off_time,
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        logger.info("live.starting", mode=router.mode, tracked=strategy.tracked_symbols)
        await listener.start()
        try:
            await trading.run(stop)
        finally:
            await trading.shutdown("SESSION_END")
            await listener.stop()
    finally:
        await engine.dispose()


# ====================================================================== backfill
async def _backfill(days: int, analyse: bool, limit: int) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    now = datetime.now(MARKET_TZ)
    engine, sf = await _open_db(settings)
    try:
        fetched, inserted = await _ingest_news(settings, sf)
        analysed = material = 0
        if analyse:
            analysed, material = await _analyse_pending(settings, sf, now - timedelta(days=days), limit)
    finally:
        await engine.dispose()
    typer.echo(f"Backfill complete: fetched={fetched} new={inserted} analysed={analysed} material={material}")
    typer.echo("Note: RSS feeds only expose recent items; this ingests what the feeds currently publish.")


# ====================================================================== commands
@app.command("run-premarket")
def run_premarket(
    lookback_hours: int = typer.Option(0, help="News lookback window in hours (0 = config default)."),
    skip_llm: bool = typer.Option(False, "--skip-llm", help="Ingest only; do not call the LLM."),
) -> None:
    """Ingest pre-market news, score it with the LLM and print the top-N watchlist."""
    _execute(_premarket(lookback_hours or None, skip_llm))


@app.command("run-live")
def run_live() -> None:
    """Connect to the Kite WebSocket and run the ORB strategy on today's watchlist."""
    _execute(_live())


@app.command("backfill-news")
def backfill_news(
    days: int = typer.Option(2, min=1, max=30, help="Analyse pending headlines from the last N days."),
    analyse: bool = typer.Option(True, "--analyse/--no-analyse", help="Run the LLM on unanalysed headlines."),
    limit: int = typer.Option(200, min=1, max=2000, help="Maximum headlines to analyse in this run."),
) -> None:
    """Ingest current RSS items into the local DB and score any that are still unanalysed."""
    _execute(_backfill(days, analyse, limit))


@app.command("init-db")
def init_database() -> None:
    """Create database tables if they do not exist."""

    async def _init() -> None:
        settings = get_settings()
        engine = create_db_engine(settings.database_url)
        try:
            await init_db(engine)
        finally:
            await engine.dispose()

    _execute(_init())
    typer.echo("Database tables are ready.")


@app.command("kite-login-url")
def kite_login_url() -> None:
    """Print the Kite login URL. Open it, log in, and copy the request_token from the redirect."""
    settings = get_settings()
    if not settings.kite_api_key:
        raise typer.BadParameter("KITE_API_KEY is not set")
    typer.echo(KiteConnect(api_key=settings.kite_api_key).login_url())


@app.command("kite-session")
def kite_session(request_token: str = typer.Option(..., help="request_token from the Kite redirect URL.")) -> None:
    """Exchange a request_token for today's access token. Put the output in .env."""
    settings = get_settings()
    if not settings.kite_api_key or not settings.kite_api_secret.get_secret_value():
        raise typer.BadParameter("KITE_API_KEY and KITE_API_SECRET must be set")
    kite = KiteConnect(api_key=settings.kite_api_key)
    session = kite.generate_session(request_token, api_secret=settings.kite_api_secret.get_secret_value())
    typer.secho("Add this to .env (it expires daily and must not be committed):", fg=typer.colors.YELLOW)
    typer.echo(f"KITE_ACCESS_TOKEN={session['access_token']}")


if __name__ == "__main__":
    app()
