"""CurrentsAPI client tests. Uses httpx.MockTransport, so no network or API key is needed."""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timezone

import httpx
import pytest

from ingestion.currentsapi_client import CurrentsAPIClient, CurrentsAPIError, article_to_item

FETCHED_AT = datetime(2026, 11, 10, 2, 30, tzinfo=timezone.utc)


def run(fn):
    """Run an async test without pytest-asyncio."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def _article(i: int, **overrides) -> dict:
    base = {
        "id": f"uuid-{i}",
        "title": f"Headline number {i} about TCS",
        "description": "<p>Shares rose after a <b>large</b> contract win.</p>",
        "url": f"https://www.example.com/news/{i}/?utm_source=x",
        "author": "example.com",
        "image": "None",
        "language": "en",
        "category": ["business"],
        "source_category": ["finance"],
        "published": "2026-11-10 01:15:00 +0000",
    }
    base.update(overrides)
    return base


def _client_with(handler, **kwargs) -> CurrentsAPIClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CurrentsAPIClient("test-key", client=http, **kwargs)


def test_article_to_item_maps_fields_and_cleans_text():
    item = article_to_item(_article(1), FETCHED_AT)
    assert item is not None
    assert item.source == "currentsapi:example.com"
    assert item.title == "Headline number 1 about TCS"
    assert item.summary == "Shares rose after a large contract win."
    assert item.published_at == datetime(2026, 11, 10, 1, 15, tzinfo=timezone.utc)
    assert item.raw_payload["article_id"] == "uuid-1"


def test_article_to_item_skips_blank_title():
    assert article_to_item(_article(1, title="   "), FETCHED_AT) is None


def test_source_falls_back_to_url_host_when_author_is_none_string():
    item = article_to_item(_article(1, author="None"), FETCHED_AT)
    assert item is not None
    assert item.source == "currentsapi:example.com"


def test_bad_published_date_becomes_none_and_falls_back_to_fetch_time():
    item = article_to_item(_article(1, published="not a date"), FETCHED_AT)
    assert item is not None
    assert item.published_at is None


@run
async def test_fetch_sends_bearer_header_and_params():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"status": "ok", "news": [_article(1)], "page": 1})

    async with _client_with(handler, language="en", country="IN", category="business", page_size=20) as client:
        items = await client.fetch_latest()

    assert len(items) == 1
    assert seen["auth"] == "Bearer test-key"
    assert seen["params"] == {
        "page_number": "1",
        "page_size": "20",
        "language": "en",
        "country": "IN",
        "category": "business",
    }


@run
async def test_pagination_stops_early_on_short_page():
    pages_requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page_number"]
        pages_requested.append(page)
        news = [_article(i) for i in range(2)]  # 2 < page_size of 3, so this is the last page
        return httpx.Response(200, json={"status": "ok", "news": news, "page": int(page)})

    async with _client_with(handler, page_size=3, max_pages=5) as client:
        items = await client.fetch_latest()

    assert pages_requested == ["1"]
    assert len(items) == 2


@run
async def test_pagination_fetches_up_to_max_pages_when_pages_are_full():
    pages_requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page_number"]
        pages_requested.append(page)
        news = [_article(int(page) * 10 + i) for i in range(2)]
        return httpx.Response(200, json={"status": "ok", "news": news, "page": int(page)})

    async with _client_with(handler, page_size=2, max_pages=3) as client:
        items = await client.fetch_latest()

    assert pages_requested == ["1", "2", "3"]
    assert len(items) == 6


@run
async def test_error_status_payload_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "msg": "Invalid API key"})

    async with _client_with(handler) as client:
        with pytest.raises(CurrentsAPIError, match="Invalid API key"):
            await client.fetch_latest()


@run
async def test_unauthorised_http_error_is_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"status": "401", "msg": "Unauthorized"})

    async with _client_with(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_latest()
    assert calls == 1


def test_blank_api_key_is_rejected():
    with pytest.raises(ValueError):
        CurrentsAPIClient("")

