#!/usr/bin/env bash
# V2.x — REAL GitOps validation (Gitea → Argo CD → kind clusters). Writes evidence.
#   A. Git → Kubernetes : commit an annotation in the GitOps repo, measure until it is on the object.
#   B. Drift (primary)  : scale a Deployment by hand, measure until Argo CD restores the Git value.
#   C. Ownership (standby): replicas on `secondary` belong to the failover controller → NOT reverted,
#                           but a Git-owned field (env) on the same Deployment IS reverted.
#   A'. Revert the Git commit, measure until the annotation is gone.
# Commits go ONLY to the local Gitea repo (finops/platform-gitops), never to the project repo.
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=${1:-docs/evidence/gitops-validation-$(date +%F).log}
WORK=.gitops-work
AUTH="Authorization: Basic $(printf 'finops:%s' "$(cat .secrets/gitea_admin_password)" | base64 -w0)"
P="kubectl --context kind-primary"; S="kubectl --context kind-secondary"
now() { date +%s.%N; }
ts() { date -u +%H:%M:%S; }
log() { echo "[$(ts)] $*" | tee -a "$OUT"; }
wait_for() {  # wait_for <timeout_s> <cmd...> ; prints elapsed seconds
  local t0; t0=$(now)
  timeout "$1" bash -c "until $2; do sleep 1; done" || { log "TIMEOUT after $1 s: $2"; return 1; }
  python3 -c "import sys; print(f'{float(sys.argv[2]) - float(sys.argv[1]):.1f}')" "$t0" "$(now)"
}
git_push() {
  git -C "$WORK" -c user.name=finops-gitops -c user.email=finops@localhost commit -q -am "$1"
  git -C "$WORK" -c http.extraHeader="$AUTH" push -q origin main
  git -C "$WORK" rev-parse --short HEAD
}
: > "$OUT"
log "== GitOps validation — Gitea → Argo CD (hub=secondary, OCM placement) → primary/secondary"
log "Argo CD applications:"; $S -n argocd get applications --no-headers | tee -a "$OUT"
git -C "$WORK" -c http.extraHeader="$AUTH" pull -q --rebase origin main

# ---------- A. Git → Kubernetes ----------
MARK="validation-$(date +%s)"
F=$WORK/workloads/banking/k8s/overlays/primary/kustomization.yaml
cat >> "$F" <<EOF
  - target: {kind: Deployment, name: txn-processor}
    patch: |-
      - op: add
        path: /metadata/annotations/finops.io~1gitops-test
        value: "$MARK"
EOF
REV=$(git_push "gitops test A: annotate txn-processor ($MARK)")
log "A. commit $REV pushed to Gitea (annotation finops.io/gitops-test=$MARK on primary banking/txn-processor)"
DT=$(wait_for 300 "[ \"\$($P -n banking get deploy txn-processor -o jsonpath='{.metadata.annotations.finops\.io/gitops-test}')\" = '$MARK' ]")
log "A. PASS — annotation present in the primary cluster ${DT}s after the push (Argo CD polls Git every 60 s)"
log "A. Argo CD revision of banking-primary: $($S -n argocd get app banking-primary -o jsonpath='{.status.sync.revision}' | cut -c1-7)"

# ---------- B. Drift on primary ----------
GITV=$($P -n banking get deploy fraud-scorer -o jsonpath='{.spec.replicas}')
log "B. Git: banking/fraud-scorer replicas=$GITV on primary → manual kubectl scale to 3"
$P -n banking scale deploy fraud-scorer --replicas=3 >/dev/null
DT=$(wait_for 300 "[ \"\$($P -n banking get deploy fraud-scorer -o jsonpath='{.spec.replicas}')\" = '$GITV' ]")
log "B. PASS — drift detected and reverted to replicas=$GITV by Argo CD self-heal in ${DT}s"

# ---------- C. Ownership split on the standby ----------
SREP=$($S -n banking get deploy fraud-scorer -o jsonpath='{.spec.replicas}')
log "C1. secondary banking/fraud-scorer replicas=$SREP → scale to 2 (what the failover controller does)"
$S -n banking scale deploy fraud-scorer --replicas=2 >/dev/null
sleep 90
AFTER=$($S -n banking get deploy fraud-scorer -o jsonpath='{.spec.replicas}')
SYNC=$($S -n argocd get app banking-secondary -o jsonpath='{.status.sync.status}')
if [ "$AFTER" = 2 ]; then log "C1. PASS — replicas still 2 after 90 s (ignoreDifferences /spec/replicas); app sync=$SYNC"; else log "C1. FAIL — replicas=$AFTER"; fi
$S -n banking scale deploy fraud-scorer --replicas="$SREP" >/dev/null
log "C2. secondary banking/fraud-scorer: manual env change TPS=999 (Git-owned field)"
$S -n banking set env deploy/fraud-scorer TPS=999 >/dev/null
DT=$(wait_for 300 "[ \"\$($S -n banking get deploy fraud-scorer -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"TPS\")].value}')\" != '999' ]")
log "C2. PASS — Git-owned field restored on the standby by Argo CD in ${DT}s"

# ---------- A'. Revert in Git ----------
git -C "$WORK" -c user.name=finops-gitops -c user.email=finops@localhost revert --no-edit HEAD >/dev/null
git -C "$WORK" -c http.extraHeader="$AUTH" push -q origin main
log "A'. git revert $(git -C "$WORK" rev-parse --short HEAD) pushed"
DT=$(wait_for 300 "[ -z \"\$($P -n banking get deploy txn-processor -o jsonpath='{.metadata.annotations.finops\.io/gitops-test}')\" ]")
log "A'. PASS — annotation removed from the cluster ${DT}s after the revert"
log "Final:"; $S -n argocd get applications --no-headers | tee -a "$OUT"
