"""Asynchronous RSS/Atom ingestion with headline and URL deduplication.

Deduplication happens at two levels:
  * in-batch: identical normalised headline (same IST day) or identical canonical URL
  * persistent: ``dedupe_key`` has a UNIQUE constraint in ``news_events``
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx
from loguru import logger

from config import MARKET_TZ
from core.resilience import retry_async
from schemas import NewsItem

USER_AGENT = "stocks-trading-automation/0.1 (+RSS reader)"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TAG_RE = re.compile(r"<[^>]+>")
_TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "mc_")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(html.unescape(_TAG_RE.sub(" ", value)).split())


def normalise_title(title: str) -> str:
    return _NON_ALNUM.sub(" ", title.lower()).strip()


def headline_key(title: str, day_iso: str) -> str:
    """Stable hash of a normalised headline scoped to one calendar day.

    Scoping by day lets recurring headlines ("Sensex closes higher") be stored
    on different days while still collapsing duplicates within a day.
    """
    basis = f"{normalise_title(title)}|{day_iso}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlparse(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
    ]
    return urlunparse(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", urlencode(query), "")
    )


def dedupe_items(items: Iterable[NewsItem]) -> list[NewsItem]:
    """Keep the first occurrence of each headline key or canonical URL."""
    seen_keys: set[str] = set()
    seen_urls: set[str] = set()
    unique: list[NewsItem] = []
    for item in items:
        url_key = canonical_url(item.url)
        if item.dedupe_key in seen_keys or (url_key and url_key in seen_urls):
            continue
        seen_keys.add(item.dedupe_key)
        if url_key:
            seen_urls.add(url_key)
        unique.append(item)
    return unique


def _published_at(entry: Any) -> datetime | None:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct:
        return datetime(*struct[:6], tzinfo=timezone.utc)
    return None


def entry_to_item(source: str, entry: Any, fetched_at: datetime) -> NewsItem | None:
    title = clean_text(entry.get("title"))
    if not title:
        return None
    url = (entry.get("link") or "").strip()
    published = _published_at(entry)
    anchor_day = (published or fetched_at).astimezone(MARKET_TZ).date().isoformat()
    tags = [t.get("term") for t in (entry.get("tags") or []) if t.get("term")]
    return NewsItem(
        source=source,
        title=title[:500],
        url=url[:1000],
        published_at=published,
        fetched_at=fetched_at,
        summary=clean_text(entry.get("summary"))[:2000],
        dedupe_key=headline_key(title, anchor_day),
        raw_payload={"id": entry.get("id"), "published": entry.get("published"), "tags": tags},
    )


def _source_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.") or url


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


class NewsScraper:
    """Fetches all configured feeds concurrently. Use as an async context manager."""

    def __init__(
        self,
        feed_urls: Sequence[str],
        *,
        timeout: float = 10.0,
        max_concurrency: int = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._feed_urls = list(feed_urls)
        self._timeout = timeout
        self._sem = asyncio.Semaphore(max_concurrency)
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> NewsScraper:
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

    async def fetch_all(self) -> list[NewsItem]:
        if self._client is None:
            raise RuntimeError("Use NewsScraper as 'async with NewsScraper(...)'")
        fetched_at = datetime.now(timezone.utc)
        results = await asyncio.gather(
            *(self._fetch_feed(url, fetched_at) for url in self._feed_urls),
            return_exceptions=True,
        )
        items: list[NewsItem] = []
        for url, result in zip(self._feed_urls, results, strict=True):
            if isinstance(result, BaseException):
                # One dead feed must never stop the pipeline.
                logger.error("news.feed_failed", feed=url, error=repr(result))
                continue
            logger.info("news.feed_fetched", feed=url, entries=len(result))
            items.extend(result)
        unique = dedupe_items(items)
        logger.info("news.batch_deduped", raw=len(items), unique=len(unique))
        return unique

    async def _fetch_feed(self, url: str, fetched_at: datetime) -> list[NewsItem]:
        source = _source_name(url)
        async with self._sem:
            body = await retry_async(
                lambda: self._get(url),
                attempts=3,
                base_delay=1.0,
                max_delay=10.0,
                retry_if=_is_retryable,
                op_name=f"news.get:{source}",
            )
        parsed = await asyncio.to_thread(feedparser.parse, body)
        if parsed.get("bozo") and not parsed.get("entries"):
            raise ValueError(f"unparseable feed {url}: {parsed.get('bozo_exception')!r}")
        items = []
        for entry in parsed.get("entries", []):
            item = entry_to_item(source, entry, fetched_at)
            if item is not None:
                items.append(item)
        return items

    async def _get(self, url: str) -> bytes:
        assert self._client is not None
        response = await self._client.get(url)
        response.raise_for_status()
        return response.content
