"""Regression for the 2026-10-01 incident (docs/DR.md §5): a cluster outage must never
lead GitOps to delete the failed site's workloads. The REAL long-outage run is
scripts/dr/long-outage-test.sh (evidence in docs/evidence/)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _docs(path: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((ROOT / path).read_text()) if d]


def test_every_applicationset_preserves_resources_when_an_application_is_deleted():
    appsets = [d for d in _docs("gitops/apps/workloads.yaml") if d["kind"] == "ApplicationSet"]
    assert {a["metadata"]["name"] for a in appsets} == {"workloads", "finops-platform"}
    for a in appsets:
        assert a["spec"]["syncPolicy"]["preserveResourcesOnDeletion"] is True, a["metadata"]["name"]


def test_placement_keeps_an_unreachable_or_unavailable_cluster_for_the_whole_outage():
    placement = next(d for d in _docs("gitops/bootstrap/ocm/placements.yaml") if d["kind"] == "Placement")
    tol = {t["key"]: t for t in placement["spec"]["tolerations"]}
    for key in ("cluster.open-cluster-management.io/unreachable", "cluster.open-cluster-management.io/unavailable"):
        assert tol[key]["operator"] == "Exists"
        assert "tolerationSeconds" not in tol[key]          # no expiry: an outage of any length is tolerated
