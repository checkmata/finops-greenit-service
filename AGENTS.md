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
- No external database, no second scraping system, no live external API. PUE and grid carbon intensity are static constants in `app/config.py`, not fetched.
- All telemetry — Prometheus core metrics AND Kepler metrics — goes through the single `app/adapters/outbound/prometheus_adapter.py`.

## Scope for this build (1-month MVP)
- `recommendation_engine.py` generates advice only. Do NOT implement any action/actuation logic (no HPA/VPA patching, no auto-remediation) — this is explicitly deferred.
- No persistence layer. `prometheus_adapter.py` queries live on each request. Do not add a database or `storage_port` implementation unless asked.
- Before implementing any metric, check `docs/metrics-reference.md` for the exact metric name and PromQL pattern — do not invent or assume metric names.

## Conventions
- Type hints on every function.
- Docstrings explaining the "why," not just the "what" — this project must be defensible line-by-line in an oral exam.
- pytest for tests; domain engine tests use plain fixtures, no mocks (pure functions). Only `prometheus_adapter.py` integration tests use HTTP mocking.
