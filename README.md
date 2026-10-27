# FloodSense

**Live flood risk, explained, for any US ZIP code.**

FloodSense is a Streamlit app that combines live National Weather Service alerts and forecasts, river gauge readings and historical flood reports into a knowledge graph. It then applies simple, transparent rules to that graph to give a flood risk level (**low**, **elevated** or **high**) along with the reasons for it, the roads to avoid and where to get help. Residents can also sign up for free phone push alerts when the risk in their ZIP goes up.

Built for the Congressional App Challenge (NJ-07). The default ZIP is `08807` (Bridgewater, NJ), but every page works for any US ZIP.

## Features

| Page | What it shows |
| --- | --- |
| **View environmental intelligence** | The ZIP's knowledge graph (live data → places → actions), its risk level, graph statistics, and **Ask the graph**, a rule-based assistant that answers questions like "why is the risk high?" or "which roads flood?" |
| **Search weather forecast** | A dashboard of the ZIP's risk, the next 12 hours of weather, active alerts and nearby stream gauges |
| **Flood map** | Flood spots colored by current risk, river gauges and open shelters, with layers you can switch on and off |
| **Flood alerts** | Each active NWS flood alert, with the affected roads, precautions and a "Why this alert?" explanation |
| **Local news** | Recent flood, storm and weather news for the area (Google News RSS, shown as plain text) |
| **Get phone alerts** | Register a ZIP, then subscribe to its [ntfy](https://ntfy.sh) topic by link or QR code |
| **Get help** | Emergency lines, the local NWS office and open FEMA shelters, nearest first. Still works when live data is down. |

A sidebar ZIP box is shared by every page, and a breaking-alert banner appears at the top of every page when there's an active warning.

## How it works

```
ZIP ──► places record (per ZIP, versioned in Supabase, rebuilt weekly)
          waterways, gauges, flood spots, history, shelters, help lines
      + live inputs (cached, network first)
          NWS alerts, hourly rain forecast, NWPS/USGS gauge stages
      ──► knowledge graph (networkx)
      ──► risk rules ──► level + reasons + precautions
```

**Places record** ([data/places.py](data/places.py)): Built automatically for any ZIP from public sources: NWS/NWPS gauges and their official flood stages, Iowa Environmental Mesonet storm reports and alert history, OpenFEMA disaster declarations and NFIP claims, and FEMA open shelters. Nothing is hand-curated. Records are stored in Supabase, with a local copy in `data/cache/places/`. A record is rebuilt after 7 days, or sooner if one of its sections failed.

**Knowledge graph** ([graph/build.py](graph/build.py), [graph/context.py](graph/context.py)): Nodes for the area, alerts, rain, gauges, waterways, flood spots, roads, past storms, FEMA disasters, precautions and help.

**Risk rules** ([graph/reason.py](graph/reason.py)). All thresholds live in [graph/thresholds.py](graph/thresholds.py).

1. An active NWS flood alert sets the level: warnings and emergencies give **high**, watches and advisories give **elevated**.
2. A gauge at its action stage makes its waterway **elevated**, and at minor flood stage **high**. A fast-rising gauge raises its waterway one level.
3. Heavy forecast rain (hourly or over 6 hours) makes the area and its flood spots **elevated**.
4. Recent flood spots on a raised waterway are listed as roads to avoid.
5. When the area is **high**, open shelters are listed first.

The area's level is the highest level any rule gives. Every reason can be traced as a path through the graph ([graph/explain.py](graph/explain.py)).

**Live data** ([data/sources.py](data/sources.py), [data/cache.py](data/cache.py)): Every request tries the network first. If it fails, the last cached copy is used and marked stale. ZIP lookups are offline, using [pgeocode](https://pypi.org/project/pgeocode/).

**Phone alerts** ([alerts/](alerts/)): A GitHub Actions job runs every 15 minutes. It computes each registered ZIP's risk with the same pipeline the pages use and sends a push through ntfy when the level rises, with a 6-hour cooldown per level. Each ZIP's topic name includes an HMAC so it can't be guessed.

## Getting started

### Requirements

- Python 3.12
- A [Supabase](https://supabase.com) project (free tier is fine) for places records and phone alert registrations. It's optional for trying the app locally, since places are also cached on disk.

### Setup

```bash
git clone <repo-url> uscongressionalapp-floodsense
cd uscongressionalapp-floodsense
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# then edit .streamlit/secrets.toml (see Configuration below)

streamlit run app.py
```

Open <http://localhost:8501>.

### Supabase

Run [supabase/schema.sql](supabase/schema.sql) once in the Supabase SQL editor (it's safe to re-run). It creates the `places_versions`, `registrations` and `zip_state` tables with row-level security. The app uses only the anon key, which can't delete anything.

### Docker

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill it in first
docker compose up --build
```

The secrets file is mounted read-only. The API cache is kept in a named volume, and logs are written to `./log/` on the host.

## Configuration

Settings are read from `.streamlit/secrets.toml` first, then from environment variables.

| Setting | Used for |
| --- | --- |
| `NWS_USER_AGENT` | **Required by NWS.** Identifies the app and gives a contact, e.g. `FloodSense (you@example.com)` |
| `SUPABASE_URL`, `SUPABASE_KEY` | Supabase project URL and **anon** key |
| `NTFY_TOPIC_SECRET` | Random secret that makes each ZIP's ntfy topic unguessable. Phone alerts are off without it. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(16))"` |
| `APP_URL` | Public app link used in push messages (default `http://localhost:8501`) |
| `SUPABASE_SERVICE_KEY` | Service-role key. Only needed for the scheduled jobs; the app never uses it. |
| `FLOODSENSE_CACHE_DIR`, `FLOODSENSE_LOG_DIR` | Override the cache folder (`data/cache`) and log folder (`log`) |

Other constants, such as the default ZIP, search radii, rebuild ages and national help lines, are in [config.py](config.py).

## Scheduled jobs

Both jobs run in GitHub Actions and need the settings above saved as repository secrets.

- **[check.yml](.github/workflows/check.yml)**: Runs every 15 minutes and checks every registered ZIP, sending a push when the risk rises. You can also run it by hand with a single `zip`, or with a `force_level` to send a test push marked TEST that doesn't change the stored state.
- **[cleanup.yml](.github/workflows/cleanup.yml)**: Runs every Monday and deletes every places version except the latest one per ZIP.

## Command-line tools

Run these from the project root:

```bash
python -m data.places 08807 [--force]                      # build (or rebuild) one ZIP's places record
python -m alerts.check --zip 08807 --dry-run               # print the computed level and push without sending
python -m alerts.check --zip 08807 --force-level elevated  # send a test push
python scripts/cleanup_place_versions.py                   # the weekly cleanup, by hand
python scripts/fetch_ida_archive.py                        # refresh data/history/ida_2021.json (Hurricane Ida gauge heights)
```

## Tests

```bash
python -m pytest            # offline: every HTTP call is mocked with recorded fixtures
python -m pytest -m live    # smoke tests against the real NWS/USGS APIs
```

Test files are named after the acceptance criteria they cover (`test_ac_<story>_<n>_*.py`). The recorded API responses are in [tests/fixtures/](tests/fixtures/).

## Project layout

```
app.py                 app shell: header, navigation, sidebar ZIP, breaking-alert banner
app_pages/             one file per page
ui_shared.py           shared UI pieces (ZIP box, risk badge, alert banner)
assessment_loader.py   places → live inputs → graph → assessment, cached for the pages
news_loader.py         cached news per ZIP
config.py              settings and constants
app_logging.py         daily rolling log in log/
data/                  live fetchers, cache, ZIP lookup, places builder, news, Supabase client
graph/                 knowledge graph, context nodes, risk rules, explanations, Ask the graph
alerts/                scheduled risk check and ntfy push
scripts/               maintenance scripts
supabase/schema.sql    database schema and row-level security policies
tests/                 pytest suite and recorded fixtures
```

## Data sources

All the data comes from free public sources:

- [National Weather Service API](https://www.weather.gov/documentation/services-web-api) (alerts, forecasts, forecast offices)
- [NOAA National Water Prediction Service](https://water.noaa.gov) (river gauges and flood stages)
- [USGS Water Services](https://waterservices.usgs.gov) (instantaneous gauge heights)
- [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu) (local storm reports, past alerts)
- [OpenFEMA](https://www.fema.gov/about/openfema/api) (disaster declarations, NFIP claims) and FEMA open shelters
- [GeoNames](https://www.geonames.org) via pgeocode (ZIP to location)
- Google News RSS (local news)

FloodSense is an informational tool. In an emergency, follow official NWS warnings and local authorities, and call **911**.
