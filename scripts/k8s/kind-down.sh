#!/usr/bin/env bash
set -euo pipefail
for c in primary secondary; do kind delete cluster --name "finops-$c" || true; done
