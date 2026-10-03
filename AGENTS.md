# finops-greenit-service — Agent Instructions

## Architecture (non-negotiable)
Strict Hexagonal Architecture (Ports & Adapters).
- `app/domain/` — pure Python only. Zero imports from FastAPI, Pydantic, httpx, or any adapter. Dataclasses, not Pydantic models.
- `app/ports/` — abstract interfaces (ABC) only. No implementation logic.
- `app/adapters/inbound/api/` — FastAPI routers + Pydantic DTOs (schemas/). Maps domain objects to/from DTOs.
- `app/adapters/outbound/` — concrete integrations (Prometheus/Kepler HTTP calls).
- Never let a domain file import anything from `adapters/`. Never let `ports/` contain business logic.

## Data sources — exactly two kinds, nothing else
- OpenShift's built-in Prometheus (already scrapes cAdvisor, kube-state-metrics, node-exporter, kubelet by default — do not suggest deploying these). V1: ONE Prometheus PER observed cluster, listed in `CLUSTERS_CONFIG_FILE` (`config/clusters.*.yaml`); one `PrometheusMetricsAdapter` instance per cluster, created once in `app/composition.py`.
- Kepler eBPF DaemonSet (the only component we deploy ourselves) — scraped by the same Prometheus via ServiceMonitor.
- TimescaleDB (PostgreSQL + TimescaleDB extension) for persistence — deployed as part of our stack (container locally, StatefulSet on OpenShift), not an external managed service. No second scraping system, no live external API. PUE and grid carbon intensity are static constants in `app/config.py`, not fetched.
- All telemetry — Prometheus core metrics AND Kepler metrics — goes through the single `app/adapters/outbound/prometheus_adapter.py`.

## Scope for this build (1-month MVP)
- `recommendation_engine.py` generates advice only. Actuation exists ONLY in two audited paths: the failover controller (V2/V2.x) and the V4 agent (policy → human approval → Git commit → Argo CD). No HPA/VPA patching, no auto-remediation anywhere else.
- `prometheus_adapter.py` still queries Prometheus live for "current state" endpoints — this behavior does NOT change.
- Persistence IS now in scope, added via `storage_port.py` + `app/adapters/outbound/timescale_adapter.py`, using PostgreSQL + the TimescaleDB extension (single container, both relational and time-series data — no polyglot persistence).
- Three tables (V1): `recommendations` (relational, lifecycle pending/approved/rejected/executed/failed enforced by `app/domain/recommendation_lifecycle.py`, de-duplicated by fingerprint), `metric_snapshots` (hypertable, one row per cluster × namespace per run, cost + energy + CO2) and `workload_snapshots` (hypertable, one row per cluster × namespace × workload, cost only). 7-day retention on both hypertables. Schema changes = a new numbered SQL migration (`002_v1_multicluster.sql`), never an edit of an applied one.
- Writes to storage happen from the leader's timers (`snapshot_job.py`, the ML loop `run_ml_loop`, the failover/data-guard loops) and from EXPLICIT human commands (failover approve/reject, agent actions). Routers must NEVER write on a read request — that would duplicate rows on every Grafana refresh. Each recommendation producer only closes (`obsolete`) ITS categories.
- Local dev: TimescaleDB runs via `docker-compose.yaml` (`timescale/timescaledb:latest-pg16` image) — the exact same image later deployed to OpenShift as a StatefulSet. Do not use SQLite; it fails our Grafana-native-plugin and TTL/downsampling requirements.
- Domain layer (`app/domain/`) must remain completely unaware storage exists — SQLAlchemy models live in `app/adapters/outbound/db/models.py`, separate from domain dataclasses, mapped by `timescale_adapter.py`.
- Before implementing any metric, check `docs/metrics-reference.md` for the exact metric name and PromQL pattern — do not invent or assume metric names.

## V1 rules (added)
- Never invent a value: unknown = `None` / `MetricStatus.UNAVAILABLE`, never `0.0` (storage without kubelet stats, idle energy without a Kepler split, energy without attribution).
- Every energy figure carries its provenance (`EnergyAttribution`): container / pod / node_cpu_share (estimated) / unavailable. Estimated figures are flagged in recommendations (`estimation_method`).
- CPU usage = time-average of 5 m rates (`avg_over_time(...[30d:5m])`), never `rate(x[30d])` (under-reports young series). No right-sizing below `MIN_OBSERVATION_HOURS` of history.
- Right-sizing savings = released reservation `(current − recommended) × price × 720` → Σ savings ≤ Σ per-resource waste. Keep a test for this invariant.
- V2 failover: the pure controller (`app/domain/failover_controller.py`) decides with hysteresis; the only workload actuation is **scaling existing Deployments** on the standby via `FailoverActuatorPort` (namespaced Roles, allowed namespaces, replica cap, exclusions), in `auto` mode or after human approval in `manual` (default). Never create/delete resources, never move traffic. Only the leader replica acts. Simulations must be labelled as such (exporter/, docs).
- V2.x data: replicated databases go through `DataReplicationPort`. On failover the standby copy is PROMOTED before scaling; a writable copy on an older timeline is FENCED (data-guard loop, 1 s); the failback is gated on the data (`app/domain/data_replication.py`). A data directory is only rebuilt by an explicit Git change (`LEDGER_RESYNC_GENERATION`), never automatically.
- V2.x GitOps: everything declarative is in Git (Gitea → OCM Placement → Argo CD). On the standby, `Deployment.spec.replicas` is owned by the failover controller (`ignoreDifferences`); everything else by Git. No Secret in Git.
- Secrets for Prometheus auth / K8s API come only from `*_env` or `*_file` references in `config/clusters*.yaml`; inline secret values are rejected by `validate_structure()`.
- Validation means real infrastructure (`pytest -m infra`, docs/INFRASTRUCTURE.md); mocks, SQLite and the simulation exporter are for unit tests / demos only.
- Secrets: `.env` (git-ignored) for local dev only; Kubernetes Secrets / a secret manager otherwise. `manifests/*secret.example.yaml` are templates.

## V3 ML rules
- `app/domain/ml/` stays pure (no numpy/sklearn); Isolation Forest is evaluation-only (`scripts/ml/evaluate.py`), it lost to the robust z-score on real data.
- ML features use POINT-IN-TIME usage (`cpu_cores_used_now`), never the long-window average `cpu_cores_used` (it hides every spike).
- Forecasts are validated temporally (choose on validation origins, score on later test origins); never report a model without its test error vs naive.
- An increase is never a negative "saving" (`estimated_savings_usd` ≥ 0 is an API contract). Standby clusters are never right-sized on their own idle usage.

## V4 agent rules
- Deterministic, no LLM: every sentence of an explanation is computed from stored data; unknown stays "unknown". A request change has NO direct energy effect — say so, never invent kWh.
- Prioritization returns its factors, weights and contributions (no opaque score).
- The agent has NO Kubernetes write identity: changes are Git commits (own Gitea identity, one repo) applied by Argo CD, verified via Prometheus, rolled back by an inverse commit. Never apply a diff different from the approved dry-run.
- ML values are workload TOTALS: convert to per-pod before any resize. Policy: primary only, ±50 % per step, memory never below observed p99, allowlisted actions (resize/scale) only.

## Conventions
- Type hints on every function.
- Docstrings explaining the "why," not just the "what" — this project must be defensible line-by-line in an oral exam.
- pytest for tests; domain engine tests use plain fixtures, no mocks (pure functions). Only `prometheus_adapter.py` integration tests use HTTP mocking.
