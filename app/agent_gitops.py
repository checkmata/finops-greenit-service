"""
V4 — translate an approved action proposal into a GitOps change (the files Argo CD applies).

    resize_workload → workloads/<ns>/k8s/overlays/<cluster>/agent-patches.yaml
                      (multi-document strategic-merge patch owned by the agent, referenced
                      once from the overlay's kustomization.yaml)
    scale_workload  → the overlay's `replicas:` entry (a patch cannot win over kustomize's
                      replicas transformer — verified with `kubectl kustomize`)

plan_*() never writes: it returns the exact file changes + a unified diff. That is the
OFFLINE dry-run shown to the approver. (A Kubernetes server-side dry-run would need a
write identity on the cluster; the agent deliberately has none — Argo CD applies.)
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass

import yaml

from app.ports.agent_ports import FileChange

HEADER = ("# Managed by finops-agent (V4). Every document here comes from an agent action that passed\n"
          "# the policy and was APPROVED by a human — see agent_actions / agent_audit.\n")


class PlanError(ValueError):
    """The change cannot be expressed safely on this repository layout (refused, nothing written)."""


@dataclass(frozen=True)
class ChangePlan:
    changes: tuple[FileChange, ...]
    diff: str
    summary: str


def overlay_dir(namespace: str, cluster: str) -> str:
    return f"workloads/{namespace}/k8s/overlays/{cluster}"


def cpu_quantity(cores: float) -> str:
    return f"{max(1, math.ceil(cores * 1000 - 1e-6))}m"


def memory_quantity(gib: float) -> str:
    return f"{max(1, math.ceil(gib * 1024 - 1e-6))}Mi"


def _diff(changes: list[FileChange]) -> str:
    out = []
    for ch in changes:
        out += difflib.unified_diff((ch.old_content or "").splitlines(True), (ch.new_content or "").splitlines(True),
                                    fromfile=f"a/{ch.path}", tofile=f"b/{ch.path}")
    return "".join(out)


def _reference(kustomization: str) -> str | None:
    """kustomization.yaml with `- path: agent-patches.yaml` added (None if already there)."""
    if re.search(r"^\s*-\s*path:\s*agent-patches\.yaml\s*$", kustomization, re.M):
        return None
    if re.search(r"^patches:\s*$", kustomization, re.M):
        return kustomization.rstrip("\n") + "\n  # V4 agent actions (approved, audited)\n  - path: agent-patches.yaml\n"
    return kustomization.rstrip("\n") + "\npatches:\n  - path: agent-patches.yaml\n"


def plan_resize(*, namespace: str, cluster: str, deployment: str, container: str, resource: str, value: float,
                live: dict[str, float], kustomization: str, agent_file: str | None) -> ChangePlan:
    if resource not in ("cpu", "memory"):
        raise PlanError(f"unsupported resource {resource}")
    q = cpu_quantity(value) if resource == "cpu" else memory_quantity(value)
    docs = [d for d in yaml.safe_load_all(agent_file or "") if d]
    doc = next((d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == deployment), None)
    if doc is None:
        doc = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": deployment, "namespace": namespace},
               "spec": {"template": {"spec": {"containers": []}}}}
        docs.append(doc)
    containers = doc["spec"]["template"]["spec"]["containers"]
    c = next((c for c in containers if c["name"] == container), None)
    if c is None:
        c = {"name": container, "resources": {}}
        containers.append(c)
    res = c.setdefault("resources", {})
    res.setdefault("requests", {})[resource] = q
    if resource == "memory":
        limit, old_req = live.get("memory_limit"), live.get("memory_request")
        # Keep the manifests' request == limit pattern, and never leave limit < request.
        if limit is not None and (limit < value * 2**30 or (old_req is not None and abs(limit - old_req) < 1)):
            res.setdefault("limits", {})["memory"] = q
    base = overlay_dir(namespace, cluster)
    new_agent = HEADER + yaml.safe_dump_all(docs, sort_keys=False)
    changes = [FileChange(f"{base}/agent-patches.yaml", agent_file, new_agent)]
    ref = _reference(kustomization)
    if ref is not None:
        changes.append(FileChange(f"{base}/kustomization.yaml", kustomization, ref))
    return ChangePlan(tuple(changes), _diff(changes),
                      f"{cluster}/{namespace}/{deployment}[{container}] {resource} request → {q}")


def plan_scale(*, namespace: str, cluster: str, deployment: str, replicas: int, kustomization: str) -> ChangePlan:
    line = re.compile(rf"^(\s*-\s*\{{name:\s*{re.escape(deployment)},\s*count:\s*)(\d+)(\s*\}}\s*)$", re.M)
    matches = line.findall(kustomization)
    if len(matches) > 1:
        raise PlanError(f"{deployment} appears {len(matches)} times in replicas:")
    if matches:
        new = line.sub(rf"\g<1>{replicas}\g<3>", kustomization)
    elif re.search(r"^replicas:\s*$", kustomization, re.M):
        new = re.sub(r"^replicas:\s*$", f"replicas:\n  - {{name: {deployment}, count: {replicas}}}", kustomization,
                     count=1, flags=re.M)
    elif re.search(r"^replicas:", kustomization, re.M):
        raise PlanError("unrecognised replicas: layout (refusing to rewrite it)")
    else:
        new = kustomization.rstrip("\n") + f"\nreplicas:\n  - {{name: {deployment}, count: {replicas}}}\n"
    ch = [FileChange(f"{overlay_dir(namespace, cluster)}/kustomization.yaml", kustomization, new)]
    return ChangePlan(tuple(ch), _diff(ch), f"{cluster}/{namespace}/{deployment} replicas → {replicas}")


def inverse(plan: ChangePlan) -> tuple[FileChange, ...]:
    """The rollback change: every file back to its content before the action."""
    return tuple(FileChange(c.path, c.new_content, c.old_content) for c in plan.changes)
