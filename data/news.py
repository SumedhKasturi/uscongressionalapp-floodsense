"""Local flood, storm and weather news for any US ZIP, from a Google News RSS search.

The raw RSS XML is fetched network first and cached per ZIP (``data/cache.get_text``), then
parsed with ``feedparser``. Only each item's title, source name, link and published time are
kept, as plain text: feed HTML and summaries are never used.
"""

from __future__ import annotations

import html
import logging
import re
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser

from data import cache

log = logging.getLogger(__name__)

NEWS_BASE = "https://news.google.com/rss/search"
MAX_ITEMS = 15
MAX_AGE_DAYS = 365  # 12 months (user decision: Bridgewater had no storm news in the last 30 days)
# Google News answers generic clients; a plain identifying User-Agent is enough.
HEADERS = {"User-Agent": "FloodSense/1.0 (local flood news)"}

_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")
_NON_WORD = re.compile(r"[\W_]+")


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    link: str
    published: datetime  # UTC


@dataclass(frozen=True)
class NewsResult:
    items: list[NewsItem]
    stale: bool
    fetched_at: str


def news_url(place: str, state: str) -> str:
    """The Google News RSS search for flood, storm or weather news about "Place, ST"."""
    where = ", ".join(p for p in (str(place or "").strip(), str(state or "").strip()) if p)
    q = f'"{where}" (flood OR storm OR weather)'
    return f"{NEWS_BASE}?q={quote(q, safe='')}&hl=en-US&gl=US&ceid=US:en"


def split_place(label: str) -> tuple[str, str]:
    """("Bridgewater", "NJ") from a Location label like "Bridgewater, NJ"."""
    place, sep, state = str(label or "").rpartition(", ")
    return (place, state) if sep else (str(label or ""), "")


def plain(text) -> str:
    """Plain text: tags stripped, entities unescaped, whitespace collapsed."""
    s = _TAG.sub("", str(text or ""))
    s = _TAG.sub("", html.unescape(s))  # entity-escaped tags, e.g. "&lt;b&gt;"
    return _SPACE.sub(" ", s).strip()


def _norm_title(title: str) -> str:
    """Dedupe key: casefolded letters and digits only (any script). May be empty."""
    return _NON_WORD.sub("", title.casefold())


def _published(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime.fromtimestamp(timegm(t), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_items(xml: str, now: datetime | None = None) -> list[NewsItem]:
    """Items from RSS text: newest first, deduped by link and title, at most 12 months old, max 15."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    feed = feedparser.parse(xml)
    items = []
    for entry in feed.entries or []:
        link = str(entry.get("link") or "").strip()
        title = plain(entry.get("title"))
        published = _published(entry)
        if not title or not link.lower().startswith(("https://", "http://")) or published is None:
            continue
        if published < cutoff:
            continue
        source = plain((entry.get("source") or {}).get("title"))
        # Google News titles end in " - <source>"; the source is shown on its own line.
        if source and title.endswith(f" - {source}") and len(title) > len(source) + 3:
            title = title[: -len(source) - 3].rstrip()
        items.append(NewsItem(title=title, source=source, link=link, published=published))

    items.sort(key=lambda i: i.published, reverse=True)
    seen_links, seen_titles, out = set(), set(), []
    for item in items:
        key = _norm_title(item.title)  # empty (e.g. punctuation only): dedupe by link alone
        if item.link in seen_links or (key and key in seen_titles):
            continue
        seen_links.add(item.link)
        if key:
            seen_titles.add(key)
        out.append(item)
        if len(out) == MAX_ITEMS:
            break
    return out


def validate_feed(text: str) -> None:
    """Raise ValueError unless ``text`` looks like an RSS/Atom feed (e.g. not an HTML consent page)."""
    head = str(text or "").lstrip("\ufeff \t\r\n")
    if not head.startswith(("<?xml", "<rss", "<feed")):
        raise ValueError("not an RSS or Atom feed")
    feed = feedparser.parse(text)
    if feed.get("bozo") and not feed.entries:
        raise ValueError(f"unparseable feed: {feed.get('bozo_exception')}")


def fetch_news(loc, now: datetime | None = None) -> NewsResult | None:
    """Recent news for ``loc``'s place, or None when the feed failed with no cached copy."""
    url = news_url(*split_place(loc.place))
    res = cache.get_text(f"news_{loc.zip}", url, headers=HEADERS, validate=validate_feed)
    if res is None:
        return None
    try:
        items = parse_items(res.data, now)
    except Exception:
        log.exception("News feed could not be parsed for %s", loc.zip)
        return None
    return NewsResult(items=items, stale=res.stale, fetched_at=res.fetched_at)
