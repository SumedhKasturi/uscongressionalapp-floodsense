FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PGEOCODE_DATA_DIR=/app/.pgeocode \
    FLOODSENSE_CACHE_DIR=/app/data/cache \
    FLOODSENSE_LOG_DIR=/app/log \
    TZ=America/New_York

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Pre-download the pgeocode US data so ZIP lookups work offline at runtime.
RUN python -c "import pgeocode; r = pgeocode.Nominatim('us').query_postal_code('08807'); assert r.latitude == r.latitude, 'pgeocode warm-up failed'"

# App code only; .dockerignore keeps .streamlit/secrets.toml and the cache out.
COPY . .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data/cache /app/log \
    && chown -R app:app /app/data/cache /app/log /app/.pgeocode
USER app

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)" || exit 1

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false"]
