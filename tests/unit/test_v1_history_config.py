"""V1: history aggregation rules and cluster configuration parsing."""

from datetime import timedelta

import pytest

from app.cluster_config import ClusterConfigError, load_clusters, parse_clusters, validate_structure
from app.config import Settings
from app.domain.history import aggregate
from app.domain.models.cluster import ClusterRole
from app.domain.models.snapshot import MetricSnapshot, WorkloadSnapshot
from tests.helpers_v1 import NOW


def snap(minutes=0, cluster="primary", ns="banking", cost=10.0, energy=1.0):
    return MetricSnapshot(
        time=NOW + timedelta(minutes=minutes), namespace=ns, team="t", cost_center="c", environment="e",
        cpu_core_hours=1, memory_gb_hours=1, cost_usd=cost, energy_kwh=energy, co2e_kg=None, waste_pct=0,
        cluster=cluster,
    )


class TestAggregate:
    def test_samples_of_same_series_are_averaged_not_summed(self):
        series = aggregate([snap(0, cost=10), snap(5, cost=20)], metric="cost", group_by=["cluster"])
        assert series[0].points[0].value == pytest.approx(15.0)

    def test_different_series_are_summed(self):
        rows = [snap(0, ns="banking", cost=10), snap(0, ns="trading", cost=5), snap(0, cluster="secondary", cost=1)]
        by_cluster = {s.group["cluster"]: s.points[0].value for s in aggregate(rows, metric="cost", group_by=["cluster"])}
        assert by_cluster == {"primary": 15.0, "secondary": 1.0}

    def test_buckets(self):
        rows = [snap(0), snap(61)]
        s = aggregate(rows, metric="cost", group_by=["namespace"], bucket_seconds=3600)[0]
        assert len(s.points) == 2

    def test_none_is_not_zero(self):
        s = aggregate([snap(0, energy=None)], metric="energy", group_by=["cluster"])[0]
        assert s.points[0].value is None

    def test_validation(self):
        with pytest.raises(ValueError):
            aggregate([snap()], metric="bogus", group_by=["cluster"])
        with pytest.raises(ValueError):
            aggregate([snap()], metric="cost", group_by=["workload"])  # namespace rows have no workload

    def test_workload_scope(self):
        w = WorkloadSnapshot(NOW, "primary", "ecommerce", "frontend", "t", "c", "e", 1, 2, 1, 2, 10, 4, 6, 3)
        s = aggregate([w], metric="savings", group_by=["workload"])[0]
        assert s.group == {"workload": "frontend"} and s.points[0].value == 3


class TestClusterConfig:
    def test_valid(self):
        cs = parse_clusters({"clusters": [
            {"name": "a", "role": "primary", "prometheus_url": "http://a"},
            {"name": "b", "role": "secondary", "prometheus_url": "http://b"},
        ]})
        assert [(c.name, c.role) for c in cs] == [("a", ClusterRole.PRIMARY), ("b", ClusterRole.SECONDARY)]

    @pytest.mark.parametrize("data", [
        {},
        {"clusters": []},
        {"clusters": [{"name": "a", "role": "secondary", "prometheus_url": "http://a"}]},
        {"clusters": [{"name": "a", "role": "primary", "prometheus_url": "u"},
                      {"name": "a", "role": "secondary", "prometheus_url": "u"}]},
        {"clusters": [{"name": "a", "role": "primary", "prometheus_url": "u"},
                      {"name": "b", "role": "primary", "prometheus_url": "u"}]},
        {"clusters": [{"name": "a", "role": "boss", "prometheus_url": "u"}]},
        {"clusters": [{"name": "a", "role": "primary"}]},
    ])
    def test_invalid(self, data):
        with pytest.raises(ClusterConfigError):
            parse_clusters(data)

    def test_single_cluster_fallback(self):
        s = Settings(CLUSTER_NAME="solo", PROMETHEUS_URL="http://p:9090", CLUSTERS_CONFIG_FILE=None)
        assert [(c.name, c.role, c.prometheus_url) for c in load_clusters(s)] == [("solo", ClusterRole.PRIMARY, "http://p:9090")]

    def test_repo_example_files_are_valid(self, monkeypatch):
        import pathlib

        import yaml

        # clusters.real.yaml takes its URLs from the environment (never from Git)
        monkeypatch.setenv("PROMETHEUS_PRIMARY_URL", "http://p:9090")
        monkeypatch.setenv("PROMETHEUS_SECONDARY_URL", "http://s:9090")

        files = sorted(pathlib.Path("config").glob("clusters*.yaml"))
        assert files, "example cluster files missing"
        for f in files:
            validate_structure(yaml.safe_load(f.read_text()))  # secrets are mounted only at runtime

    def test_env_expansion_and_missing_variable(self, monkeypatch):
        monkeypatch.setenv("PROM_A", "http://a:9090")
        cs = parse_clusters({"clusters": [{"name": "a", "role": "primary", "prometheus_url": "${PROM_A}"}]})
        assert cs[0].prometheus_url == "http://a:9090"
        monkeypatch.delenv("PROM_A")
        with pytest.raises(ClusterConfigError):
            parse_clusters({"clusters": [{"name": "a", "role": "primary", "prometheus_url": "${PROM_A}"}]})
