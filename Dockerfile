FROM python:3.11-slim

# build-essential: safety net for any pinned package without a prebuilt wheel
# for this platform/arch (most of requirements.txt ships manylinux wheels —
# torch, numpy, pandas, asyncpg — so this rarely actually compiles anything).
# TA-Lib's native library build previously lived here; removed along with the
# TA-Lib/scikit-learn/xgboost pins in Fase 2's dependency audit — none of the
# three were ever imported anywhere in this codebase (see requirements.txt).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# The app imports itself as `swarm_trading.*` — a root-level __init__.py makes
# this directory importable as that top-level package once its *parent* is on
# PYTHONPATH. Mirrors how it's run outside Docker (PYTHONPATH=<parent-of-repo>).
WORKDIR /app/swarm_trading
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data \
    && useradd --create-home --uid 1000 swarm \
    && chown -R swarm:swarm /app /data
USER swarm

EXPOSE 8000
CMD ["python", "main.py"]
