# finops-greenit-service — Agent Instructions

## Architecture (non-negotiable)
Strict Hexagonal Architecture (Ports & Adapters).
- `app/domain/` — pure Python only. Zero imports from FastAPI, Pydantic, httpx, or any adapter. Dataclasses, not Pydantic models.
- `app/ports/` — abstract interfaces (ABC) only. No implementation logic.
- `app/adapters/inbound/api/` — FastAPI routers + Pydantic DTOs (schemas/). Maps domain objects to/from DTOs.
- `app/adapters/outbound/` — concrete integrations (Prometheus/Kepler HTTP calls).
- Never let a domain file import anything from `adapters/`. Never let `ports/` contain business logic.

## Data sources — exactly two, nothing else
- OpenShift's built-in Prometheus (already scrapes cAdvisor, kube-state-metrics, node-exporter, kubelet by default — do not suggest deploying these).
- Kepler eBPF DaemonSet (the only component we deploy ourselves) — scraped by the same Prometheus via ServiceMonitor.
- TimescaleDB (PostgreSQL + TimescaleDB extension) for persistence — deployed as part of our stack (container locally, StatefulSet on OpenShift), not an external managed service. No second scraping system, no live external API. PUE and grid carbon intensity are static constants in `app/config.py`, not fetched.
- All telemetry — Prometheus core metrics AND Kepler metrics — goes through the single `app/adapters/outbound/prometheus_adapter.py`.

## Scope for this build (1-month MVP)
- `recommendation_engine.py` generates advice only. Do NOT implement any action/actuation logic (no HPA/VPA patching, no auto-remediation) — this is explicitly deferred.
- `prometheus_adapter.py` still queries Prometheus live for "current state" endpoints — this behavior does NOT change.
- Persistence IS now in scope, added via `storage_port.py` + `app/adapters/outbound/timescale_adapter.py`, using PostgreSQL + the TimescaleDB extension (single container, both relational and time-series data — no polyglot persistence).
- Two tables only: `recommendations` (relational, has a status lifecycle: pending/approved/executed/dismissed) and `metric_snapshots` (TimescaleDB hypertable, one row per namespace per scheduler run, 7-day retention policy via `add_retention_policy`).
- Writes to storage happen ONLY from `app/scheduler/snapshot_job.py`, running on a timer. Routers must NEVER write to storage directly on every request — that would duplicate rows on every Grafana refresh.
- Local dev: TimescaleDB runs via `docker-compose.yaml` (`timescale/timescaledb:latest-pg16` image) — the exact same image later deployed to OpenShift as a StatefulSet. Do not use SQLite; it fails our Grafana-native-plugin and TTL/downsampling requirements.
- Domain layer (`app/domain/`) must remain completely unaware storage exists — SQLAlchemy models live in `app/adapters/outbound/db/models.py`, separate from domain dataclasses, mapped by `timescale_adapter.py`.
- Before implementing any metric, check `docs/metrics-reference.md` for the exact metric name and PromQL pattern — do not invent or assume metric names.

## Conventions
- Type hints on every function.
- Docstrings explaining the "why," not just the "what" — this project must be defensible line-by-line in an oral exam.
- pytest for tests; domain engine tests use plain fixtures, no mocks (pure functions). Only `prometheus_adapter.py` integration tests use HTTP mocking.
