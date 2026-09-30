# =============================================================================
# finops-greenit-service — multi-stage Dockerfile
# =============================================================================
# WHY multi-stage:
#   - Stage 1 (builder): installs deps into a virtualenv — includes build tools
#   - Stage 2 (production): copies ONLY the virtualenv + app code — no pip, no gcc
#   - Result: ~180MB image vs ~450MB with build tools left in
#
# WHY non-root:
#   - OpenShift runs containers with random UIDs (1000650000+)
#   - Must not assume UID 0 — base image sets USER, so we don't override
#
# Build:
#   podman build -t quay.io/YOUR_ORG/finops-greenit-service:latest .
#   podman push quay.io/YOUR_ORG/finops-greenit-service:latest
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: builder — install Python dependencies into a virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12.4-slim AS builder

# Prevent Python from writing .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install build dependencies (gcc, libpq-dev for asyncpg compilation)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps into a virtualenv (isolates from system Python)
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: production — lean runtime image
# ---------------------------------------------------------------------------
FROM python:3.12.4-slim AS production

# Copy the virtualenv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install runtime-only system dependency (libpq for asyncpg at runtime)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user (OpenShift best practice)
# OpenShift runs with random UIDs, but having a named user helps local testing
RUN groupadd -r finops && useradd --no-log-init -r -g finops finops

WORKDIR /app

# Copy application code (after deps, for better layer caching)
COPY app/ ./app/
# V1: example multicluster configs (select one with CLUSTERS_CONFIG_FILE)
COPY config/ ./config/

# Ensure the migration files are available (TimescaleDB init)
# Mounted read-only into TimescaleDB container via docker-entrypoint-initdb.d
# This COPY ensures they're part of the image for the StatefulSet
COPY app/adapters/outbound/db/migrations/ /app/migrations/

# Don't switch USER here — OpenShift overrides with its own UID
# For local testing: USER finops

EXPOSE 8080

# Liveness only (V1): /health/live never depends on Prometheus/DB — restarting
# this container would not fix an external dependency. Readiness = /health.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/health/live || exit 1

# Run with uvicorn — single worker for the MVP scheduler (shares event loop)
CMD ["uvicorn", "app.adapters.inbound.api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--log-level", "info"]