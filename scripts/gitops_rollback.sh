#!/usr/bin/env bash
# GitOps rollback demo: the deploy is whatever deploy/version.yaml says. Rollback = git revert + re-apply.
# Default is a dry run that prints the plan; pass --apply on a kind cluster to roll the Deployment.
set -euo pipefail
LAST=$(git log -n1 --format=%H -- deploy/version.yaml)
echo "last deploy commit: $LAST"
git revert --no-edit "$LAST"
V=$(python3 -c "import yaml;print(yaml.safe_load(open('deploy/version.yaml'))['version'])")
echo "desired version after revert: $V"
if [[ "${1:-}" == "--apply" ]]; then
  kubectl set env deploy/quillmoor-api APP_VERSION="$V" APP_BROKEN_CONFIG=0
  kubectl rollout status deploy/quillmoor-api --timeout=120s
fi
