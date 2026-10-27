"""AC 5.5 (P0 gate): stories 1 to 5 end to end, for 08807 and one other ZIP, from cached fixtures.

The real app shell opens the forecast, Flood alerts, Get help and intelligence pages in turn
with every source mocked, and each must render its key content without an exception.
"""

import pytest
from streamlit.testing.v1 import AppTest

from tests.test_ac_5_help_page import NATIONAL, OFFICES, call_links, mock_zip

APP = "../app.py"
PLACES = {"08807": "Bridgewater, NJ (08807)", "10001": "New York, NY (10001)"}


@pytest.mark.parametrize("zip_code", ["08807", "10001"])
def test_ac_5_5_all_four_pages_render_from_fixtures(rsps, zip_code):
    mock_zip(rsps, zip_code)
    at = AppTest.from_file(APP, default_timeout=30).switch_page("app_pages/forecast.py").run()
    assert not at.exception, at.exception

    # Story 1: forecast, set to the ZIP under test.
    if zip_code != "08807":
        at.text_input(key="zip").set_value(zip_code).run()
        assert not at.exception, at.exception
    assert at.text_input(key="zip").value == zip_code
    assert "Next 12 hours" in [h.value for h in at.header]
    assert not at.error
    assert at.session_state["current_zip"] == zip_code

    # Story 4: Flood alerts, with exactly one risk banner.
    at.switch_page("app_pages/alerts.py").run()
    assert not at.exception, at.exception
    assert at.text_input(key="zip").value == zip_code
    assert PLACES[zip_code] in [s.value for s in at.subheader]
    banners = [el.proto.title for kind in ("success", "warning", "error") for el in getattr(at, kind)]
    assert len(banners) == 1 and banners[0].endswith("risk")
    assert not at.error

    # Story 5: Get help, with the national lines and the ZIP's NWS office.
    at.switch_page("app_pages/help.py").run()
    assert not at.exception, at.exception
    assert at.text_input(key="zip").value == zip_code
    office_number, office_url = OFFICES[zip_code]
    assert call_links(at) == [(f"Call {n}", u) for n, u in NATIONAL.items()] + [(f"Call {office_number}", office_url)]
    assert "Open shelters" in [s.value for s in at.subheader]
    assert not at.error

    # Stories 2 and 3: the knowledge graph built from the places record and live inputs.
    at.switch_page("app_pages/intelligence.py").run()
    assert not at.exception, at.exception
    assert at.text_input(key="zip").value == zip_code
    assert PLACES[zip_code] in [s.value for s in at.subheader]
    assert any("Area risk:" in m.value for m in at.markdown)
    assert len(at.get("vega_lite_chart")) == 1
    assert not at.error
