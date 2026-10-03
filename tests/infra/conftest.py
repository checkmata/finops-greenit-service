"""
REAL-infrastructure tests (no mocks, no simulator, no SQLite).

They talk to live components and are skipped unless these variables are set:
    INFRA_PROMETHEUS_PRIMARY_URL    e.g. http://192.168.122.242:30090
    INFRA_PROMETHEUS_SECONDARY_URL  e.g. http://192.168.122.50:30090
    INFRA_DATABASE_URL              real PostgreSQL/TimescaleDB (postgresql://…)
    INFRA_API_URL                   running service, e.g. http://192.168.122.50:8080

Run:  set -a; . ./.env.infra; set +a; pytest -m infra -v
(.env.infra is git-ignored; scripts/infra_env.sh generates it from minikube.)
"""

import os

import pytest

REQUIRED = {
    "prometheus": ("INFRA_PROMETHEUS_PRIMARY_URL", "INFRA_PROMETHEUS_SECONDARY_URL"),
    "database": ("INFRA_DATABASE_URL",),
    "api": ("INFRA_API_URL",),
    # V2.x
    "ledger": ("INFRA_LEDGER_PRIMARY_HOST", "INFRA_LEDGER_SECONDARY_HOST", "INFRA_LEDGER_PASSWORD_FILE"),
    "kube": ("INFRA_KUBE_CONTEXTS",),
}


def env_or_skip(group: str) -> list[str]:
    values = [os.getenv(v) for v in REQUIRED[group]]
    if not all(values):
        pytest.skip(f"real infrastructure not configured ({', '.join(REQUIRED[group])})")
    return values


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/infra/" in str(item.fspath):
            item.add_marker(pytest.mark.infra)
