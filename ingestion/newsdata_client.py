"""NewsData.io "latest news" client (free plan compatible).

Free plan limits this client is written for (see https://newsdata.io/pricing):
  * 200 API credits per day, one credit per successful request
  * 1-10 articles per credit (``size`` parameter)
  * articles are delayed by about 12 hours and only the last 48 hours are available

Only the "latest" endpoint is used. The archive endpoint is a paid feature.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from config import MARKET_TZ
from core.resilience import retry_async
from ingestion.news_scraper import USER_AGENT, _is_retryable, clean_text, headline_key
from schemas import NewsItem

NEWSDATA_LATEST_URL = "https://newsdata.io/api/1/latest"
SOURCE_NAME = "newsdata.io"


class NewsDataError(RuntimeError):
    """The API answered with an error status or an error payload."""


def _parse_pub_date(value: str | None) -> datetime | None:
    """NewsData returns ``pubDate`` as ``YYYY-MM-DD HH:MM:SS`` in UTC."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def article_to_item(article: dict[str, Any], fetched_at: datetime) -> NewsItem | None:
    title = clean_text(article.get("title"))
    if not title:
        return None
    published = _parse_pub_date(article.get("pubDate"))
    # Same keying as RSS items so the same headline from both sources collapses on insert.
    anchor = (published or fetched_at).astimezone(MARKET_TZ).date().isoformat()
    summary = clean_text(article.get("description") or article.get("content"))
    source_id = article.get("source_id") or "unknown"
    return NewsItem(
        source=f"{SOURCE_NAME}:{source_id}"[:100],
        title=title[:500],
        url=(article.get("link") or "").strip()[:1000],
        published_at=published,
        fetched_at=fetched_at,
        summary=summary[:2000],
        dedupe_key=headline_key(title, anchor),
        raw_payload={
            "article_id": article.get("article_id"),
            "keywords": article.get("keywords"),
            "country": article.get("country"),
            "language": article.get("language"),
            "category": article.get("category"),
        },
    )


class NewsDataClient:
    """Fetches the latest articles. Use as an async context manager."""

    def __init__(
        self,
        api_key: str,
        *,
        query: str = "",
        country: str = "",
        language: str = "",
        size: int = 10,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("NewsDataClient requires an API key")
        self._api_key = api_key
        self._query = query
        self._country = country
        self._language = language
        self._size = size
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> NewsDataClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers={"User-Agent": USER_AGENT},
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _params(self) -> dict[str, str]:
        params = {"size": str(self._size)}
        if self._query:
            params["q"] = self._query
        if self._country:
            params["country"] = self._country
        if self._language:
            params["language"] = self._language
        return params

    async def fetch_latest(self) -> list[NewsItem]:
        """One request (one credit). Raises NewsDataError on an error payload."""
        if self._client is None:
            raise RuntimeError("Use NewsDataClient as 'async with NewsDataClient(...)'")
        fetched_at = datetime.now(timezone.utc)
        payload = await retry_async(
            self._get,
            attempts=3,
            base_delay=1.0,
            max_delay=10.0,
            retry_if=_is_retryable,
            op_name="newsdata.latest",
        )
        if payload.get("status") != "success":
            raise NewsDataError(f"NewsData error: {payload.get('results') or payload}")
        items: list[NewsItem] = []
        for article in payload.get("results") or []:
            item = article_to_item(article, fetched_at)
            if item is not None:
                items.append(item)
        logger.info("newsdata.fetched", returned=len(items), total=payload.get("totalResults"))
        return items

    async def _get(self) -> dict[str, Any]:
        assert self._client is not None
        # NewsData authenticates with the X-ACCESS-KEY header (as its official MCP server does), not a query parameter.
        response = await self._client.get(
            NEWSDATA_LATEST_URL,
            params=self._params(),
            headers={"X-ACCESS-KEY": self._api_key},
        )
        response.raise_for_status()
        return response.json()


async def fetch_newsdata(
    api_key: str,
    *,
    query: str,
    country: str,
    language: str,
    size: int,
    timeout: float,
) -> list[NewsItem]:
    """Convenience wrapper used by the pipeline."""
    async with NewsDataClient(
        api_key, query=query, country=country, language=language, size=size, timeout=timeout
    ) as client:
        return await client.fetch_latest()
