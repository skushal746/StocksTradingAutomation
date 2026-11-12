"""NewsData.io client tests. Uses httpx.MockTransport, so no network or API key is needed."""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timezone

import httpx
import pytest

from ingestion.news_scraper import headline_key
from ingestion.newsdata_client import NewsDataClient, NewsDataError, article_to_item

FETCHED_AT = datetime(2026, 11, 12, 2, 30, tzinfo=timezone.utc)


def run(fn):
    """Run an async test without pytest-asyncio."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def _article(**overrides):
    base = {
        "article_id": "abc123",
        "title": "Reliance Industries wins &amp; new contract",
        "link": "https://example.com/reliance-contract",
        "keywords": ["reliance", "contract"],
        "description": "<p>Details of the contract.</p>",
        "pubDate": "2026-11-11 10:15:00",
        "source_id": "moneycontrol",
        "country": ["india"],
        "language": "english",
        "category": ["business"],
    }
    base.update(overrides)
    return base


def _client_with(handler, **kwargs) -> NewsDataClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NewsDataClient("test-key", client=http, **kwargs)


@run
async def test_parses_success_payload_into_news_items():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "totalResults": 1, "results": [_article()]})

    async with _client_with(handler) as client:
        items = await client.fetch_latest()

    assert len(items) == 1
    item = items[0]
    assert item.source == "newsdata.io:moneycontrol"
    assert item.title == "Reliance Industries wins & new contract"
    assert item.url == "https://example.com/reliance-contract"
    assert item.summary == "Details of the contract."
    assert item.published_at == datetime(2026, 11, 11, 10, 15, tzinfo=timezone.utc)
    assert item.raw_payload["article_id"] == "abc123"


@run
async def test_request_carries_filters_and_key():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        seen["x-access-key"] = request.headers.get("X-ACCESS-KEY", "")
        return httpx.Response(200, json={"status": "success", "results": []})

    async with _client_with(handler, query="NSE OR Nifty", country="in", language="en", size=7) as client:
        assert await client.fetch_latest() == []

    assert seen == {"q": "NSE OR Nifty", "country": "in", "language": "en", "size": "7", "x-access-key": "test-key"}
    assert "apikey" not in seen


@run
async def test_error_payload_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "results": {"message": "Daily limit reached"}})

    async with _client_with(handler) as client:
        with pytest.raises(NewsDataError, match="Daily limit reached"):
            await client.fetch_latest()


@run
async def test_unauthorised_is_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"status": "error"})

    async with _client_with(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_latest()
    assert calls == 1


@run
async def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("core.resilience.asyncio.sleep", no_sleep)
    responses = iter([httpx.Response(429), httpx.Response(200, json={"status": "success", "results": []})])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with _client_with(handler) as client:
        assert await client.fetch_latest() == []


def test_article_without_title_is_skipped():
    assert article_to_item(_article(title="   "), FETCHED_AT) is None


def test_same_headline_same_day_gets_same_key_as_rss():
    item = article_to_item(_article(), FETCHED_AT)
    assert item is not None
    # RSS keys use the IST calendar day of the published time; 10:15 UTC on 11 Nov is 15:45 IST, same day.
    assert item.dedupe_key == headline_key("Reliance Industries wins & new contract", "2026-11-11")


def test_missing_published_date_falls_back_to_fetch_time():
    item = article_to_item(_article(pubDate=None), FETCHED_AT)
    assert item is not None
    assert item.published_at is None
    assert item.fetched_at == FETCHED_AT


def test_empty_api_key_is_rejected():
    with pytest.raises(ValueError):
        NewsDataClient("")
