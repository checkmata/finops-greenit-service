"""
Self-observability of the service (V1): Prometheus metrics exposed on /metrics.

WHY: the Deployment already declared `prometheus.io/path: /metrics` but the app
exposed nothing. These metrics answer "is the scheduler working, per cluster?"
They describe the SERVICE, not the business figures (those live in TimescaleDB).
"""

from prometheus_client import Counter, Gauge, Histogram

SCHEDULER_CYCLES = Counter(
    "finops_scheduler_cycles_total", "Snapshot cycles run", ["cluster", "outcome"]
)
SCHEDULER_CYCLE_SECONDS = Histogram(
    "finops_scheduler_cycle_duration_seconds", "Duration of one cluster cycle", ["cluster"]
)
NAMESPACE_ERRORS = Counter(
    "finops_namespace_processing_errors_total", "Namespaces that failed in a cycle", ["cluster"]
)
NAMESPACES_PROCESSED = Gauge(
    "finops_namespaces_processed", "Namespaces processed in the last cycle", ["cluster"]
)
RECOMMENDATIONS_WRITTEN = Counter(
    "finops_recommendations_upserts_total", "Recommendation upserts by action", ["cluster", "action"]
)
LAST_CYCLE_TIMESTAMP = Gauge(
    "finops_scheduler_last_cycle_timestamp_seconds", "Unix time of the last cycle end", ["cluster"]
)

# --- V2 HA / failover ---
SCHEDULER_SKIPPED_NOT_LEADER = Counter(
    "finops_scheduler_skipped_not_leader_total", "Cycles skipped because this replica is not the leader"
)
IS_LEADER = Gauge("finops_is_leader", "1 if this replica holds the scheduler/failover leadership")
FAILOVER_ACTIVE_CLUSTER = Gauge("finops_failover_active", "1 for the cluster the controller considers active",
                                ["cluster"])
