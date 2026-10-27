"""AC 1.5 and the I/O matrix: the home page shows forecast and alerts for 08807 and 10001."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests
from streamlit.testing.v1 import AppTest

from data import location
from data.location import resolve_zip
from tests.conftest import NWPS_GAUGES_URL, load_fixture, mock_gauges, mock_live, mock_nws

APP = "../app.py"
FORECAST = "app_pages/forecast.py"  # no longer the default page; its url_path matches its filename


def new_app():
    return AppTest.from_file(APP, default_timeout=20).switch_page(FORECAST)


def run_app(zip_code=None, at=None):
    at = at or new_app()
    at.run()
    if zip_code is not None:
        at.text_input(key="zip").set_value(zip_code).run()
    assert not at.exception, at.exception
    return at


def texts(at):
    return [m.value for m in at.markdown] + [e.value for e in at.error] + [s.value for s in at.success] + [
        w.value for w in at.warning
    ] + [i.value for i in at.info]


def gauge_table(at):
    return at.dataframe[-1].value.set_index("Site")


@pytest.mark.parametrize("zip_code", ["08807", "10001"])
def test_ac_1_5_forecast_and_no_active_alerts(rsps, zip_code):
    mock_live(rsps, resolve_zip(zip_code))
    at = run_app(None if zip_code == "08807" else zip_code)
    assert at.text_input(key="zip").value == zip_code
    forecast = at.dataframe[0].value
    assert len(forecast) == 12
    assert list(forecast.columns) == ["Time", "Temp (°F)", "Rain chance (%)", "Forecast"]
    # First row: real fixture temperature, current hour rendered in ET.
    first_temp = load_fixture(f"nws_hourly_{zip_code}.json")["properties"]["periods"][0]["temperature"]
    assert forecast.iloc[0]["Temp (°F)"] == first_temp
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    assert forecast.iloc[0]["Time"] == hour.astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %I:%M %p ET")
    assert [s.value for s in at.success] == ["No active alerts"]
    assert not at.error and not at.warning
    gauges = gauge_table(at)
    assert list(gauges.columns) == ["Gauge", "Distance (mi)", "Level", "Reading time", "Flood status", "Status"]
    nearest = NEAREST[zip_code]
    assert list(gauges.index) == nearest[0]
    assert list(gauges["Distance (mi)"]) == nearest[1]
    assert list(gauges["Distance (mi)"]) == sorted(gauges["Distance (mi)"])
    first = nearest[0][0]
    assert gauges.loc[first, "Status"] == "live"
    assert gauges.loc[first, "Reading time"].endswith(" ET")
    no_reading = nearest[2]
    if no_reading:
        assert gauges.loc[no_reading, "Level"] == "no reading"
        assert gauges.loc[no_reading, "Status"] == "no reading"


NEAREST = {  # (sites nearest first, distances, site with no valid value) from the bBox fixtures
    "08807": (["SMVN4", "MRTN4", "MNVN4", "BDBN4", "BDKN4", "MILN4"],
              [1.2, 2.4, 3.3, 4.6, 4.9, 8.0], "BDKN4"),
    "10001": (["BATN6", "NWKN4", "THRN4", "BGNN6", "VRON4", "HAMN6"],
              [3.4, 7.0, 10.6, 10.9, 14.2, 14.8], "BGNN6"),
}


def test_ac_1_5_default_zip_is_08807(rsps):
    mock_live(rsps, resolve_zip("08807"))
    at = run_app()
    assert at.text_input(key="zip").value == "08807"
    assert any("Bridgewater" in s.value for s in at.subheader)
    assert "Stream gauges near Bridgewater, NJ" in [h.value for h in at.header]


def test_ac_1_5_other_zip_shows_its_own_nearby_gauges(rsps):
    mock_live(rsps, resolve_zip("10001"))
    at = run_app("10001")
    assert "Stream gauges near New York, NY" in [h.value for h in at.header]
    assert not any("Bridgewater" in h.value for h in at.header)
    assert not any("Bridgewater" in c.value for c in at.caption)
    names = list(gauge_table(at)["Gauge"])
    assert names[0] == "New York Harbor at The Battery (IN MLLW)"
    assert not any("Middle Brook" in n or "Raritan" in n for n in names)


def test_ac_1_5_flood_status_column_labels(rsps):
    mock_live(rsps, resolve_zip("08807"))
    flood = gauge_table(run_app())["Flood status"]
    assert flood["MNVN4"] == "No flooding"
    assert flood["MILN4"] == "Minor"
    assert flood["SMVN4"] == ""  # not_defined
    assert flood["BDKN4"] == ""  # no reading


def test_ac_1_5_no_gauges_found_message(rsps):
    loc = resolve_zip("08807")
    mock_nws(rsps, loc)
    rsps.get(NWPS_GAUGES_URL, json={"gauges": []})
    at = run_app()
    assert [i.value for i in at.info] == ["No active stream gauges within about 35 miles"]
    assert not at.error
    assert len(at.dataframe) == 1  # forecast only


def test_ac_1_5_active_alert_shows_event_headline_severity(rsps):
    mock_live(rsps, resolve_zip("08807"), alerts="nws_alerts_one.json")
    at = run_app()
    body = " ".join(texts(at))
    assert "Coastal Flood Advisory" in body
    assert "Minor" in body
    assert "issued October 24" in body
    assert not at.success


def test_ac_1_5_alert_without_event_falls_back_to_alert(rsps):
    def blank_event(body):
        body["features"][0]["properties"]["event"] = ""

    mock_live(rsps, resolve_zip("08807"), alerts="nws_alerts_one.json", alerts_edit=blank_event)
    at = run_app()
    md = [m.value for m in at.markdown]
    assert any(m.startswith("**Alert**") for m in md)
    assert not any("****" in m for m in md)


def test_ac_1_5_null_rain_chance_is_blank_not_zero(rsps):
    def null_pop(body):
        body["properties"]["periods"][0]["probabilityOfPrecipitation"] = {"unitCode": "wmoUnit:percent", "value": None}
        body["properties"]["periods"][1].pop("probabilityOfPrecipitation")

    mock_live(rsps, resolve_zip("08807"), hourly_edit=null_pop)
    forecast = run_app().dataframe[0].value
    assert pd.isna(forecast.iloc[0]["Rain chance (%)"])
    assert pd.isna(forecast.iloc[1]["Rain chance (%)"])
    assert forecast.iloc[2]["Rain chance (%)"] == 0


def test_ac_1_5_network_down_with_cache_shows_data_as_of(rsps):
    mock_live(rsps, resolve_zip("08807"))
    run_app()  # warms the JSON cache
    import streamlit as st

    st.cache_data.clear()  # as if the 5-minute memo expired
    rsps.reset()  # every request now raises ConnectionError
    at = run_app()
    assert len(at.dataframe[0].value) == 12
    # Stale empty alerts are never shown as a green "all clear".
    assert not at.success
    assert len(at.info) == 1
    assert at.info[0].value.startswith("No active alerts as of ")
    assert at.info[0].value.endswith("ET (could not refresh)")
    # Each section (alerts, hourly, gauges) shows its own stale notice.
    assert len(at.warning) == 3
    assert all(w.value.startswith("Data as of ") and w.value.endswith(" ET") for w in at.warning)
    assert not at.error
    status = gauge_table(at)["Status"]
    assert status["SMVN4"].startswith("cached (as of ") and status["SMVN4"].endswith(" ET)")
    assert status["BDKN4"] == "no reading"


def test_ac_1_5_network_down_without_cache_shows_unavailable(rsps):
    at = run_app()  # empty RequestsMock: every request raises ConnectionError
    assert at.error and all(e.value == "Live data unavailable" for e in at.error)
    assert len(at.error) == 3
    assert not at.dataframe


def test_ac_1_5_recovers_on_next_run_after_outage(rsps):
    at = run_app()
    assert len(at.error) == 3
    mock_live(rsps, resolve_zip("08807"))  # network is back
    at = run_app(at=at)
    assert not at.error
    assert len(at.dataframe[0].value) == 12
    assert [s.value for s in at.success] == ["No active alerts"]


def test_ac_1_5_partial_failure_nws_down_gauges_up(rsps):
    mock_gauges(rsps)
    at = run_app()
    assert [e.value for e in at.error] == ["Live data unavailable", "Live data unavailable"]
    status = gauge_table(at)["Status"]
    assert status["SMVN4"] == "live"
    assert status["BDKN4"] == "no reading"


def test_ac_1_5_partial_failure_gauges_down_marks_gauges_unavailable(rsps):
    mock_nws(rsps, resolve_zip("08807"))
    rsps.get(NWPS_GAUGES_URL, body=requests.exceptions.ConnectionError("down"))
    at = run_app()
    assert len(at.dataframe[0].value) == 12
    assert [e.value for e in at.error] == ["Live data unavailable"]  # gauge section only


def test_ac_1_5_zip_lookup_failure_shows_message(rsps, monkeypatch):
    def broken():
        raise OSError("pgeocode download failed")

    monkeypatch.setattr(location, "_nominatim", broken)
    at = run_app()
    assert [e.value for e in at.error] == ["ZIP lookup unavailable"]
    assert len(rsps.calls) == 0
