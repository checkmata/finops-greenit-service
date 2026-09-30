#!/usr/bin/env bash
# Publishes the declarative state (workloads/*/k8s, manifests/, gitops/) to the LOCAL
# Git server (Gitea) that Argo CD watches. The project repository itself is not
# committed or pushed anywhere by this script.
# Usage: scripts/gitops/publish.sh "commit message"
set -euo pipefail
cd "$(dirname "$0")/../.."
MSG=${1:?commit message}
WORK=.gitops-work                     # git-ignored clone of finops/platform-gitops
PW=$(cat .secrets/gitea_admin_password)
AUTH="Authorization: Basic $(printf 'finops:%s' "$PW" | base64 -w0)"
URL=http://127.0.0.1:3001/finops/platform-gitops.git
if [[ ! -d $WORK/.git ]]; then
  git -c http.extraHeader="$AUTH" clone -q "$URL" "$WORK" 2>/dev/null || { mkdir -p "$WORK"; git -C "$WORK" init -q -b main; git -C "$WORK" remote add origin "$URL"; }
fi
git -C "$WORK" -c http.extraHeader="$AUTH" pull -q --rebase origin main 2>/dev/null || true
# V4: changes committed by the agent (approved actions) since our last publication are
# brought BACK into the project first — otherwise the rsync below would silently revert them.
LAST=$(git -C "$WORK" log -1 --format=%H --author=finops-gitops 2>/dev/null || true)
if [[ -n $LAST ]]; then
  for f in $(git -C "$WORK" log --format= --name-only --author=finops-agent "$LAST..HEAD" | sort -u); do
    case "$f" in workloads/*|gitops/*|manifests/*) ;; *) continue ;; esac
    if [[ -f $WORK/$f ]]; then mkdir -p "$(dirname "$f")"; cp "$WORK/$f" "$f"; echo "pulled back agent change: $f"
    else rm -f "$f"; echo "pulled back agent deletion: $f"; fi
  done
fi
rsync -a --delete --exclude .git --exclude '__pycache__' --exclude '*.py' --exclude Dockerfile \
  --include 'workloads/***' --include 'manifests/***' --include 'gitops/***' --exclude '*' ./ "$WORK/"
git -C "$WORK" add -A
if git -C "$WORK" diff --cached --quiet; then echo "nothing to publish"; exit 0; fi
git -C "$WORK" -c user.name="finops-gitops" -c user.email="finops@localhost" commit -q -m "$MSG"
git -C "$WORK" -c http.extraHeader="$AUTH" push -q origin main
git -C "$WORK" log --oneline -1
