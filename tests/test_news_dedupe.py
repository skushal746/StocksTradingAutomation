from datetime import datetime, timezone

import feedparser

from config import MARKET_TZ
from ingestion.news_scraper import canonical_url, dedupe_items, entry_to_item, headline_key, normalise_title
from schemas import NewsItem

FETCHED = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)


def _item(title: str, url: str, key_day: str = "2026-11-11") -> NewsItem:
    return NewsItem(
        source="test",
        title=title,
        url=url,
        fetched_at=FETCHED,
        dedupe_key=headline_key(title, key_day),
    )


def test_normalise_title_ignores_case_and_punctuation():
    assert normalise_title("  Reliance Q2: Profit   UP 12%!  ") == "reliance q2 profit up 12"


def test_same_headline_same_day_has_same_key_and_different_day_differs():
    assert headline_key("Sensex closes higher", "2026-11-11") == headline_key("SENSEX closes higher!", "2026-11-11")
    assert headline_key("Sensex closes higher", "2026-11-11") != headline_key("Sensex closes higher", "2026-11-12")


def test_canonical_url_strips_tracking_and_trailing_slash():
    url = "https://Example.com/news/story/?utm_source=x&id=7&fbclid=abc#top"
    assert canonical_url(url) == "https://example.com/news/story?id=7"


def test_dedupe_items_collapses_cross_source_duplicates_and_shared_urls():
    items = [
        _item("TCS wins Rs 5,000 crore deal", "https://a.com/tcs?utm_campaign=1"),
        _item("TCS wins Rs 5,000 crore deal!", "https://b.com/tcs"),  # same headline, other source
        _item("Infosys guidance raised", "https://a.com/tcs"),  # same canonical URL as first
        _item("HDFC Bank rallies", "https://c.com/hdfc"),
    ]
    unique = dedupe_items(items)
    assert [i.title for i in unique] == ["TCS wins Rs 5,000 crore deal", "HDFC Bank rallies"]


def test_entry_to_item_parses_rss_and_normalises_fields():
    rss = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item>
        <title>Reliance <b>bags</b> regulatory approval &amp; expands</title>
        <link>https://news.example/reliance-approval</link>
        <pubDate>Wed, 11 Nov 2026 08:30:00 GMT</pubDate>
        <description>&lt;p&gt;Approval received.&lt;/p&gt;</description>
      </item>
    </channel></rss>"""
    parsed = feedparser.parse(rss)
    item = entry_to_item("news.example", parsed.entries[0], FETCHED)
    assert item is not None
    assert item.title == "Reliance bags regulatory approval & expands"
    assert item.summary == "Approval received."
    assert item.published_at == datetime(2026, 11, 11, 8, 30, tzinfo=timezone.utc)
    assert item.published_at.astimezone(MARKET_TZ).date().isoformat() == "2026-11-11"
    assert item.dedupe_key == headline_key(item.title, "2026-11-11")


def test_entry_without_title_is_skipped():
    parsed = feedparser.parse(b"""<rss version="2.0"><channel><item><link>https://x.y/z</link></item></channel></rss>""")
    assert entry_to_item("x", parsed.entries[0], FETCHED) is None
