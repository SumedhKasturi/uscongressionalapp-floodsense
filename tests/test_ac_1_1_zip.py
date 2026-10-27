"""AC 1.1: ZIP 08807 resolves near Bridgewater; invalid ZIPs give a clear message."""

import pytest
import responses as responses_lib

from data.location import INVALID_ZIP_MESSAGE, InvalidZipError, Location, resolve_zip


def test_ac_1_1_08807_within_005_deg_of_bridgewater():
    loc = resolve_zip("08807")
    assert isinstance(loc, Location)
    assert loc.zip == "08807"
    assert abs(loc.lat - 40.59) <= 0.05
    assert abs(loc.lon - (-74.61)) <= 0.05
    assert "Bridgewater" in loc.place


def test_ac_1_1_other_zip_resolves():
    loc = resolve_zip(" 10001 ")
    assert loc.zip == "10001"
    assert abs(loc.lat - 40.75) < 0.05 and abs(loc.lon - (-74.0)) < 0.05


@pytest.mark.parametrize("bad", ["00000", "abcde", "123", "", "123456", "0880a", None])
def test_ac_1_1_invalid_zip_raises_clear_message(bad):
    with pytest.raises(InvalidZipError) as exc:
        resolve_zip(bad)
    assert str(exc.value) == INVALID_ZIP_MESSAGE == "Enter a valid 5-digit US ZIP"


@pytest.mark.parametrize("bad", ["00000", "abcde", "123"])
def test_ac_1_1_invalid_zip_in_app_shows_message_without_fetch(bad):
    from streamlit.testing.v1 import AppTest

    # Any HTTP call would raise ConnectionError inside an empty RequestsMock.
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as r:
        at = AppTest.from_file("../app.py", default_timeout=15).run()
        r.reset()
        at.text_input(key="zip").set_value(bad).run()
        assert len(r.calls) == 0
    assert not at.exception
    assert [e.value for e in at.error] == ["Enter a valid 5-digit US ZIP"]
