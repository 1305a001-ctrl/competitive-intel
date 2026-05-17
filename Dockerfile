FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
RUN pip install .

# Watchlist YAML is baked in at build time; mount-override via compose
# if you want to edit live without rebuilding.
COPY data/ /app/data/

# Signal-log location; mount a volume here to persist across restarts.
RUN mkdir -p /var/lib/competitive-intel

CMD ["python", "-m", "competitive_intel.main"]
