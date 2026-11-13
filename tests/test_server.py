"""Scheduler and server tests. No network, database or broker is touched."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from config import MARKET_TZ, Settings
from server import app, build_scrape_trigger


def _fire_times(trigger, start: datetime, count: int) -> list[datetime]:
    times = []
    current = start
    previous = None
    for _ in range(count):
        current = trigger.get_next_fire_time(previous, current)
        previous = current
        times.append(current)
    return times


def test_scrape_runs_hourly_from_4_to_9_ist_inclusive():
    settings = Settings(news_scrape_start_hour=4, news_scrape_end_hour=9)
    trigger = build_scrape_trigger(settings)

    start = datetime(2026, 11, 13, 3, 30, tzinfo=MARKET_TZ)
    fires = _fire_times(trigger, start, 7)

    assert [f.strftime("%H:%M") for f in fires] == [
        "04:00",
        "05:00",
        "06:00",
        "07:00",
        "08:00",
        "09:00",
        "04:00",  # next day wraps around
    ]
    assert all(f.tzinfo is not None and f.utcoffset().total_seconds() == 19800 for f in fires)  # IST = UTC+5:30


def test_no_scrape_outside_window():
    settings = Settings(news_scrape_start_hour=4, news_scrape_end_hour=9)
    trigger = build_scrape_trigger(settings)
    fires = _fire_times(trigger, datetime(2026, 11, 13, 9, 30, tzinfo=MARKET_TZ), 1)
    assert fires[0].hour == 4  # 09:30 is past the window, so the next run is 04:00 the following day


def test_rejects_start_after_end():
    settings = Settings(news_scrape_start_hour=10, news_scrape_end_hour=9)
    with pytest.raises(ValueError):
        build_scrape_trigger(settings)


def test_health_reports_scheduler_and_next_scrape():
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["scheduler_running"] is True
    assert body["next_news_scrape"] is not None
