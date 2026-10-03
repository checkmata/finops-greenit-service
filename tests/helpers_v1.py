"""Shared builders and an in-memory MetricsPort for V1 tests (no mocks of domain logic)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from app.adapters.outbound.timescale_adapter import TimescaleAdapter
from app.domain.models.cluster import DeploymentAvailability
from app.domain.models.data_quality import DataQuality, TelemetrySource
from app.domain.models.energy import EnergyAttribution, EnergyMeasurement
from app.domain.models.pod_metric import ClusterAllocatable, PodMetric, StorageMetric
from app.ports.metrics_port import MetricsPort

GB = 1024 ** 3
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def pod(namespace="banking", workload="*", cluster="primary", **kw) -> PodMetric:
    base = dict(
        namespace=namespace, team="payments", cost_center="cc-100", environment="prod",
        cpu_core_hours=0.6, memory_bytes=0.5 * GB, cpu_requested=2.0, memory_requested=2 * GB,
        timestamp=NOW, cluster=cluster, workload=workload,
    )
    base.update(kw)
    return PodMetric(**base)


def pvc(name="data", used_gb=2.0, cap_gb=10.0, stats=True, namespace="banking") -> StorageMetric:
    return StorageMetric(namespace, name, used_gb * GB, cap_gb * GB, True, NOW, has_usage_stats=stats)


def all_healthy() -> DataQuality:
    return DataQuality(frozenset(TelemetrySource), NOW)


def energy(namespace="banking", kwh=5.0, idle=None, attribution=EnergyAttribution.CONTAINER) -> EnergyMeasurement:
    return EnergyMeasurement(namespace, kwh, idle, attribution, "test")


class InMemoryMetricsPort(MetricsPort):
    """A deterministic cluster: namespaces → (namespace total, workloads, pvcs, energy)."""

    def __init__(self, cluster: str, data: dict, *, reachable=True, deployments=(), quality=None):
        self.cluster = cluster
        self.data = data
        self.reachable = reachable
        self.deployments = list(deployments)
        self.quality = quality or all_healthy()
        self.calls: list[str] = []

    def _ns(self, namespace):
        return self.data.get(namespace)

    async def ping(self) -> bool:
        return self.reachable

    async def list_namespaces(self) -> list[str]:
        return sorted(self.data)

    async def get_pod_metrics(self, namespace: str) -> list[PodMetric]:
        self.calls.append(f"pod:{namespace}")
        d = self._ns(namespace)
        return [d["total"]] if d else []

    async def get_workload_metrics(self, namespace, namespace_total=None):
        d = self._ns(namespace)
        return list(d.get("workloads", [])) if d else []

    async def get_cluster_allocatable(self) -> ClusterAllocatable:
        return ClusterAllocatable(32.0, 128 * GB, NOW)

    async def get_storage_metrics(self, namespace):
        d = self._ns(namespace)
        return d.get("pvcs") if d else None

    async def check_data_quality(self) -> DataQuality:
        return self.quality

    async def get_energy_kwh(self, namespace):
        e = await self.get_energy(namespace)
        return e.total_kwh or 0.0

    async def get_workload_energy(self, namespace, window_seconds=86400):
        d = self._ns(namespace)
        return list(d.get("workload_energy", [])) if d else []

    async def get_energy(self, namespace, window_seconds=86400):
        d = self._ns(namespace)
        if not d or "energy" not in d:
            return EnergyMeasurement(namespace, None, None, EnergyAttribution.UNAVAILABLE, "none")
        return d["energy"]

    async def get_deployment_availability(self):
        return [d if isinstance(d, DeploymentAvailability) else DeploymentAvailability(*d) for d in self.deployments]


async def sqlite_storage() -> TimescaleAdapter:
    adapter = TimescaleAdapter("sqlite+aiosqlite:///:memory:")
    await adapter.ensure_tables_exist()
    return adapter


@asynccontextmanager
async def storage_ctx():
    """SQLite-backed TimescaleAdapter that is ALWAYS closed.

    WHY: aiosqlite runs a non-daemon worker thread; if a failing assertion skips
    close(), the test process hangs at exit instead of reporting the failure.
    """
    adapter = await sqlite_storage()
    try:
        yield adapter
    finally:
        await adapter.close()
