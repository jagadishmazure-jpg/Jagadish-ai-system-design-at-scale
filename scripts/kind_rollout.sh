#!/usr/bin/env bash
# Runs on a free kind cluster (CI). v1 -> v2 rolling update under load, then a broken v3 that must stall.
set -euo pipefail
SHA=${GIT_SHA:-$(git rev-parse --short HEAD)}
IMG=kestrel-api:$SHA
OUT=${OUT:-loadtest/results/k8s_rollout.json}
docker build -q --build-arg GIT_SHA="$SHA" -t "$IMG" .
kind load docker-image "$IMG"
kubectl apply -f k8s/redis.yaml -f k8s/configmap.yaml -f k8s/service.yaml
kubectl rollout status deploy/redis --timeout=120s
sed "s#image: kestrel-api:v1#image: $IMG#" k8s/deployment.yaml | kubectl apply -f -
kubectl rollout status deploy/kestrel-api --timeout=120s
kubectl create configmap loader --from-file=scripts/rollout_loader.py --dry-run=client -o yaml | kubectl apply -f -

load() {  # $1 = name, $2 = seconds
  kubectl run "$1" --image="$IMG" --restart=Never --image-pull-policy=IfNotPresent \
    --overrides='{"spec":{"volumes":[{"name":"l","configMap":{"name":"loader"}}],"containers":[{"name":"'"$1"'","image":"'"$IMG"'","command":["python","/l/rollout_loader.py","http://kestrel-api/version","'"$2"'"],"volumeMounts":[{"name":"l","mountPath":"/l"}]}]}}'
}

# Phase 1: v1 -> v2 under load
load load-v2 45
sleep 5
T0=$(date +%s.%N)
kubectl set env deploy/kestrel-api APP_VERSION=v2
kubectl rollout status deploy/kestrel-api --timeout=120s
T1=$(date +%s.%N)
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/load-v2 --timeout=90s
R2=$(kubectl logs load-v2 | grep RESULT | sed 's/RESULT //')

# Phase 2: broken v3 (readiness fails) -> rollout must stall, v2 keeps serving
load load-v3 40
sleep 3
kubectl set env deploy/kestrel-api APP_VERSION=v3 APP_BROKEN_CONFIG=1
if kubectl rollout status deploy/kestrel-api --timeout=30s; then V3_HALTED=false; else V3_HALTED=true; fi
READY=$(kubectl get deploy kestrel-api -o jsonpath='{.status.readyReplicas}')
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/load-v3 --timeout=90s
R3=$(kubectl logs load-v3 | grep RESULT | sed 's/RESULT //')
kubectl rollout undo deploy/kestrel-api
kubectl rollout status deploy/kestrel-api --timeout=120s

mkdir -p "$(dirname "$OUT")"
python3 - "$R2" "$R3" "$V3_HALTED" "$READY" "$T0" "$T1" "$SHA" > "$OUT" <<'PY'
import json, sys
r2, r3 = json.loads(sys.argv[1]), json.loads(sys.argv[2])
print(json.dumps({"source": "kind cluster in GitHub Actions (real run)", "image_sha": sys.argv[7],
  "v1_to_v2": {**r2, "rollout_seconds": round(float(sys.argv[6]) - float(sys.argv[5]), 1)},
  "broken_v3": {**r3, "rollout_halted": sys.argv[3] == "true", "ready_replicas_during_stall": int(sys.argv[4] or 0),
                "served_v3": r3["versions"].get("v3", 0)}}, indent=2))
PY
cat "$OUT"
python3 - "$OUT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["v1_to_v2"]["failed"] == 0, "requests failed during v1->v2"
assert d["broken_v3"]["rollout_halted"] and d["broken_v3"]["failed"] == 0 and d["broken_v3"]["served_v3"] == 0
print("rollout checks passed")
PY
