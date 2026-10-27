"""Story 8 (CAP-6), AC 8.1-8.3 and 8.5: the Local news page and its Google News feed.

Feed tests build RSS text in the test; the page tests run the real app shell with AppTest.
All HTTP is mocked (see conftest).
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests
import streamlit
from streamlit.testing.v1 import AppTest

import app_logging
import news_loader
from data import news
from data.location import Location, resolve_zip
from tests.test_ac_4_flood_alerts_page import cards, texts
from tests.test_ac_5_help_page import mock_zip

APP = "../app.py"
PAGE = "app_pages/news.py"
NEWS_RE = re.compile(r"https://news\.google\.com/rss/search\?.*")
URL_08807 = ("https://news.google.com/rss/search?q=%22Bridgewater%2C%20NJ%22%20%28flood%20OR%20storm%20OR%20weather%29"
             "&hl=en-US&gl=US&ceid=US:en")
NOW = datetime.now(timezone.utc).replace(microsecond=0)
CONSENT_HTML = ("<!DOCTYPE html><html><head><title>Before you continue to Google News</title></head>"
                "<body><form>Accept all</form></body></html>")


# --- helpers -----------------------------------------------------------------------

def item_xml(title, link, age_hours, source="Patch", description="<p>Body <b>html</b></p>"):
    pub = format_datetime(NOW - timedelta(hours=age_hours))
    return (f"<item><title>{title}</title><link>{link}</link><pubDate>{pub}</pubDate>"
            f"<description><![CDATA[{description}]]></description>"
            f'<source url="https://example.com">{source}</source></item>')


def rss(*items) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            "<title>Google News</title>" + "".join(items) + "</channel></rss>")


def feed_20():
    """20 items: 15 unique and recent, 1 duplicate link, 1 duplicate title, 3 older than 12 months."""
    items = []
    # 15 recent unique items, ages 1..15 h, listed out of order.
    for age in (7, 2, 15, 1, 9, 4, 12, 3, 14, 5, 10, 6, 13, 8, 11):
        items.append(item_xml(f"Storm story {age} - Patch", f"https://news.example/{age}", age))
    items.append(item_xml("Another headline - Patch", "https://news.example/3", 20))  # duplicate link
    items.append(item_xml("STORM story 5! - Patch", "https://news.example/dup-title", 30))  # duplicate title
    for days in (366, 400, 500):  # older than MAX_AGE_DAYS (365)
        items.append(item_xml(f"Old storm {days} - Patch", f"https://news.example/old-{days}", days * 24))
    return rss(*items)


def mock_news(rsps, body=None, status=200):
    if isinstance(body, Exception):
        rsps.get(NEWS_RE, body=body)
    else:
        rsps.get(NEWS_RE, body=body if body is not None else rss(), status=status,
                 content_type="application/rss+xml")


def news_calls(rsps):
    return [c for c in rsps.calls if c.request.url.startswith("https://news.google.com/")]


def open_news(rsps, body=None, status=200, zip_code="08807", **kw):
    mock_zip(rsps, zip_code, **kw)
    mock_news(rsps, body, status)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    at.switch_page(PAGE).run()
    assert not at.exception, at.exception
    return at


def news_rows(at):
    """(title markdown, caption) of each bordered news row."""
    return [tuple(texts(c)) for c in cards(at)]


def bdw():
    return resolve_zip("08807")


# --- AC 8.1: Google News search for the ZIP's place; title, source, link, date only -----

def test_ac_8_1_menu_has_local_news_after_flood_alerts(rsps, monkeypatch):
    seen = []
    real = streamlit.navigation

    def spy(pages, **kw):
        seen.append(list(pages))
        return real(pages, **kw)

    monkeypatch.setattr(streamlit, "navigation", spy)
    open_news(rsps)
    pages = seen[-1]
    titles = [p.title for p in pages]
    assert titles.index("Local news") == titles.index("Flood alerts") + 1
    page = pages[titles.index("Local news")]
    assert page.url_path == "news" and page.icon == ":material/newspaper:"


def test_ac_8_1_news_url_for_08807():
    assert news.news_url("Bridgewater", "NJ") == URL_08807


def test_ac_8_1_other_zip_queries_its_place(rsps):
    """Matrix row Other ZIP: 98101 searches "Seattle, WA"."""
    mock_news(rsps, rss(item_xml("Seattle storm - KING 5", "https://news.example/sea", 2, source="KING 5")))
    loc = Location(zip="98101", lat=47.6114, lon=-122.3305, place="Seattle, WA")
    res = news.fetch_news(loc)
    assert [i.title for i in res.items] == ["Seattle storm"]
    url = news_calls(rsps)[0].request.url
    assert "q=%22Seattle%2C%20WA%22%20%28flood%20OR%20storm%20OR%20weather%29" in url
    assert url.endswith("&hl=en-US&gl=US&ceid=US:en")


def test_ac_8_1_page_shows_title_link_source_and_date_in_et(rsps):
    body = rss(item_xml("Middle Brook rises after storm - TAPinto", "https://news.example/a", 3, source="TAPinto"))
    at = open_news(rsps, body)
    assert news_calls(rsps)[0].request.url == URL_08807
    assert "Bridgewater, NJ (08807)" in [s.value for s in at.subheader]
    assert any("Bridgewater, NJ" in c.value for c in at.caption)
    (row,) = news_rows(at)
    assert row[0] == "[Middle Brook rises after storm](https://news.example/a)"
    source, date = row[1].split(" · ")
    assert source == "TAPinto"
    assert date.endswith(" ET")
    assert date == (NOW - timedelta(hours=3)).astimezone(news_page_tz()).strftime("%b %d, %Y, %I:%M %p ET")
    assert re.fullmatch(r"[A-Z][a-z]{2} \d{2}, \d{4}, \d{2}:\d{2} [AP]M ET", date)


def news_page_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/New_York")


def test_ac_8_1_html_in_title_is_plain_text_and_summary_never_shown(rsps):
    """Matrix row HTML in title: "<b>Flood</b> & more" shows as plain text."""
    body = rss(item_xml("&lt;b&gt;Flood&lt;/b&gt; &amp;amp; more", "https://news.example/h", 1,
                        description='<img src="x" onerror="alert(1)"><b>SECRET SUMMARY</b>'))
    items = news.parse_items(body)
    assert [i.title for i in items] == ["Flood & more"]
    at = open_news(rsps, body)
    (row,) = news_rows(at)
    assert row[0] == "[Flood & more](https://news.example/h)"
    every_text = [m.value for m in at.markdown] + [c.value for c in at.caption]
    assert not any("SECRET SUMMARY" in t or "<img" in t or "<b>" in t for t in every_text)
    assert not at.get("html")[1:]  # only the shell's header block is HTML


def test_ac_8_1_plain_strips_tags_and_unescapes():
    assert news.plain("<b>Flood</b> &amp; more") == "Flood & more"
    assert news.plain("&lt;i&gt;Storm&lt;/i&gt;  surge\n") == "Storm surge"


def test_ac_8_1_markdown_in_title_is_escaped(rsps):
    body = rss(item_xml("Rain *totals* [link](javascript:x) - Patch", "https://news.example/m", 1))
    at = open_news(rsps, body)
    (row,) = news_rows(at)
    assert row[0] == r"[Rain \*totals\* \[link\](javascript:x)](https://news.example/m)"


def test_ac_8_1_non_http_links_are_dropped():
    body = rss(item_xml("Bad link", "javascript:alert(1)", 1), item_xml("Good", "https://news.example/g", 2))
    assert [i.link for i in news.parse_items(body)] == ["https://news.example/g"]


# --- AC 8.2: newest first, deduped, 12-month cutoff, at most 15 -----------------------

def test_ac_8_2_sorted_deduped_cut_off_and_capped():
    """Matrix row Normal: 20 items, 2 duplicates, 3 older than 12 months -> the 15 newest unique."""
    items = news.parse_items(feed_20())
    assert len(items) == 15
    assert [i.link for i in items] == [f"https://news.example/{age}" for age in range(1, 16)]
    assert all(i.title.startswith("Storm story ") and i.source == "Patch" for i in items)
    published = [i.published for i in items]
    assert published == sorted(published, reverse=True)


def test_ac_8_2_cap_of_15():
    body = rss(*(item_xml(f"Story {n}", f"https://news.example/{n}", n) for n in range(1, 26)))
    items = news.parse_items(body)
    assert [i.title for i in items] == [f"Story {n}" for n in range(1, 16)]


def test_ac_8_2_page_lists_15_newest_unique(rsps):
    at = open_news(rsps, feed_20())
    rows = news_rows(at)
    assert len(rows) == 15
    assert rows[0][0] == "[Storm story 1](https://news.example/1)"
    assert rows[-1][0] == "[Storm story 15](https://news.example/15)"


def test_ac_8_2_no_news_message(rsps):
    """Matrix row No news: a feed with 0 items."""
    at = open_news(rsps, rss())
    assert not news_rows(at)
    assert [i.value for i in at.info] == ["No recent flood or storm news for Bridgewater, NJ"]


# --- AC 8.3: feed failure falls back to cache, else "News unavailable" -------------------

def seed_cache(rsps, body):
    """One good fetch, so the per-ZIP cache holds the feed."""
    mock_news(rsps, body)
    res = news.fetch_news(bdw())
    assert res is not None and not res.stale
    rsps.reset()  # the page run below registers its own mocks


@pytest.fixture
def app_log_records():
    """Records from the app logger (it does not propagate to the root logger)."""
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record)

    h = H(level=logging.DEBUG)
    loggers = [app_logging.log, logging.getLogger("data.cache")]
    for lg in loggers:
        lg.addHandler(h)
    yield records
    for lg in loggers:
        lg.removeHandler(h)


FAILURES = {
    "timeout": (requests.exceptions.Timeout("timed out"), 200),
    "5xx": ("server error", 503),
    "html": (CONSENT_HTML, 200),  # a 200 consent page is not a feed
    "empty": ("", 200),
}


@pytest.mark.parametrize("failure", list(FAILURES))
def test_ac_8_3_feed_fails_uses_cache_with_caption(rsps, failure, app_log_records):
    seed_cache(rsps, rss(item_xml("Cached storm news - Patch", "https://news.example/c", 2)))
    at = open_news(rsps, *FAILURES[failure])
    assert news_rows(at)[0][0] == "[Cached storm news](https://news.example/c)"
    captions = [c.value for c in at.caption]
    assert any(c.startswith("News as of ") and c.endswith(" ET") for c in captions), captions
    assert any("using cache" in r.getMessage() and "news.google.com" in r.getMessage() for r in app_log_records)


def test_ac_8_3_fetch_uses_10s_timeout(rsps, monkeypatch):
    seen = {}
    real = requests.get

    def spy(url, **kw):
        seen["timeout"] = kw.get("timeout")
        return real(url, **kw)

    monkeypatch.setattr(requests, "get", spy)
    mock_news(rsps, rss())
    news.fetch_news(bdw())
    assert seen["timeout"] == 10


@pytest.mark.parametrize("failure", list(FAILURES))
def test_ac_8_3_feed_fails_no_cache_says_unavailable(rsps, failure, app_log_records):
    at = open_news(rsps, *FAILURES[failure])
    assert not news_rows(at)
    assert "News unavailable right now" in [i.value for i in at.info]
    assert any("no cached copy" in r.getMessage() for r in app_log_records)


def test_ac_8_3_fetch_news_none_without_cache(rsps):
    mock_news(rsps, requests.exceptions.ConnectionError("down"))
    assert news.fetch_news(bdw()) is None


def test_ac_8_3_fresh_feed_has_no_stale_caption(rsps):
    at = open_news(rsps, rss(item_xml("Fresh - Patch", "https://news.example/f", 1)))
    assert not [c.value for c in at.caption if c.value.startswith("News as of")]


# --- AC 8.5: NWS office and FEMA alert links --------------------------------------------

def test_ac_8_5_links_to_nws_office_and_fema_alerts(rsps):
    at = open_news(rsps)
    assert "Stay informed" in [s.value for s in at.subheader]
    urls = [b.proto.url for b in at.get("link_button")]
    assert "https://www.weather.gov/phi" in urls
    assert "https://www.ready.gov/alerts" in urls


def test_ac_8_5_links_shown_when_news_unavailable(rsps):
    at = open_news(rsps, requests.exceptions.Timeout("timed out"))
    urls = [b.proto.url for b in at.get("link_button")]
    assert urls == ["https://www.weather.gov/phi", "https://www.ready.gov/alerts"]


def test_ac_8_invalid_zip_asks_for_zip(rsps):
    from ui_shared import ASK_FOR_ZIP

    at = open_news(rsps)
    at.text_input(key="zip").set_value("abc").run()
    assert not at.exception, at.exception
    assert ASK_FOR_ZIP in [i.value for i in at.info]
    assert not news_rows(at)


# --- Review patches (P1, P2, P4, P8-P10) ----------------------------------------------

def test_ac_8_2_keeps_items_up_to_12_months():
    body = rss(item_xml("Half a year ago", "https://news.example/200", 200 * 24),
               item_xml("Just inside a year", "https://news.example/364", 364 * 24),
               item_xml("Just over a year", "https://news.example/366", 366 * 24))
    assert [i.link for i in news.parse_items(body)] == ["https://news.example/200", "https://news.example/364"]


def test_ac_8_2_non_latin_titles_are_kept():
    """Unicode-aware title dedupe: two different non-Latin titles are both kept."""
    body = rss(item_xml("洪水警報が発令", "https://news.example/ja", 1),
               item_xml("Наводнение в городе", "https://news.example/ru", 2),
               item_xml("наводнение В ГОРОДЕ!", "https://news.example/ru-dup", 3))
    assert [i.link for i in news.parse_items(body)] == ["https://news.example/ja", "https://news.example/ru"]


def test_ac_8_2_punctuation_only_titles_dedupe_by_link_only():
    body = rss(item_xml("!!!", "https://news.example/p1", 1), item_xml("???", "https://news.example/p2", 2),
               item_xml("...", "https://news.example/p1", 3))
    assert [i.link for i in news.parse_items(body)] == ["https://news.example/p1", "https://news.example/p2"]


@pytest.mark.parametrize("body", [CONSENT_HTML, "", "   ", "<rss><channel><title>broken", "plain text"])
def test_ac_8_3_bad_body_never_replaces_good_cache(rsps, body):
    seed_cache(rsps, rss(item_xml("Good cached news - Patch", "https://news.example/good", 2)))
    mock_news(rsps, body)
    res = news.fetch_news(bdw())
    assert res.stale and [i.title for i in res.items] == ["Good cached news"]
    # The cache file still holds the good feed.
    rsps.reset()
    mock_news(rsps, requests.exceptions.Timeout("timed out"))
    assert [i.title for i in news.fetch_news(bdw()).items] == ["Good cached news"]


def test_ac_8_3_validate_feed():
    news.validate_feed(rss())
    news.validate_feed("\n  " + rss(item_xml("Leading space", "https://news.example/s", 1)))
    news.validate_feed('<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>')
    for bad in (CONSENT_HTML, "", "not xml", "<rss><channel><title>broken"):
        with pytest.raises(ValueError):
            news.validate_feed(bad)


def test_ac_8_3_reruns_reuse_cached_news(rsps):
    """P1: the page's news is cached per ZIP, so a rerun does not fetch Google News again."""
    at = open_news(rsps, rss(item_xml("Fresh - Patch", "https://news.example/f", 1)))
    at.run()
    assert not at.exception, at.exception
    assert len(news_calls(rsps)) == 1
    assert news_rows(at)[0][0] == "[Fresh](https://news.example/f)"


def test_ac_8_3_unavailable_news_is_not_cached(rsps):
    from news_loader import load_news

    mock_news(rsps, requests.exceptions.Timeout("timed out"))
    assert load_news(bdw()) is None
    rsps.reset()
    mock_news(rsps, rss(item_xml("Back - Patch", "https://news.example/b", 1)))
    assert [i.title for i in load_news(bdw()).items] == ["Back"]


def test_ac_8_1_link_parens_escaped(rsps):
    link = "https://news.example/a(b)c d<e>"
    body = rss(item_xml("Odd link", link.replace("<", "&lt;").replace(">", "&gt;"), 1))
    at = open_news(rsps, body)
    (row,) = news_rows(at)
    assert row[0] == "[Odd link](https://news.example/a%28b%29c%20d%3Ce%3E)"
    assert news_loader.safe_url("https://x.example/p?q=a b&r=(1)#f") == "https://x.example/p?q=a%20b&r=%281%29#f"


def test_ac_8_5_office_url_requires_three_letter_wfo(monkeypatch):
    helpers = news_loader
    loc = bdw()
    for wfo, want in (("PHI", "https://www.weather.gov/phi"), ("okx", "https://www.weather.gov/okx"),
                      ("PH", None), ("PHIL", None), ("../x", None), ("", None), (None, None)):
        monkeypatch.setattr(helpers, "load_places", lambda loc, w=wfo: {"area": {"wfo": w}})
        assert helpers.office_url(loc) == want, wfo


def test_ac_8_1_fmt_time_never_raises():
    helpers = news_loader
    assert helpers.fmt_time("2025-11-03T21:10:00+00:00") == "Nov 03, 2025, 04:10 PM ET"
    assert helpers.fmt_time(None) == ""
    assert helpers.fmt_time("not a time") == "not a time"
    assert helpers.fmt_time(datetime.min).startswith("0001-01-01")  # OverflowError on conversion to ET
