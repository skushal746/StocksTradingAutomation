"""CurrentsAPI "latest news" client (https://currentsapi.services/en/docs/).

Free plan limits this client is written for (see the docs page):
  * 250 requests per day, quota resets at 00:00 UTC
  * up to 20 articles per request (``page_size``)
  * article descriptions are truncated to about 300 characters

Only ``/v1/latest-news`` is used. Keyword search and date-range archive queries
are not needed for the morning watchlist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from config import MARKET_TZ
from core.resilience import retry_async
from ingestion.news_scraper import USER_AGENT, _is_retryable, clean_text, headline_key
from schemas import NewsItem

CURRENTS_LATEST_URL = "https://api.currentsapi.services/v1/latest-news"
SOURCE_NAME = "currentsapi"
# CurrentsAPI returns "published" as "YYYY-MM-DD HH:MM:SS +0000".
_PUB_FORMAT = "%Y-%m-%d %H:%M:%S %z"


class CurrentsAPIError(RuntimeError):
    """The API answered with an error payload."""


def _parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _PUB_FORMAT).astimezone(timezone.utc)
    except ValueError:
        return None


def _source_name(article: dict[str, Any]) -> str:
    # "author" is usually the publisher's domain. Fall back to the article URL's host.
    author = (article.get("author") or "").strip()
    if author and author.lower() != "none":
        return author.lower()
    host = urlparse(article.get("url") or "").netloc.lower()
    return host.removeprefix("www.") or "unknown"


def article_to_item(article: dict[str, Any], fetched_at: datetime) -> NewsItem | None:
    title = clean_text(article.get("title"))
    if not title:
        return None
    published = _parse_published(article.get("published"))
    # Same keying as the RSS and NewsData items so one headline from several sources collapses on insert.
    anchor = (published or fetched_at).astimezone(MARKET_TZ).date().isoformat()
    return NewsItem(
        source=f"{SOURCE_NAME}:{_source_name(article)}"[:100],
        title=title[:500],
        url=(article.get("url") or "").strip()[:1000],
        published_at=published,
        fetched_at=fetched_at,
        summary=clean_text(article.get("description"))[:2000],
        dedupe_key=headline_key(title, anchor),
        raw_payload={
            "article_id": article.get("id"),
            "category": article.get("category"),
            "source_category": article.get("source_category"),
            "language": article.get("language"),
        },
    )


class CurrentsAPIClient:
    """Fetches the latest articles, one page at a time. Use as an async context manager."""

    def __init__(
        self,
        api_key: str,
        *,
        language: str = "en",
        country: str = "",
        category: str = "",
        page_size: int = 20,
        max_pages: int = 1,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("CurrentsAPIClient requires an API key")
        self._api_key = api_key
        self._language = language
        self._country = country
        self._category = category
        self._page_size = page_size
        self._max_pages = max_pages
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> CurrentsAPIClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _params(self, page: int) -> dict[str, str]:
        params = {"page_number": str(page), "page_size": str(self._page_size)}
        if self._language:
            params["language"] = self._language
        if self._country:
            params["country"] = self._country
        if self._category:
            params["category"] = self._category
        return params

    async def fetch_latest(self) -> list[NewsItem]:
        """Fetches up to ``max_pages`` pages. Stops early when a page comes back short."""
        if self._client is None:
            raise RuntimeError("Use CurrentsAPIClient as 'async with CurrentsAPIClient(...)'")
        fetched_at = datetime.now(timezone.utc)
        items: list[NewsItem] = []
        for page in range(1, self._max_pages + 1):
            payload = await retry_async(
                lambda p=page: self._get(p),
                attempts=3,
                base_delay=1.0,
                max_delay=10.0,
                retry_if=_is_retryable,
                op_name=f"currentsapi.latest:{page}",
            )
            if payload.get("status") != "ok":
                raise CurrentsAPIError(f"CurrentsAPI error: {payload.get('msg') or payload}")
            articles = payload.get("news") or []
            for article in articles:
                item = article_to_item(article, fetched_at)
                if item is not None:
                    items.append(item)
            if len(articles) < self._page_size:
                break
        logger.info("currentsapi.fetched", returned=len(items), pages=page)
        return items

    async def _get(self, page: int) -> dict[str, Any]:
        assert self._client is not None
        # CurrentsAPI authenticates with a bearer token in the Authorization header.
        response = await self._client.get(
            CURRENTS_LATEST_URL,
            params=self._params(page),
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        response.raise_for_status()
        return response.json()


async def fetch_currentsapi(
    api_key: str,
    *,
    language: str,
    country: str,
    category: str,
    page_size: int,
    max_pages: int,
    timeout: float,
) -> list[NewsItem]:
    """Convenience wrapper used by the pipeline."""
    async with CurrentsAPIClient(
        api_key,
        language=language,
        country=country,
        category=category,
        page_size=page_size,
        max_pages=max_pages,
        timeout=timeout,
    ) as client:
        return await client.fetch_latest()
