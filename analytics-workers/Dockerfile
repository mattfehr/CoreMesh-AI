# Module: CoreMesh offline analytics-worker container image.
# Role: packages the Phase 4.1 log miner, its scheduler, migrations, and manual
# verification command without adding ML dependencies to services-runtime.
# Dependencies: Python 3.11, requirements.txt, compiled HDBSCAN wheels, and
# PostgreSQL/OpenAI connections supplied at runtime.
# Side effects: build downloads packages; runtime can migrate/write PostgreSQL
# and call the configured embedding and reference-generation providers.

FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 coremesh

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels

COPY --chown=coremesh:coremesh src/ ./src/
COPY --chown=coremesh:coremesh migrations/ ./migrations/
COPY --chown=coremesh:coremesh scripts/ ./scripts/

USER coremesh

ENTRYPOINT ["python", "-m", "src.log_miner.extractor"]
CMD ["schedule"]
