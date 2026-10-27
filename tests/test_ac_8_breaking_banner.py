"""Story 8 (CAP-6), AC 8.4: the breaking-alert banner under the header on every page.

Every page test runs the real app shell with AppTest; all HTTP is mocked (see conftest).
"""

import logging

import pytest
from streamlit.testing.v1 import AppTest

import app_logging
import assessment_loader
import ui_shared
from tests.test_ac_4_flood_alerts_page import set_alerts
from tests.test_ac_5_help_page import mock_zip
from tests.test_ac_8_news_page import NEWS_RE, rss
from ui_shared import BREAKING_ICON, SEE_ALERTS, pick_alert

APP = "../app.py"
PAGES = ("app_pages/intelligence.py", "app_pages/forecast.py", "app_pages/map.py", "app_pages/alerts.py",
         "app_pages/news.py", "app_pages/phone_alerts.py", "app_pages/help.py")


def mock_app(rsps, *events):
    if events:
        mock_zip(rsps, alerts="nws_alerts_one.json", alerts_edit=set_alerts(*events))
    else:
        mock_zip(rsps)
    rsps.get(NEWS_RE, body=rss(), content_type="application/rss+xml")


def run(at, page=None):
    if page:
        at.switch_page(page)
    at.run()
    assert not at.exception, (page, at.exception)
    return at


def banners(at):
    """(kind, body) of each breaking-alert banner in the main area."""
    return [(kind, el.value) for kind in ("warning", "error") for el in getattr(at.main, kind)
            if el.icon == BREAKING_ICON]


def banner_links(at):
    return [(p.proto.label, p.proto.page) for p in at.main.get("page_link") if p.proto.label == SEE_ALERTS]


def first_main_kinds(at, n=3):
    """Element types at the top of the main area, in order (header html first)."""
    return [type(c).__name__ for c in list(at.main.children.values())[:n]]


# --- Picking the alert ---------------------------------------------------------------

def test_ac_8_4_pick_highest_level_then_severity_then_first():
    watch = {"event": "Flood Watch", "headline": "w", "severity": "Moderate"}
    warning = {"event": "Flash Flood Warning", "headline": "ffw", "severity": "Severe"}
    assert pick_alert([watch, warning]) is warning
    # A non-flood alert has no rule level, so any flood alert outranks it.
    tornado = {"event": "Tornado Warning", "headline": "t", "severity": "Extreme"}
    assert pick_alert([tornado, watch]) is watch
    # Same level: higher severity wins (Extreme > Severe > Moderate > Minor > Unknown).
    adv_minor = {"event": "Flood Advisory", "headline": "a", "severity": "Minor"}
    adv_unknown = {"event": "Flood Advisory", "headline": "b", "severity": ""}
    adv_extreme = {"event": "Flood Watch", "headline": "c", "severity": "Extreme"}
    assert pick_alert([adv_unknown, adv_minor]) is adv_minor
    assert pick_alert([watch, adv_extreme]) is adv_extreme
    # Ties: the first.
    w1, w2 = dict(watch, headline="first"), dict(watch, headline="second")
    assert pick_alert([w1, w2]) is w1
    assert pick_alert([]) is None


@pytest.mark.parametrize("alert, red", [
    ({"event": "Flash Flood Warning", "severity": "Moderate"}, True),
    ({"event": "Flood Watch", "severity": "Severe"}, True),
    ({"event": "Wind Advisory", "severity": "Extreme"}, True),
    ({"event": "Flood Watch", "severity": "Moderate"}, False),
    ({"event": "Wind Advisory", "severity": "Minor"}, False),
])
def test_ac_8_4_color(alert, red):
    assert ui_shared.is_red(alert) is red


# --- The banner on every page --------------------------------------------------------

def test_ac_8_4_no_alert_no_banner_on_any_page(rsps):
    """Matrix row Banner: none."""
    mock_app(rsps)
    at = run(AppTest.from_file(APP, default_timeout=30))
    for page in PAGES:
        run(at, page)
        assert not banners(at) and not banner_links(at), page


def test_ac_8_4_flood_watch_orange_banner_on_every_page(rsps):
    """Matrix row Banner: Flood Watch (Moderate) -> orange, with a link to Flood alerts, on every page."""
    mock_app(rsps, ("Flood Watch", "Moderate"))
    at = run(AppTest.from_file(APP, default_timeout=30))
    for page in PAGES:
        run(at, page)
        assert banners(at) == [("warning", "**Flood Watch**: Flood Watch until 9 PM EDT")], page
        if page == "app_pages/alerts.py":
            # No link to the page you are already on (P6).
            assert not banner_links(at), page
            assert first_main_kinds(at, 2) == ["Html", "Warning"], page
        else:
            assert banner_links(at) == [(SEE_ALERTS, "alerts")], page
            # Under the header (the shell's HTML block), above the page's own content.
            assert first_main_kinds(at) == ["Html", "Warning", "PageLink"], page


def test_ac_8_4_two_alerts_red_flash_flood_warning(rsps):
    """Matrix row Banner: two alerts -> the red Flash Flood Warning banner."""
    mock_app(rsps, ("Flood Watch", "Moderate"), ("Flash Flood Warning", "Severe"))
    at = run(AppTest.from_file(APP, default_timeout=30), "app_pages/news.py")
    assert banners(at) == [("error", "**Flash Flood Warning**: Flash Flood Warning until 9 PM EDT")]


def test_ac_8_4_assessment_failure_hides_banner_and_page_renders(rsps, monkeypatch):
    """Matrix row Banner: failure -> no banner, the page still renders, logged."""
    mock_app(rsps, ("Flash Flood Warning", "Severe"))

    def boom(loc):
        raise RuntimeError("assessment down")

    monkeypatch.setattr(assessment_loader, "load_assessment", boom)
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record)

    h = H()
    app_logging.log.addHandler(h)
    try:
        at = run(AppTest.from_file(APP, default_timeout=30), "app_pages/news.py")
    finally:
        app_logging.log.removeHandler(h)
    assert not banners(at)
    assert "Stay informed" in [s.value for s in at.subheader]  # the news page rendered
    # The shell loads the assessment once (P5); its failure is logged there.
    assert any("Area risk could not be assessed" in r.getMessage() for r in records)


def test_ac_8_4_invalid_zip_no_banner(rsps):
    mock_app(rsps, ("Flash Flood Warning", "Severe"))
    at = run(AppTest.from_file(APP, default_timeout=30), "app_pages/news.py")
    assert banners(at)
    at.text_input(key="zip").set_value("abcde").run()
    assert not at.exception, at.exception
    assert not banners(at)


def test_ac_8_4_banner_adds_no_fetch(rsps, monkeypatch):
    """The banner reuses the shell's cached assessment: the same requests with or without it."""

    def calls_for_one_run():
        rsps.reset()
        assessment_loader.st.cache_data.clear()
        mock_app(rsps, ("Flood Watch", "Moderate"))
        at = run(AppTest.from_file(APP, default_timeout=30), "app_pages/forecast.py")
        return at, sorted(c.request.url for c in rsps.calls)

    calls_for_one_run()  # warm-up: saves the local places copy both measured runs then read
    at, with_banner = calls_for_one_run()
    assert banners(at)
    monkeypatch.setattr(ui_shared, "breaking_alert", lambda *a, **kw: None)
    at, without_banner = calls_for_one_run()
    assert not banners(at)
    assert with_banner == without_banner


def test_ac_8_4_headline_is_escaped(rsps):
    def edit(body):
        set_alerts(("Flood Watch", "Moderate"))(body)
        body["features"][0]["properties"]["headline"] = "Flood *Watch* <b>now</b>"

    mock_zip(rsps, alerts="nws_alerts_one.json", alerts_edit=edit)
    rsps.get(NEWS_RE, body=rss(), content_type="application/rss+xml")
    at = run(AppTest.from_file(APP, default_timeout=30), "app_pages/news.py")
    assert banners(at) == [("warning", r"**Flood Watch**: Flood \*Watch\* \<b\>now\</b\>")]


# --- Review patches (P5-P7) ------------------------------------------------------------

def test_ac_8_4_one_assessment_per_shell_run(rsps, monkeypatch):
    """P5: the risk badge and the banner share one load_assessment call per rerun."""
    mock_app(rsps, ("Flood Watch", "Moderate"))
    calls = []
    real = assessment_loader.load_assessment

    def counting(loc):
        calls.append(loc.zip)
        return real(loc)

    monkeypatch.setattr(assessment_loader, "load_assessment", counting)
    # The news page itself never loads the assessment, so every call here is the shell's.
    at = run(AppTest.from_file(APP, default_timeout=30), "app_pages/news.py")
    assert banners(at)
    assert ":orange-badge[:material/warning: Elevated risk]" in [m.value for m in at.sidebar.markdown]
    assert calls == ["08807"]
    calls.clear()
    run(at)
    assert calls == ["08807"]


def test_ac_8_4_no_self_link_on_flood_alerts_page(rsps):
    """P6: the banner shows on the Flood alerts page, without its "See flood alerts" link."""
    mock_app(rsps, ("Flash Flood Warning", "Severe"))
    at = run(AppTest.from_file(APP, default_timeout=30), "app_pages/alerts.py")
    assert banners(at) == [("error", "**Flash Flood Warning**: Flash Flood Warning until 9 PM EDT")]
    assert not banner_links(at)
    at = run(at, "app_pages/help.py")
    assert banner_links(at) == [(SEE_ALERTS, "alerts")]


def test_ac_8_4_stale_alert_data_caption(rsps):
    """P7: alerts served from the cache get an "Live data as of <time> ET" caption under the banner."""
    import requests
    import responses

    import config
    from data.location import resolve_zip

    mock_app(rsps, ("Flood Watch", "Moderate"))
    at = run(AppTest.from_file(APP, default_timeout=30), "app_pages/news.py")
    assert banners(at)
    assert not [c.value for c in at.main.caption if c.value.startswith(ui_shared.ALERT_DATA_AS_OF)]
    # The alerts feed now times out: the next assessment uses the cached alerts.
    loc = resolve_zip("08807")
    rsps.remove(responses.GET, f"{config.NWS_BASE}/alerts/active?point={loc.lat:.4f},{loc.lon:.4f}")
    rsps.get(f"{config.NWS_BASE}/alerts/active?point={loc.lat:.4f},{loc.lon:.4f}",
             body=requests.exceptions.Timeout("timed out"))
    assessment_loader.st.cache_data.clear()
    run(at)
    assert banners(at) == [("warning", "**Flood Watch**: Flood Watch until 9 PM EDT")]
    stale = [c.value for c in at.main.caption if c.value.startswith(ui_shared.ALERT_DATA_AS_OF)]
    assert len(stale) == 1 and stale[0].endswith(" ET"), stale
    # Directly under the banner.
    kinds = first_main_kinds(at, 4)
    assert kinds[:3] == ["Html", "Warning", "Caption"], kinds


def test_ac_8_4_stale_without_time_says_may_be_out_of_date(monkeypatch):
    from types import SimpleNamespace

    shown = []
    monkeypatch.setattr(ui_shared.st, "warning", lambda body, **kw: shown.append(("warning", body)))
    monkeypatch.setattr(ui_shared.st, "caption", lambda body, **kw: shown.append(("caption", body)))
    monkeypatch.setattr(ui_shared.st, "page_link", lambda *a, **kw: shown.append(("link", kw.get("label"))))
    inputs = SimpleNamespace(alerts=[{"event": "Flood Watch", "headline": "h", "severity": "Moderate"}],
                             stale=True, as_of=None)
    ui_shared.breaking_alert(None, (inputs, None), current_page="news")
    assert shown == [("warning", "**Flood Watch**: h"), ("caption", ui_shared.ALERT_DATA_STALE),
                     ("link", SEE_ALERTS)]
