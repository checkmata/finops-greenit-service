"""V4 GitOps change planner (offline dry-run) — the output is validated by the REAL kustomize."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.agent_gitops import PlanError, cpu_quantity, inverse, memory_quantity, plan_resize, plan_scale

ROOT = Path(__file__).resolve().parents[2]
KUST = (ROOT / "workloads/banking/k8s/overlays/primary/kustomization.yaml").read_text()
ECOM = (ROOT / "workloads/ecommerce/k8s/overlays/primary/kustomization.yaml").read_text()


def test_quantities_round_up():
    assert cpu_quantity(0.1504) == "151m" and cpu_quantity(0.0001) == "1m"
    assert memory_quantity(0.25) == "256Mi" and memory_quantity(0.2501) == "257Mi"


def test_resize_creates_agent_file_and_reference_once():
    p = plan_resize(namespace="banking", cluster="primary", deployment="txn-processor", container="txn-processor",
                    resource="cpu", value=0.2, live={}, kustomization=KUST, agent_file=None)
    paths = [c.path for c in p.changes]
    assert paths == ["workloads/banking/k8s/overlays/primary/agent-patches.yaml",
                     "workloads/banking/k8s/overlays/primary/kustomization.yaml"]
    doc = next(yaml.safe_load_all(p.changes[0].new_content))
    assert doc["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] == "200m"
    assert "+  - path: agent-patches.yaml" in p.diff and "Managed by finops-agent" in p.changes[0].new_content
    # second action on the same overlay: no second reference, the document is updated in place
    p2 = plan_resize(namespace="banking", cluster="primary", deployment="txn-processor", container="txn-processor",
                     resource="memory", value=0.25, live={"memory_limit": 268435456.0, "memory_request": 268435456.0},
                     kustomization=p.changes[1].new_content, agent_file=p.changes[0].new_content)
    assert len(p2.changes) == 1
    res = next(yaml.safe_load_all(p2.changes[0].new_content))["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res == {"requests": {"cpu": "200m", "memory": "256Mi"}, "limits": {"memory": "256Mi"}}


def test_scale_edits_the_replicas_entry_or_adds_one():
    p = plan_scale(namespace="banking", cluster="primary", deployment="txn-processor", replicas=3, kustomization=KUST)
    assert "- {name: txn-processor, count: 3}" in p.changes[0].new_content and "-  - {name: txn-processor, count: 2}" in p.diff
    p2 = plan_scale(namespace="ecommerce", cluster="primary", deployment="frontend", replicas=2, kustomization=ECOM)
    assert p2.changes[0].new_content.rstrip().endswith("replicas:\n  - {name: frontend, count: 2}")
    with pytest.raises(PlanError):
        plan_scale(namespace="x", cluster="primary", deployment="a", replicas=2,
                   kustomization="replicas: [{name: a, count: 1}]\n")


def test_rollback_is_the_exact_inverse():
    p = plan_resize(namespace="banking", cluster="primary", deployment="txn-processor", container="txn-processor",
                    resource="cpu", value=0.2, live={}, kustomization=KUST, agent_file=None)
    inv = inverse(p)
    assert inv[0].new_content is None and inv[1].new_content == KUST      # created file deleted, reference removed


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")
def test_planned_overlay_renders_with_real_kustomize(tmp_path):
    shutil.copytree(ROOT / "workloads", tmp_path / "workloads")
    shutil.copytree(ROOT / "manifests", tmp_path / "manifests")
    ov = tmp_path / "workloads/banking/k8s/overlays/primary"
    p = plan_resize(namespace="banking", cluster="primary", deployment="txn-processor", container="txn-processor",
                    resource="cpu", value=0.15, live={}, kustomization=(ov / "kustomization.yaml").read_text(),
                    agent_file=None)
    for c in p.changes:
        (tmp_path / c.path).write_text(c.new_content)
    s = plan_scale(namespace="banking", cluster="primary", deployment="txn-processor", replicas=1,
                   kustomization=(ov / "kustomization.yaml").read_text())
    (tmp_path / s.changes[0].path).write_text(s.changes[0].new_content)
    out = subprocess.run(["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(ov)],
                         capture_output=True, text=True, check=True).stdout
    dep = next(d for d in yaml.safe_load_all(out) if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "txn-processor")
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] == "150m"
