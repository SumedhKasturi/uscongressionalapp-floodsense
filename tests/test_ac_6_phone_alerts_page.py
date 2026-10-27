"""Story 6 (CAP-8), AC 6.1 and 6.2: the Get phone alerts page (registration, subscribe link and QR code).

The page runs with AppTest; the Supabase insert is mocked with ``responses`` and ntfy is never called.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json

import pytest
import streamlit
from PIL import Image
from streamlit.testing.v1 import AppTest

import config
from data.location import INVALID_ZIP_MESSAGE
from tests.conftest import SUPABASE_TEST_KEY, SUPABASE_TEST_URL
from tests.test_ac_5_help_page import mock_zip

APP = "../app.py"
PAGE_FILE = "../app_pages/phone_alerts.py"
SECRET = "test-topic-secret-123"
REG_URL = f"{SUPABASE_TEST_URL}/rest/v1/registrations"
SAVE_FAILED = "Couldn't save your registration right now; you can still subscribe below"


@pytest.fixture
def ntfy_secret(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC_SECRET", SECRET)
    return SECRET


@pytest.fixture
def supabase_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_TEST_URL)
    monkeypatch.setenv("SUPABASE_KEY", SUPABASE_TEST_KEY)


def open_page(**state):
    at = AppTest.from_file(PAGE_FILE, default_timeout=30)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, at.exception
    return at


def submit(at, zip_code="08807", name="", consent=True):
    at.text_input(key="phone_zip").set_value(zip_code)
    at.text_input(key="phone_name").set_value(name)
    at.checkbox(key="phone_consent").set_value(consent)
    at.button[0].click().run()
    assert not at.exception, at.exception
    return at


def links(at):
    return [(b.proto.label, b.proto.url) for b in at.get("link_button")]


def subscribe_link(at):
    return [u for label, u in links(at) if label == "Subscribe in ntfy"]


def qr_images(at):
    return at.get("image")


def reg_posts(rsps):
    return [c for c in rsps.calls if c.request.url.startswith(REG_URL)]


def expected_topic(zip_code, secret=SECRET):
    """floodsense-{zip}-{h}, h = first 16 chars of base64url(HMAC-SHA256(secret, zip)) without padding."""
    digest = hmac.new(secret.encode(), zip_code.encode(), hashlib.sha256).digest()
    return f"floodsense-{zip_code}-{base64.urlsafe_b64encode(digest).rstrip(b'=').decode()[:16]}"


def topic_url(zip_code):
    return f"https://ntfy.sh/{expected_topic(zip_code)}"


# --- AC 6.1: ZIP and consent required, name optional; a valid submit saves one row ------------

def test_ac_6_1_valid_signup_inserts_row(rsps, ntfy_secret, supabase_env):
    rsps.post(REG_URL, status=201)
    at = submit(open_page())
    posts = reg_posts(rsps)
    assert len(posts) == 1
    req = posts[0].request
    assert json.loads(req.body) == {"zip": "08807", "name": None, "consent": True}
    assert req.headers["apikey"] == SUPABASE_TEST_KEY  # the app's anon key
    assert "return=minimal" in req.headers["Prefer"]  # anon cannot read the row back
    assert [s.value for s in at.success] and "08807" in at.success[0].value
    assert not at.error and not at.warning


def test_ac_6_1_name_is_stored_trimmed(rsps, ntfy_secret, supabase_env):
    rsps.post(REG_URL, status=201)
    submit(open_page(), name="  Uday  ")
    assert json.loads(reg_posts(rsps)[0].request.body) == {"zip": "08807", "name": "Uday", "consent": True}


def test_ac_6_1_name_limited_to_50_chars(ntfy_secret):
    at = open_page()
    assert at.text_input(key="phone_name").max_chars == 50
    assert at.text_input(key="phone_zip").max_chars == 5


def test_ac_6_1_no_consent_no_insert(rsps, ntfy_secret, supabase_env):
    rsps.post(REG_URL, status=201)
    at = submit(open_page(), consent=False)
    assert [e.value for e in at.error] == ["Please agree to receive alerts"]
    assert not reg_posts(rsps)
    assert not subscribe_link(at)


@pytest.mark.parametrize("bad", ["abcde", "00000", "123", ""])
def test_ac_6_1_bad_zip_no_insert(rsps, ntfy_secret, supabase_env, bad):
    rsps.post(REG_URL, status=201)
    at = submit(open_page(), zip_code=bad)
    assert [e.value for e in at.error] == [INVALID_ZIP_MESSAGE]
    assert not reg_posts(rsps)
    assert not subscribe_link(at)


def test_ac_6_1_zip_prefilled_from_current_zip(ntfy_secret):
    at = open_page(current_zip="10001")
    assert at.text_input(key="phone_zip").value == "10001"


def test_ac_6_1_registration_list_never_read(rsps, ntfy_secret, supabase_env):
    rsps.post(REG_URL, status=201)
    submit(open_page())
    assert all(c.request.method == "POST" for c in rsps.calls)


def test_ac_6_1_schema_registrations_anon_insert_only():
    sql = (config.APP_DIR / "supabase" / "schema.sql").read_text()
    reg = sql[sql.index("create table if not exists public.registrations"):sql.index("public.zip_state (")]
    assert "check (zip ~ '^[0-9]{5}$')" in reg
    assert "char_length(name) <= 50" in reg and "check (consent = true)" in reg
    assert "generated always as identity" in reg
    assert "for insert to anon" in reg and "grant insert on public.registrations to anon" in reg
    assert "for select to anon" not in reg and "grant select on public.registrations to anon" not in reg
    state = sql[sql.index("public.zip_state ("):]
    assert "zip        text        primary key" in state and "last_risk" in state and "updated_at" in state
    assert "to anon" not in state.replace("from anon", "")
    assert "revoke all on public.zip_state from anon, authenticated" in state
    # places_versions is unchanged and the script stays re-runnable.
    assert "create table if not exists public.places_versions" in sql
    assert sql.count("create table if not exists") == 3
    assert all(f"drop policy if exists {p}" in sql for p in ("registrations_anon_insert",))


# --- AC 6.2: subscribe link and QR code; one topic per ZIP -----------------------------------

def test_ac_6_2_link_and_qr_after_signup(rsps, ntfy_secret, supabase_env):
    rsps.post(REG_URL, status=201)
    at = submit(open_page())
    assert subscribe_link(at) == [topic_url("08807")]
    imgs = qr_images(at)
    assert len(imgs) == 1
    img = imgs[0].proto.imgs[0]
    assert img.alt and "QR code" in img.alt


def test_ac_6_2_qr_png_decodes_to_the_link(ntfy_secret):
    # Only the helper is needed; load it without running the page.
    src = (config.APP_DIR / "app_pages" / "phone_alerts.py").read_text()
    ns: dict = {}
    exec(src[src.index("def qr_png"):src.index("st.caption(")], {"io": io, "qrcode": __import__("qrcode")}, ns)
    png = ns["qr_png"](topic_url("08807"))
    assert png.startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(png)).size[0] > 100


def test_ac_6_2_same_zip_twice_same_topic(rsps, ntfy_secret, supabase_env):
    rsps.post(REG_URL, status=201)
    at = submit(open_page())
    first = subscribe_link(at)
    at = submit(at)
    assert subscribe_link(at) == first == [topic_url("08807")]
    assert len(reg_posts(rsps)) == 2
    assert config.ntfy_topic("08807") == config.ntfy_topic("08807") == expected_topic("08807")
    assert SECRET not in first[0]


def test_ac_6_2_topics_differ_per_zip_and_hide_the_secret(ntfy_secret):
    a, b = config.ntfy_topic("08807"), config.ntfy_topic("10001")
    suffix_a, suffix_b = a.rsplit("-", 1)[1], b.rsplit("-", 1)[1]
    assert a.startswith("floodsense-08807-") and b.startswith("floodsense-10001-")
    assert len(suffix_a) == len(suffix_b) == 16 and suffix_a != suffix_b
    assert SECRET not in a and SECRET not in b
    assert SECRET not in config.ntfy_topic_url("08807")


def test_ac_6_2_supabase_down_still_shows_link(rsps, ntfy_secret, supabase_env):
    rsps.post(REG_URL, status=503)
    at = submit(open_page())
    assert [w.value for w in at.warning] == [SAVE_FAILED]
    assert not at.success
    assert subscribe_link(at) == [topic_url("08807")]
    assert len(qr_images(at)) == 1


def test_ac_6_2_supabase_unconfigured_still_shows_link(rsps, ntfy_secret):
    at = submit(open_page())
    assert [w.value for w in at.warning] == [SAVE_FAILED]
    assert subscribe_link(at) == [topic_url("08807")]


def test_ac_6_2_secret_missing_not_configured(rsps, monkeypatch, supabase_env):
    monkeypatch.delenv("NTFY_TOPIC_SECRET", raising=False)
    at = open_page()
    assert [w.value for w in at.warning] == ["Phone alerts are not configured"]
    assert not at.get("form") and not at.text_input and not links(at)
    assert config.ntfy_topic("08807") is None and config.ntfy_topic_url("08807") is None


def test_ac_6_2_menu_order_and_page_in_shell(rsps, monkeypatch, ntfy_secret):
    seen = []
    real = streamlit.navigation

    def spy(pages, **kw):
        seen.append(list(pages))
        return real(pages, **kw)

    monkeypatch.setattr(streamlit, "navigation", spy)
    mock_zip(rsps)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    pages = seen[-1]
    assert [p.title for p in pages] == ["View environmental intelligence", "Search weather forecast",
                                        "Flood map", "Flood alerts", "Local news", "Get phone alerts",
                                        "Get help"]
    assert pages[5].url_path == "phone-alerts" and pages[5].icon == ":material/notifications:"
    at.switch_page("app_pages/phone_alerts.py").run()
    assert not at.exception, at.exception
    assert at.text_input(key="phone_zip").value == "08807"


def test_ac_6_1_new_submit_clears_previous_link(rsps, ntfy_secret, supabase_env):
    rsps.post(REG_URL, status=201)
    at = submit(open_page())
    assert subscribe_link(at) == [topic_url("08807")] and at.success
    at = submit(at, zip_code="abcde")
    assert [e.value for e in at.error] == [INVALID_ZIP_MESSAGE]
    assert not subscribe_link(at) and not at.success and not qr_images(at)


def test_ac_6_1_unexpected_insert_error_is_not_swallowed(monkeypatch, ntfy_secret):
    from data import supabase_rest

    def boom(row):
        raise TypeError("bug")

    monkeypatch.setattr(supabase_rest, "insert_registration", boom)
    at = AppTest.from_file(PAGE_FILE, default_timeout=30).run()
    at.checkbox(key="phone_consent").set_value(True)
    at.button[0].click().run()
    assert at.exception  # only Supabase errors become the "couldn't save" warning


# --- The push's link opens the alerts page for that ZIP (the app shell reads ?zip=) ------------

ALERTS_PAGE = "app_pages/alerts.py"


def open_link(zip_value):
    """Open the app as a push link would: ?zip=<zip>, then the alerts page."""
    at = AppTest.from_file(APP, default_timeout=30)
    at.query_params["zip"] = zip_value
    at.run()
    assert not at.exception, at.exception
    at.switch_page(ALERTS_PAGE).run()
    assert not at.exception, at.exception
    return at


def test_ac_6_5_alerts_page_takes_zip_from_link_once(rsps):
    mock_zip(rsps)
    mock_zip(rsps, "10001")
    at = open_link("10001")
    assert at.text_input(key="zip").value == "10001"
    assert at.session_state["current_zip"] == "10001"
    assert "New York, NY (10001)" in [s.value for s in at.subheader]
    # Once used, the link's ZIP no longer overrides what the resident types.
    at.text_input(key="zip").set_value("abcde").run()
    assert at.text_input(key="zip").value == "abcde"
    assert [e.value for e in at.error] == [INVALID_ZIP_MESSAGE]
    at.text_input(key="zip").set_value("08807").run()
    assert at.session_state["current_zip"] == "08807"
    assert "Bridgewater, NJ (08807)" in [s.value for s in at.subheader]


@pytest.mark.parametrize("bad", ["abcde", "00000", "1234"])
def test_ac_6_5_alerts_page_ignores_bad_link_zip(rsps, bad):
    mock_zip(rsps)
    at = open_link(bad)
    assert at.text_input(key="zip").value == "08807"
    assert at.session_state["current_zip"] == "08807"
    assert not at.error
