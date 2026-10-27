"""Get phone alerts: register a ZIP, then subscribe to that ZIP's ntfy topic by link or QR code."""

import io

import qrcode
import streamlit as st

import config
from app_logging import log
from data import supabase_rest
from data.location import INVALID_ZIP_MESSAGE, InvalidZipError, resolve_zip

NOT_CONFIGURED = "Phone alerts are not configured"
NEED_CONSENT = "Please agree to receive alerts"
LOOKUP_UNAVAILABLE = "ZIP lookup unavailable"
SAVE_FAILED = "Couldn't save your registration right now; you can still subscribe below"
NAME_MAX = 50
NTFY_HOME = "https://ntfy.sh"


def qr_png(url: str) -> bytes:
    """The subscribe link as a QR code PNG."""
    buf = io.BytesIO()
    qrcode.make(url).save(buf, format="PNG")
    return buf.getvalue()


st.caption("Get a push on your phone when flood risk rises in your ZIP, even with FloodSense closed.")

# Without the topic secret a topic would be guessable, so no form and no topic are shown.
if not config.ntfy_topic(config.DEFAULT_ZIP):
    st.warning(NOT_CONFIGURED, icon=":material/notifications_off:")
    st.stop()

st.session_state.setdefault("current_zip", config.DEFAULT_ZIP)
# The form keeps its own ZIP field, prefilled from the sidebar's current ZIP (and refilled when that changes).
if "phone_zip" not in st.session_state or st.session_state.get("phone_zip_synced") != st.session_state["current_zip"]:
    st.session_state["phone_zip"] = st.session_state["phone_zip_synced"] = st.session_state["current_zip"]

st.subheader("1. Install ntfy")
st.markdown(f"Install the free **ntfy** app on your phone from [ntfy.sh]({NTFY_HOME}) "
            "(Android or iPhone). No account is needed.")

st.subheader("2. Register your ZIP")
with st.form("phone_alerts_form"):
    zip_code = st.text_input("ZIP code", max_chars=5, key="phone_zip")
    name = st.text_input("Name (optional)", max_chars=NAME_MAX, key="phone_name")
    consent = st.checkbox("I agree to receive flood alerts for this ZIP on my phone", key="phone_consent")
    submitted = st.form_submit_button("Register", type="primary")

if submitted:
    # A new submit replaces the previous result: no old success message or link stays on screen.
    for _k in ("phone_registered_zip", "phone_registered_place", "phone_saved"):
        st.session_state.pop(_k, None)
    try:
        loc = resolve_zip(zip_code)
    except InvalidZipError:
        st.error(INVALID_ZIP_MESSAGE)
        loc = None
    except Exception:
        log.exception("ZIP lookup failed for %r", zip_code)
        st.error(LOOKUP_UNAVAILABLE)
        loc = None
    if loc is not None and not consent:
        st.error(NEED_CONSENT)
    elif loc is not None:
        row = {"zip": loc.zip, "name": (name or "").strip()[:NAME_MAX] or None, "consent": True}
        try:
            supabase_rest.insert_registration(row)
            st.session_state["phone_saved"] = True
        except (supabase_rest.SupabaseUnavailable, supabase_rest.SupabaseError):
            log.warning("Could not save a phone-alert registration for %s", loc.zip, exc_info=True)
            st.session_state["phone_saved"] = False
        st.session_state["phone_registered_zip"] = loc.zip
        st.session_state["phone_registered_place"] = loc.place
        zip_changed = st.session_state.get("current_zip") != loc.zip
        st.session_state["current_zip"] = loc.zip
        if zip_changed:
            st.rerun()  # redraw so the sidebar ZIP box and risk badge follow the registered ZIP

registered = st.session_state.get("phone_registered_zip")
if registered:
    place = st.session_state.get("phone_registered_place") or registered
    if st.session_state.get("phone_saved"):
        st.success(f"You're registered for flood alerts for {place} ({registered}).", icon=":material/check_circle:")
    else:
        st.warning(SAVE_FAILED, icon=":material/cloud_off:")

    url = config.ntfy_topic_url(registered)
    st.subheader("3. Subscribe on your phone")
    st.markdown("Tap the button on your phone, or scan the code with your phone's camera, "
                "then choose **Subscribe** in ntfy.")
    st.link_button("Subscribe in ntfy", url, icon=":material/notifications_active:", type="primary")
    st.image(qr_png(url), width=220, alt=f"QR code for the ntfy subscribe link for ZIP {registered}")
    st.caption("Only your ZIP, the optional name and your consent are saved.")
