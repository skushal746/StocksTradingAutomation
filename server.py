"""Long-running service: FastAPI for HTTP, APScheduler for timed jobs, served by uvicorn.

Start with:
    uvicorn server:app --host 0.0.0.0 --port 8000

Scheduled jobs (IST):
    news_scrape   every hour from NEWS_SCRAPE_START_HOUR to NEWS_SCRAPE_END_HOUR (default 04:00-09:00)

The scheduler starts and stops with the server (FastAPI lifespan), so `docker compose up`
brings up the application and its schedule together.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from loguru import logger

from config import MARKET_TZ, Settings, get_settings
from core.logging_setup import configure_logging
from main import _ingest_news, _open_db

SCRAPE_JOB_ID = "news_scrape"


async def scrape_news_job() -> None:
    """One scrape cycle: fetch RSS, NewsData and CurrentsAPI, dedupe, store new headlines.

    Headlines are not sent to the LLM here. The pre-market run analyses whatever is still pending.
    """
    settings = get_settings()
    engine, sf = await _open_db(settings)
    try:
        fetched, inserted = await _ingest_news(settings, sf)
        logger.info("scheduler.news_scrape_done", fetched=fetched, new=inserted)
    except Exception:
        # Log and keep the scheduler alive; the next hour's run will try again.
        logger.exception("scheduler.news_scrape_failed")
    finally:
        await engine.dispose()


def build_scrape_trigger(settings: Settings) -> CronTrigger:
    start, end = settings.news_scrape_start_hour, settings.news_scrape_end_hour
    if start > end:
        raise ValueError(f"NEWS_SCRAPE_START_HOUR ({start}) must not be after NEWS_SCRAPE_END_HOUR ({end})")
    # hour="4-9", minute=0 fires at 04:00, 05:00, ..., 09:00 IST.
    return CronTrigger(hour=f"{start}-{end}", minute=0, timezone=MARKET_TZ)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)

    scheduler = AsyncIOScheduler(timezone=MARKET_TZ)
    if settings.scheduler_enabled:
        scheduler.add_job(
            scrape_news_job,
            build_scrape_trigger(settings),
            id=SCRAPE_JOB_ID,
            replace_existing=True,
            max_instances=1,  # never run two scrapes at once
            coalesce=True,  # missed runs collapse into one
            misfire_grace_time=900,  # still run if it was late by up to 15 minutes
        )
        scheduler.start()
    app.state.scheduler = scheduler
    logger.info(
        "server.started",
        scheduler_enabled=settings.scheduler_enabled,
        scrape_hours=f"{settings.news_scrape_start_hour}-{settings.news_scrape_end_hour} IST",
    )
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        logger.info("server.stopped")


app = FastAPI(title="Stocks Trading Automation", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    scheduler: AsyncIOScheduler = app.state.scheduler
    job = scheduler.get_job(SCRAPE_JOB_ID) if scheduler.running else None
    return {
        "status": "ok",
        "scheduler_running": scheduler.running,
        "next_news_scrape": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }
